"""Business rules. Routes in app.py stay thin and call into this file.

Order life:  pending_payment -> (verifying) -> new -> preparing -> ready -> picked_up
             "verifying" is only used for UPI: the student says they paid and the kitchen confirms it.
             (pending_payment can become expired; new/preparing/ready can become cancelled with a refund)
All times are Indian Standard Time (IST).
"""
import hmac
import json
import math
import os
import re
import secrets
import time
from datetime import date

import accounts
import db
import forecast
import payments
from accounts import ApiError
from db import CFG, HOURS, bump, meta_get, meta_set, tx

SLOT = CFG["slot_minutes"]
LIVE = ("new", "preparing", "ready")
PAID = ("new", "preparing", "ready", "picked_up")
COOKING = ("new", "preparing")
FORWARD = {"new": "preparing", "preparing": "ready", "ready": "picked_up"}
BACKWARD = {"preparing": "new", "ready": "preparing", "picked_up": "ready"}


# ------------------------------------------------------------------ clock
def is_demo(conn):
    return meta_get(conn, "clock_mode") == "demo"


def now_min(conn):
    if not is_demo(conn):
        return db.ist_minutes()       # real clock, always India time
    base = float(meta_get(conn, "clock_base"))
    if meta_get(conn, "clock_running") != "1":
        return base
    return min(float(CFG["day_end"]), base + (db.now_ts() - float(meta_get(conn, "clock_t0"))) / CFG["sec_per_min"])


def clock_info(conn):
    return {"now": now_min(conn), "running": meta_get(conn, "clock_running") == "1" or not is_demo(conn),
            "sec_per_min": CFG["sec_per_min"] if is_demo(conn) else 60, "demo": is_demo(conn), "day": today(conn)}


def clock_set(conn, minutes, running=None):
    if not is_demo(conn):
        raise ApiError("The clock can only be changed in demo mode.", 403)
    with tx(conn):
        meta_set(conn, "clock_base", max(CFG["day_start"], min(CFG["day_end"], float(minutes))))
        meta_set(conn, "clock_t0", db.now_ts())
        if running is not None:
            meta_set(conn, "clock_running", 1 if running else 0)
        bump(conn)


def today(conn):
    return meta_get(conn, "day")


def accepting(conn):
    return meta_get(conn, "accepting") == "1"


def capacity(conn):
    return int(meta_get(conn, "capacity"))


def rev(conn):
    return int(meta_get(conn, "rev", "0"))


# ------------------------------------------------------------------ day roll-over (live mode only)
def rollup(conn, day):
    """Add one finished day of orders to the sales history the forecast learns from."""
    rows = conn.execute(
        """SELECT oi.item_id, CAST(o.slot / 60 AS INTEGER) AS hour, SUM(oi.qty) AS q
           FROM order_items oi JOIN orders o ON o.id = oi.order_id
           WHERE o.day=? AND o.status IN ('new','preparing','ready','picked_up') GROUP BY oi.item_id, hour""", (day,)).fetchall()
    seen = {(r["item_id"], r["hour"]): r["q"] for r in rows}
    for m in CFG["menu"]:
        for h in HOURS:
            conn.execute("INSERT OR REPLACE INTO history VALUES(?,?,?,?)", (day, m["id"], h, seen.get((m["id"], h), 0)))
    return len(rows)


def ensure_day(conn):
    if is_demo(conn):
        return
    d = db.ist_today()
    if meta_get(conn, "day") != d:
        with tx(conn):
            rollup(conn, meta_get(conn, "day"))
            meta_set(conn, "day", d)
            meta_set(conn, "next_token", 1001)
            bump(conn)
        apply_forecast_stock(conn)


# ------------------------------------------------------------------ menu and stock
def sold_map(conn, day):
    rows = conn.execute(
        """SELECT oi.item_id, SUM(oi.qty) AS q FROM order_items oi JOIN orders o ON o.id = oi.order_id
           WHERE o.day=? AND (o.status IN ('new','preparing','ready','picked_up','verifying')
                              OR (o.status='pending_payment' AND o.hold_until > ?)) GROUP BY oi.item_id""",
        (day, db.now_ts())).fetchall()
    return {r["item_id"]: r["q"] for r in rows}


def menu(conn):
    sold = sold_map(conn, today(conn))
    out = []
    for r in conn.execute("SELECT * FROM items ORDER BY sort"):
        s = sold.get(r["id"], 0)
        left = max(0, r["stock"] - s)
        out.append({"id": r["id"], "name": r["name"], "category": r["category"], "price": r["price"], "prep": r["prep"],
                    "art": r["art"], "tint": r["tint"], "available": bool(r["available"]), "stock": r["stock"], "sold": s,
                    "left": left, "orderable": bool(r["available"]) and left > 0})
    return out


def apply_forecast_stock(conn, ids=None):
    """Set today's cooking plan from the forecast (plus a safety buffer), never below what is already sold."""
    day = today(conn)
    model = forecast.Model(conn, day)
    sold = sold_map(conn, day)
    with tx(conn):
        for m in CFG["menu"]:
            if ids is not None and m["id"] not in ids:
                continue
            want = math.ceil(model.day_total(m["id"]) * CFG["stock_buffer"])
            conn.execute("UPDATE items SET stock=? WHERE id=?", (max(want, sold.get(m["id"], 0)), m["id"]))
        bump(conn)


# ------------------------------------------------------------------ slot suggestions
def windows_for(conn, user, t):
    """Where the student can pick up today. Their own timetable gaps if they have any for today's weekday,
    otherwise the canteen's general break times. Returns (windows, source)."""
    gaps = accounts.gaps_for(user, today(conn))
    if gaps:
        return [{"session": {"id": f"w{i}", "label": "Your break"}, "start": a, "end": b}
                for i, (a, b) in enumerate(gaps) if b > t], "timetable"
    return [{"session": s, "start": s["from"], "end": s["to"]} for s in CFG["sessions"] if s["to"] > t], "canteen"


def slot_starts(w):
    """Pickup slots sit on the 5 minute grid (12:30, 12:35 ...) inside a window."""
    first = -(-int(math.ceil(w["start"])) // SLOT) * SLOT
    return range(first, int(w["end"]), SLOT)


def prep_minutes(conn, items):
    if not items:
        return 3
    prep = {r["id"]: r["prep"] for r in conn.execute("SELECT id, prep FROM items")}
    qty = sum(i["qty"] for i in items)
    return max(prep[i["id"]] for i in items) + qty // 4


def slot_load(conn, day, slot):
    r = conn.execute(
        """SELECT COALESCE(SUM(oi.qty),0) AS q FROM order_items oi JOIN orders o ON o.id = oi.order_id
           WHERE o.day=? AND o.slot=? AND (o.status IN ('new','preparing','verifying')
                                            OR (o.status='pending_payment' AND o.hold_until > ?))""",
        (day, slot, db.now_ts())).fetchone()
    return r["q"]


def earliest_slot(conn, items, t):
    return int(math.ceil((t + prep_minutes(conn, items) + 1) / SLOT) * SLOT)


def suggest_slots(conn, user, items):
    t = now_min(conn)
    items = _clean_items(conn, items, allow_empty=True)
    qty = sum(i["qty"] for i in items) or 1
    cap = capacity(conn)
    day = today(conn)
    earliest = earliest_slot(conn, items, t)
    chosen = None
    wins, source = windows_for(conn, user, t)
    for w in wins:
        slots = []
        for s in slot_starts(w):
            if s < earliest:
                continue
            load = slot_load(conn, day, s)
            slots.append({"start": s, "load": load, "ratio": min(1, load / cap), "full": load + qty > cap})
        if not slots:
            continue
        if any(not x["full"] for x in slots):
            chosen = {**w, "slots": slots}
            break
        if chosen is None:
            chosen = {**w, "slots": slots}
    if chosen is None:
        return {"closed": True, "capacity": cap, "source": source}
    ok = [x for x in chosen["slots"] if not x["full"]]
    best, reason = None, ""
    if ok:
        best = next((x for x in ok if x["ratio"] <= 0.6), None) or min(ok, key=lambda x: (x["ratio"], x["start"]))
        first_any = chosen["slots"][0]
        if first_any["full"] and best is ok[0]:
            reason = f"The {fmt_time(first_any['start'])} slot is full. This is the earliest with room."
        elif best is ok[0]:
            reason = "Earliest slot where the kitchen has room."
        else:
            reason = f"Skips the busy {fmt_time(ok[0]['start'])} slot ({ok[0]['load']} of {cap} items booked)."
    return {"closed": False, "session": chosen["session"], "start": chosen["start"], "end": chosen["end"], "slots": chosen["slots"],
            "best": {"start": best["start"]} if best else None, "reason": reason, "earliest": earliest, "qty": qty,
            "capacity": cap, "source": source}


def fmt_time(m):
    m = int(m)
    h, mm = divmod(m, 60)
    return f"{h % 12 or 12}:{mm:02d} {'am' if h < 12 else 'pm'}"


# ------------------------------------------------------------------ order shapes
def _clean_items(conn, items, allow_empty=False):
    valid = {r["id"]: r for r in conn.execute("SELECT * FROM items")}
    merged = {}
    for i in items or []:
        iid, q = str(i.get("id")), i.get("qty")
        if iid not in valid:
            raise ApiError("One of the items is not on the menu any more.")
        if not isinstance(q, int) or isinstance(q, bool) or q < 1 or q > 10:
            raise ApiError("Quantity must be between 1 and 10.")
        merged[iid] = min(10, merged.get(iid, 0) + q)
    if not merged and not allow_empty:
        raise ApiError("Add something to your order first.")
    return [{"id": k, "qty": v} for k, v in merged.items()]


def _payment_row(conn, order_id):
    return conn.execute("SELECT * FROM payments WHERE order_id=? ORDER BY created DESC, rowid DESC LIMIT 1", (order_id,)).fetchone()


def upi_cfg(conn):
    """The owner's UPI ID and the name students see. Set in the kitchen Settings, or with UPI_ID / UPI_NAME on the server."""
    return {"id": meta_get(conn, "upi_id") or os.environ.get("UPI_ID", "").strip().lower() or "",
            "name": meta_get(conn, "upi_name") or os.environ.get("UPI_NAME", "").strip() or CFG["name"]}


def pay_info(conn, gateway, kitchen=False):
    """Which payment method is live. The kitchen also gets the UPI ID so it can edit it."""
    c = upi_cfg(conn)
    out = {"kind": gateway.kind, "ready": gateway.kind != "upi" or bool(c["id"]), "test": bool(getattr(gateway, "is_test", False))}
    if kitchen:
        out.update({"upi_id": c["id"], "upi_name": c["name"], "webhook": bool(getattr(gateway, "webhook_secret", None))})
    return out


def upi_for(conn, amount, token_no):
    c = upi_cfg(conn)
    return payments.upi_params(c["id"], c["name"], amount, f"Canteen #{token_no}")


def payment_json(p):
    if p is None:
        return None
    return {"id": p["id"], "status": p["status"], "method": p["method"], "label": p["label"], "last4": p["last4"],
            "amount": p["amount"], "gateway": p["gateway"], "ref": p["ref"], "error": p["error"],
            "test": p["gateway"] == "test", "refund_ref": p["refund_ref"]}


def _phone(conn, uid):
    if not uid:
        return None
    u = conn.execute("SELECT phone FROM users WHERE id=?", (uid,)).fetchone()
    return u["phone"] if u else None


def order_json(conn, r, t, owner=False):
    items = [{"id": i["item_id"], "name": i["name"], "qty": i["qty"], "price": i["price"]}
             for i in conn.execute("SELECT * FROM order_items WHERE order_id=? ORDER BY rowid", (r["id"],))]
    d = {"id": r["id"], "token_no": r["token_no"], "token": f"#{r['token_no']}", "name": r["name"], "section": r["section"],
         "section_label": CFG["sections"].get(r["section"], {}).get("label", ""), "phone": _phone(conn, r["user_id"]), "slot": r["slot"], "status": r["status"], "total": r["total"],
         "placed": r["placed"], "paid_at": r["paid_at"], "prep_at": r["prep_at"], "ready_at": r["ready_at"],
         "picked_at": r["picked_at"], "cancelled_at": r["cancelled_at"], "cancel_reason": r["cancel_reason"], "items": items,
         "payment": payment_json(_payment_row(conn, r["id"])),
         "delayed": r["status"] in COOKING and r["slot"] < t}
    if owner:
        d["pin"] = r["pin"]
        d["qr"] = f"SCANTEEN|{r['token_no']}|{r['pin']}|{r['id']}"
        d["hold_left"] = max(0, int(r["hold_until"] - db.now_ts())) if r["status"] == "pending_payment" and r["hold_until"] else 0
        if d["payment"] and d["payment"]["gateway"] == "upi" and r["status"] in ("pending_payment", "verifying"):
            d["upi"] = upi_for(conn, r["total"], r["token_no"])
        if d["payment"] and d["payment"]["gateway"] == "razorpay" and r["status"] == "pending_payment":
            pr = _payment_row(conn, r["id"])
            d["rp"] = {"rp_order_id": pr["rp_order_id"]}
    return d


def _get_order(conn, order_id):
    r = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
    if r is None:
        raise ApiError("Order not found.", 404)
    return r


def _check_owner(r, user):
    """Students can only touch their own orders."""
    if user is None:
        raise ApiError("Please sign in first.", 401)
    if r["user_id"] != user["id"]:
        raise ApiError("Order not found.", 404)


# ------------------------------------------------------------------ student: create, pay, cancel
def _expire(conn):
    """Release orders whose payment hold ran out. Call inside a transaction."""
    t = now_min(conn)
    rows = conn.execute("SELECT id FROM orders WHERE status='pending_payment' AND hold_until IS NOT NULL AND hold_until < ?", (db.now_ts(),)).fetchall()
    for r in rows:
        conn.execute("UPDATE orders SET status='expired', cancelled_at=?, cancel_reason='Payment was not completed in time.' WHERE id=?", (t, r["id"]))
        conn.execute("UPDATE payments SET status='expired' WHERE order_id=? AND status IN ('created','failed')", (r["id"],))
    if rows:
        bump(conn)
    return len(rows)


def expire_holds(conn):
    if conn.execute("SELECT 1 FROM orders WHERE status='pending_payment' AND hold_until < ? LIMIT 1", (db.now_ts(),)).fetchone():
        with tx(conn):
            _expire(conn)


def _insert_order(conn, day, token_no, name, section, slot, status, items, placed, hold_until=None, times=None, user_id=None):
    prices = {r["id"]: (r["name"], r["price"]) for r in conn.execute("SELECT id, name, price FROM items")}
    total = sum(prices[i["id"]][1] * i["qty"] for i in items)
    oid = "ord_" + secrets.token_hex(4)
    times = times or {}
    conn.execute(
        """INSERT INTO orders(id,day,token_no,pin,secret,name,section,slot,status,total,hold_until,placed,paid_at,prep_at,ready_at,picked_at,cancelled_at,cancel_reason,user_id)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (oid, day, token_no, f"{secrets.randbelow(10000):04d}", secrets.token_urlsafe(16), name, section, slot, status, total,
         hold_until, placed, times.get("paid_at"), times.get("prep_at"), times.get("ready_at"), times.get("picked_at"),
         times.get("cancelled_at"), times.get("cancel_reason"), user_id))
    for i in items:
        conn.execute("INSERT INTO order_items VALUES(?,?,?,?,?)", (oid, i["id"], prices[i["id"]][0], prices[i["id"]][1], i["qty"]))
    return oid, total


def _next_token(conn):
    n = int(meta_get(conn, "next_token", "1001"))
    meta_set(conn, "next_token", n + 1)
    return n


def create_order(conn, gateway, user, items, slot):
    """Start checkout. The order is held for a few minutes while the student pays; the kitchen cannot see it yet."""
    if user is None:
        raise ApiError("Please sign in to order.", 401)
    items = _clean_items(conn, items)
    if not isinstance(slot, int) or isinstance(slot, bool):
        raise ApiError("Pick a pickup slot.")
    day = today(conn)
    upi = gateway.kind == "upi"
    real = gateway.kind in ("upi", "razorpay")      # real money takes longer than a simulated checkout
    with tx(conn):
        _expire(conn)
        if not accepting(conn):
            raise ApiError("The kitchen has paused new orders for a moment.", 409)
        if upi and not upi_cfg(conn)["id"]:
            raise ApiError("The canteen has not set up payments yet. Please ask the staff.", 503)
        t = now_min(conn)
        wins, _src = windows_for(conn, user, t)
        if not any(slot in slot_starts(w) for w in wins):
            raise ApiError("That pickup slot is not in one of your breaks. Pick another.", 409)
        if slot < earliest_slot(conn, items, t):
            raise ApiError("That slot is too soon for this order. Pick a later one.", 409)
        # one open checkout per student: starting a new one releases the old slot
        conn.execute("UPDATE orders SET status='cancelled', cancelled_at=?, cancel_reason='Replaced by a newer order.', hold_until=NULL "
                     "WHERE user_id=? AND status='pending_payment'", (t, user["id"]))
        sold = sold_map(conn, day)
        rows = {r["id"]: r for r in conn.execute("SELECT * FROM items")}
        for i in items:
            r = rows[i["id"]]
            left = r["stock"] - sold.get(i["id"], 0)
            if not r["available"] or left <= 0:
                raise ApiError(f"{r['name']} just sold out.", 409)
            if i["qty"] > left:
                raise ApiError(f"Only {left} {r['name']} left.", 409)
        qty = sum(i["qty"] for i in items)
        if slot_load(conn, day, slot) + qty > capacity(conn):
            raise ApiError("That slot just filled up. Pick another.", 409)
        hold_s = CFG["hold_seconds_upi"] if real else CFG["hold_seconds"]
        hold = db.now_ts() + hold_s
        oid, total = _insert_order(conn, day, _next_token(conn), user["name"], "", slot, "pending_payment", items, t,
                                   hold_until=hold, user_id=user["id"])
        pid = "pay_" + secrets.token_hex(6)
        conn.execute("INSERT INTO payments(id,order_id,status,amount,gateway,created) VALUES(?,?,?,?,?,?)",
                     (pid, oid, "created", total, gateway.kind, db.now_ts()))
        bump(conn)
    extra = {}
    if gateway.kind == "razorpay":
        try:
            extra = gateway.prepare(pid, total, oid)
        except Exception:
            with tx(conn):
                conn.execute("UPDATE orders SET status='expired', cancel_reason='Payment service unavailable.' WHERE id=?", (oid,))
                bump(conn)
            raise ApiError("Online payments are unavailable right now. Please try again in a minute.", 502)
        conn.execute("UPDATE payments SET rp_order_id=? WHERE id=?", (extra.get("rp_order_id"), pid))
    r = _get_order(conn, oid)
    out = {"order": order_json(conn, r, now_min(conn), owner=True), "hold_seconds": hold_s,
           "payment": {"id": pid, "amount": total, "gateway": gateway.kind}}
    if gateway.kind == "razorpay":
        out["payment"].update({"key_id": gateway.key_id, "rp_order_id": extra.get("rp_order_id")})
    if upi:
        out["payment"]["upi"] = upi_for(conn, total, r["token_no"])
    return out


def _capture(conn, order, payment, result, method):
    t = now_min(conn)
    conn.execute("UPDATE payments SET status='captured', method=?, label=?, last4=?, ref=?, error=NULL, captured_at=? WHERE id=?",
                 (method, result.get("label"), result.get("last4"), result.get("ref"), db.now_ts(), payment["id"]))
    conn.execute("UPDATE orders SET status='new', paid_at=?, hold_until=NULL WHERE id=?", (t, order["id"]))
    bump(conn)


def _payment_of(conn, payment_id, user):
    p = conn.execute("SELECT * FROM payments WHERE id=?", (payment_id,)).fetchone()
    if p is None:
        raise ApiError("Payment not found.", 404)
    o = _get_order(conn, p["order_id"])
    _check_owner(o, user)
    return p, o


def pay(conn, gateway, user, payment_id, method, details):
    """Take a test-gateway payment. Returns success or a clear reason it failed (the student can retry)."""
    p, o = _payment_of(conn, payment_id, user)
    if gateway.kind != "test":
        raise ApiError("This canteen takes real payments. Use the payment screen for your order.", 400)
    with tx(conn):
        _expire(conn)
        o = _get_order(conn, o["id"])
        if o["status"] == "expired":
            raise ApiError("Your slot hold ran out. Pick a slot again.", 410)
        if o["status"] != "pending_payment":
            raise ApiError("This order is no longer waiting for payment.", 409)
        if p["attempts"] >= 10:
            raise ApiError("Too many attempts. Start the order again.", 429)
        conn.execute("UPDATE payments SET attempts=attempts+1 WHERE id=?", (payment_id,))
    result = gateway.charge(str(method), details or {}, p["amount"])
    with tx(conn):
        o = _get_order(conn, o["id"])
        if o["status"] != "pending_payment":
            raise ApiError("This order is no longer waiting for payment.", 409)
        p2 = conn.execute("SELECT * FROM payments WHERE id=?", (payment_id,)).fetchone()
        if result["ok"]:
            _capture(conn, o, p2, result, str(method))
        else:
            conn.execute("UPDATE payments SET status='failed', error=?, method=? WHERE id=?", (result["error"], str(method), payment_id))
            bump(conn)
    o = _get_order(conn, o["id"])
    if result["ok"]:
        return {"status": "success", "order": order_json(conn, o, now_min(conn), owner=True)}
    return {"status": "failed", "error": result["error"]}


def gateway_paid(conn, gateway, rp_order_id, rp_payment_id):
    """Razorpay says this payment is good. Used twice for every payment, whichever arrives first wins and the second is a no-op:
    the student's return from the Razorpay window, and Razorpay's own webhook (which still arrives if the student closed the app).
    Returns (order row, 'paid' | 'already' | 'late'). 'late' means the money moved after the slot hold ended, so it is refunded."""
    p = conn.execute("SELECT * FROM payments WHERE rp_order_id=? AND gateway='razorpay' ORDER BY rowid DESC LIMIT 1", (rp_order_id,)).fetchone()
    if p is None:
        raise ApiError("This payment does not match any order.", 404)
    try:   # ask Razorpay what really happened (network, so outside any transaction); also captures an authorised payment
        pay = gateway.settle(rp_payment_id, rp_order_id, p["amount"])
    except payments.GatewayError as e:
        raise ApiError(f"We could not confirm the payment yet: {e}", 502)
    label = gateway.describe(pay)
    late = False
    with tx(conn):
        _expire(conn)      # holds are released lazily: make sure one that has run out is released before we decide
        o = _get_order(conn, p["order_id"])
        p = conn.execute("SELECT * FROM payments WHERE id=?", (p["id"],)).fetchone()
        if p["status"] in ("captured", "refunded", "refund_due"):
            return o, "already"
        if o["status"] == "pending_payment" and o["day"] == today(conn):
            _capture(conn, o, p, {"ref": rp_payment_id, "label": label}, "razorpay")
            return _get_order(conn, o["id"]), "paid"
        late = True       # the hold ran out (or the order was cancelled) but the money did move
        conn.execute("UPDATE payments SET status='captured', method='razorpay', label=?, ref=?, error=NULL, captured_at=? WHERE id=?",
                     (label, rp_payment_id, db.now_ts(), p["id"]))
        bump(conn)
    if late:
        try:
            r = gateway.refund({"ref": rp_payment_id}, p["amount"])
            with tx(conn):
                conn.execute("UPDATE payments SET status='refunded', refunded_at=?, refund_ref=? WHERE id=?", (db.now_ts(), r.get("ref"), p["id"]))
                bump(conn)
        except payments.GatewayError:
            with tx(conn):     # the kitchen sees it under "Refunds to send" and can refund it from the Razorpay dashboard
                conn.execute("UPDATE payments SET status='refund_due' WHERE id=?", (p["id"],))
                bump(conn)
    return _get_order(conn, p["order_id"]), "late"


def razorpay_verify(conn, gateway, user, payment_id, rp_order_id, rp_payment_id, signature):
    """The student comes back from the Razorpay window with a signed receipt. Check it, confirm with Razorpay, then release the order."""
    p, o = _payment_of(conn, payment_id, user)
    if gateway.kind != "razorpay" or p["rp_order_id"] != rp_order_id:
        raise ApiError("This payment does not match the order.", 400)
    if not gateway.verify(rp_order_id, rp_payment_id, signature):
        raise ApiError("Payment could not be verified.", 400)
    o, outcome = gateway_paid(conn, gateway, rp_order_id, rp_payment_id)
    if outcome == "late":
        raise ApiError("Your payment reached us after the slot hold ended, so it is being refunded to you automatically. Please place the order again.", 409)
    return {"status": "success", "order": order_json(conn, o, now_min(conn), owner=True)}


def razorpay_webhook(conn, gateway, raw_body, signature):
    """Razorpay calls this by itself when a payment goes through, even if the student has closed the app."""
    if gateway.kind != "razorpay" or not getattr(gateway, "webhook_secret", None):
        raise ApiError("Webhooks are not set up on this canteen.", 503)
    if not gateway.verify_webhook(raw_body, signature):
        raise ApiError("Bad signature.", 400)
    try:
        ev = json.loads(raw_body.decode("utf-8"))
    except ValueError:
        raise ApiError("Unreadable payload.", 400)
    ent = ((((ev or {}).get("payload") or {}).get("payment") or {}).get("entity")) or {}
    if ev.get("event") in ("payment.authorized", "payment.captured") and ent.get("order_id") and ent.get("id"):
        try:
            gateway_paid(conn, gateway, ent["order_id"], ent["id"])
        except ApiError as e:
            if e.status != 404:      # unknown order: not ours, nothing to do. Anything else makes Razorpay retry later.
                raise
    return {"ok": True}


# ------------------------------------------------------------------ UPI: the student says "I paid", the kitchen confirms
def submit_utr(conn, user, payment_id, utr):
    """The student typed the 12 digit UPI transaction ID from their payment app. The order moves to the kitchen's
    'confirm payments' list and keeps its slot while it waits."""
    p, o = _payment_of(conn, payment_id, user)
    if p["gateway"] != "upi":
        raise ApiError("This order is not paid with UPI.", 400)
    utr = re.sub(r"\s", "", str(utr or ""))
    if not payments.UTR_RE.match(utr):
        raise ApiError("The UPI transaction ID has 12 digits. Find it in your payment app under the payment details.")
    with tx(conn):
        _expire(conn)
        o = _get_order(conn, o["id"])
        p = conn.execute("SELECT * FROM payments WHERE id=?", (payment_id,)).fetchone()
        if o["status"] == "expired":
            raise ApiError("Your slot hold ran out. Please start the order again.", 410)
        if o["status"] == "verifying":
            return {"status": "verifying", "order": order_json(conn, o, now_min(conn), owner=True)}
        if o["status"] != "pending_payment":
            raise ApiError("This order is no longer waiting for payment.", 409)
        if p["attempts"] >= 5:
            raise ApiError("Too many tries for this order. Please start a new order.", 429)
        if conn.execute("SELECT 1 FROM payments WHERE gateway='upi' AND ref=? AND id<>?", (utr, payment_id)).fetchone():
            raise ApiError("This transaction ID was already used for another order.", 409)
        conn.execute("UPDATE payments SET status='verifying', ref=?, method='upi', label=?, attempts=attempts+1, error=NULL WHERE id=?",
                     (utr, "UPI", payment_id))
        conn.execute("UPDATE orders SET status='verifying', hold_until=NULL WHERE id=?", (o["id"],))
        bump(conn)
    return {"status": "verifying", "order": order_json(conn, _get_order(conn, o["id"]), now_min(conn), owner=True)}


def confirm_payment(conn, order_id):
    """Kitchen: the money arrived in the owner's UPI app. The order goes to the cooking queue."""
    with tx(conn):
        o = _get_order(conn, order_id)
        if o["status"] != "verifying":
            raise ApiError("This payment is not waiting for confirmation.", 409)
        if o["day"] != today(conn):
            raise ApiError("This order is from an earlier day, so it cannot be cooked now. Mark it as not received.", 409)
        p = _payment_row(conn, order_id)
        _capture(conn, o, p, {"ref": p["ref"], "label": p["label"]}, "upi")
    return {"order": order_json(conn, _get_order(conn, order_id), now_min(conn))}


def reject_payment(conn, order_id):
    """Kitchen: no such payment in the owner's UPI app. The student gets another go (check the ID, or pay again)."""
    with tx(conn):
        o = _get_order(conn, order_id)
        if o["status"] != "verifying":
            raise ApiError("This payment is not waiting for confirmation.", 409)
        p = _payment_row(conn, order_id)
        msg = "The canteen could not find this payment. Check the transaction ID, or pay again."
        if o["day"] != today(conn) or p["attempts"] >= 5:
            conn.execute("UPDATE orders SET status='expired', cancelled_at=?, cancel_reason='Payment was not received.', hold_until=NULL WHERE id=?",
                         (now_min(conn), order_id))
            conn.execute("UPDATE payments SET status='failed', error=?, ref=NULL WHERE id=?", ("Payment was not received.", p["id"]))
        else:
            conn.execute("UPDATE payments SET status='failed', error=?, ref=NULL WHERE id=?", (msg, p["id"]))
            conn.execute("UPDATE orders SET status='pending_payment', hold_until=? WHERE id=?", (db.now_ts() + CFG["hold_seconds_upi"], order_id))
        bump(conn)
    return {"order": order_json(conn, _get_order(conn, order_id), now_min(conn))}


def mark_refunded(conn, order_id):
    """Kitchen: the owner sent the money back from the UPI app."""
    with tx(conn):
        o = _get_order(conn, order_id)
        p = _payment_row(conn, order_id)
        if p is None or p["status"] != "refund_due":
            raise ApiError("There is no refund waiting for this order.", 409)
        conn.execute("UPDATE payments SET status='refunded', refunded_at=?, refund_ref='sent by the canteen' WHERE id=?", (db.now_ts(), p["id"]))
        bump(conn)
    return {"order": order_json(conn, _get_order(conn, order_id), now_min(conn))}


def _refund_if_paid(conn, gateway, o):
    p = _payment_row(conn, o["id"])
    if p is None or p["status"] != "captured":
        return
    r = gateway.refund(p, p["amount"])
    if r.get("manual"):     # UPI: no way to send money from here, so the owner is reminded to do it
        conn.execute("UPDATE payments SET status='refund_due' WHERE id=?", (p["id"],))
    else:
        conn.execute("UPDATE payments SET status='refunded', refunded_at=?, refund_ref=? WHERE id=?", (db.now_ts(), r["ref"], p["id"]))


def cancel_order(conn, gateway, order_id, user=None, by_kitchen=False, reason=None):
    o = _get_order(conn, order_id)
    if not by_kitchen:
        _check_owner(o, user)
    allowed = ("new", "preparing", "ready") if by_kitchen else ("new", "pending_payment")
    with tx(conn):
        o = _get_order(conn, order_id)
        if o["status"] not in allowed:
            if o["status"] in ("preparing", "ready"):
                raise ApiError("The kitchen has already started this order, so it cannot be cancelled.", 409)
            if o["status"] == "verifying":
                raise ApiError("The canteen is checking your payment. Ask the staff at the counter if you need to cancel.", 409)
            raise ApiError("This order cannot be cancelled any more.", 409)
        try:
            _refund_if_paid(conn, gateway, o)
        except Exception:
            raise ApiError("The refund could not be started. Please try again.", 502)
        if o["status"] == "pending_payment":
            conn.execute("UPDATE payments SET status='expired' WHERE order_id=? AND status IN ('created','failed')", (order_id,))
        conn.execute("UPDATE orders SET status='cancelled', cancelled_at=?, cancel_reason=?, hold_until=NULL WHERE id=?",
                     (now_min(conn), reason or ("Cancelled by the kitchen." if by_kitchen else "Cancelled by the student."), order_id))
        bump(conn)
    return {"order": order_json(conn, _get_order(conn, order_id), now_min(conn), owner=not by_kitchen)}


def my_orders(conn, user):
    """The signed-in student's orders from today, newest first."""
    expire_holds(conn)
    if user is None:
        return []
    t = now_min(conn)
    rows = conn.execute("SELECT * FROM orders WHERE user_id=? AND day=? ORDER BY placed DESC, token_no DESC LIMIT 12", (user["id"], today(conn))).fetchall()
    return [order_json(conn, r, t, owner=True) for r in rows]


# ------------------------------------------------------------------ kitchen
FAILS = {}


def kitchen_order_json(conn, r, t):
    return order_json(conn, r, t, owner=False)


def snapshot(conn):
    expire_holds(conn)
    t = now_min(conn)
    day = today(conn)
    rows = conn.execute("SELECT * FROM orders WHERE day=? AND status IN ('new','preparing','ready') ORDER BY slot, placed", (day,)).fetchall()
    orders = [kitchen_order_json(conn, r, t) for r in rows]
    counts = {"new": 0, "preparing": 0, "ready": 0, "delayed": 0}
    for o in orders:
        counts[o["status"]] += 1
        counts["delayed"] += 1 if o["delayed"] else 0
    st = conn.execute("SELECT COUNT(*) AS n, COALESCE(SUM(total),0) AS s FROM orders WHERE day=? AND status='picked_up'", (day,)).fetchone()
    got = conn.execute("SELECT COALESCE(SUM(total),0) AS s FROM orders WHERE day=? AND status IN ('new','preparing','ready','picked_up')", (day,)).fetchone()
    verifying = [kitchen_order_json(conn, r, t) for r in conn.execute("SELECT * FROM orders WHERE status='verifying' ORDER BY placed, token_no")]
    refunds = [kitchen_order_json(conn, r, t) for r in conn.execute(
        """SELECT o.* FROM orders o JOIN payments p ON p.order_id=o.id WHERE p.status='refund_due' ORDER BY o.cancelled_at, o.token_no""")]
    return {"now": t, "rev": rev(conn), "accepting": accepting(conn), "capacity": capacity(conn), "orders": orders, "counts": counts,
            "picked_up": st["n"], "load": load_next(conn, t), "day": day, "verifying": verifying, "refunds": refunds, "collected": got["s"]}


def load_next(conn, t, minutes=15):
    cap = capacity(conn) * (minutes // SLOT)
    r = conn.execute(
        "SELECT COALESCE(SUM(oi.qty),0) AS q FROM order_items oi JOIN orders o ON o.id=oi.order_id WHERE o.day=? AND o.status IN ('new','preparing') AND o.slot < ?",
        (today(conn), t + minutes)).fetchone()
    return {"booked": r["q"], "capacity": cap, "percent": min(100, half_up_int(100 * r["q"] / cap)) if cap else 0, "minutes": minutes}


def half_up_int(x):
    return int(x + 0.5)


def set_status(conn, order_id, to):
    with tx(conn):
        o = _get_order(conn, order_id)
        cur = o["status"]
        if to not in (FORWARD.get(cur), BACKWARD.get(cur)):
            raise ApiError(f"This order is {cur.replace('_', ' ')}, so it cannot move to {to.replace('_', ' ')}.", 409)
        t = now_min(conn)
        sets = {"preparing": ("prep_at", t), "ready": ("ready_at", t), "picked_up": ("picked_at", t)}
        if to == FORWARD.get(cur):
            col, val = sets[to]
            conn.execute(f"UPDATE orders SET status=?, {col}=? WHERE id=?", (to, val, order_id))
        else:  # undo: clear the time of the step we are leaving
            col = sets[cur][0]
            conn.execute(f"UPDATE orders SET status=?, {col}=NULL WHERE id=?", (to, order_id))
        bump(conn)
    return {"order": kitchen_order_json(conn, _get_order(conn, order_id), now_min(conn))}


def verify_token(conn, token_no, pin):
    key = str(token_no)
    cnt, ts = FAILS.get(key, (0, 0))
    if cnt >= 5 and time.time() - ts < 60:
        raise ApiError("Too many wrong PINs for this token. Wait a minute and try again.", 429)
    try:
        n = int(str(token_no).lstrip("#"))
    except ValueError:
        raise ApiError("Enter the token number, like 1047.")
    r = conn.execute("SELECT * FROM orders WHERE day=? AND token_no=?", (today(conn), n)).fetchone()
    if r is None or r["status"] not in PAID:
        raise ApiError("No paid order with that token today.", 404)
    if not hmac.compare_digest(str(pin or ""), r["pin"]):
        FAILS[key] = (cnt + 1 if time.time() - ts < 60 else 1, time.time())
        raise ApiError("The PIN does not match this token.", 403)
    FAILS.pop(key, None)
    return {"ok": True, "order": kitchen_order_json(conn, r, now_min(conn))}


def history(conn, q="", status="", limit=40, offset=0):
    day = today(conn)
    where, args = ["day=?", "status IN ('picked_up','cancelled')"], [day]
    if status in ("picked_up", "cancelled"):
        where.append("status=?")
        args.append(status)
    q = (q or "").strip().lstrip("#")
    if q:
        where.append("(CAST(token_no AS TEXT) LIKE ? OR LOWER(name) LIKE ?)")
        args += [q + "%", "%" + q.lower() + "%"]
    cond = " AND ".join(where)
    total = conn.execute(f"SELECT COUNT(*) AS n FROM orders WHERE {cond}", args).fetchone()["n"]
    rows = conn.execute(f"SELECT * FROM orders WHERE {cond} ORDER BY COALESCE(picked_at, cancelled_at, slot) DESC, token_no DESC LIMIT ? OFFSET ?",
                        args + [max(1, min(int(limit), 100)), max(0, int(offset))]).fetchall()
    t = now_min(conn)
    s = conn.execute(
        """SELECT COALESCE(SUM(CASE WHEN status='picked_up' THEN 1 ELSE 0 END),0) AS served,
                  COALESCE(SUM(CASE WHEN status IN ('new','preparing','ready','picked_up') THEN total ELSE 0 END),0) AS revenue,
                  COALESCE(SUM(CASE WHEN status='cancelled' AND paid_at IS NOT NULL THEN 1 ELSE 0 END),0) AS refunds,
                  COALESCE(SUM(CASE WHEN status='cancelled' AND paid_at IS NOT NULL THEN total ELSE 0 END),0) AS refunded
           FROM orders WHERE day=?""", (day,)).fetchone()
    return {"orders": [kitchen_order_json(conn, r, t) for r in rows], "total": total,
            "stats": {"served": s["served"], "revenue": s["revenue"], "refunds": s["refunds"], "refunded": s["refunded"]}}


def inventory(conn):
    day = today(conn)
    model = forecast.Model(conn, day)
    now_h = int(now_min(conn) // 60)
    out = []
    for m in menu(conn):
        day_total = model.day_total(m["id"])
        out.append({**m, "forecast_day": round(day_total), "forecast_rest": round(model.window(m["id"], now_h, 99)),
                    "suggested": math.ceil(day_total * CFG["stock_buffer"])})
    return out


def patch_menu(conn, item_id, patch):
    r = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
    if r is None:
        raise ApiError("Item not found.", 404)
    sets, args = [], []
    if "available" in patch:
        sets.append("available=?")
        args.append(1 if patch["available"] else 0)
    for field, col, lo, hi in (("price", "price", 1, 999), ("prep", "prep", 1, 30), ("stock", "stock", 0, 999)):
        if field in patch:
            v = patch[field]
            if not isinstance(v, int) or isinstance(v, bool) or not lo <= v <= hi:
                raise ApiError(f"{field.capitalize()} must be a whole number from {lo} to {hi}.")
            sets.append(f"{col}=?")
            args.append(v)
    if sets:
        with tx(conn):
            conn.execute(f"UPDATE items SET {', '.join(sets)} WHERE id=?", args + [item_id])
            bump(conn)
    return next(m for m in menu(conn) if m["id"] == item_id)


def patch_settings(conn, patch):
    with tx(conn):
        if "accepting" in patch:
            meta_set(conn, "accepting", 1 if patch["accepting"] else 0)
        if "capacity" in patch:
            v = patch["capacity"]
            if not isinstance(v, int) or isinstance(v, bool) or not 4 <= v <= 60:
                raise ApiError("Capacity must be a whole number from 4 to 60.")
            meta_set(conn, "capacity", v)
        if "upi_id" in patch:
            v = patch["upi_id"]
            if v in (None, ""):
                meta_set(conn, "upi_id", "")
            else:
                try:
                    meta_set(conn, "upi_id", payments.clean_upi_id(v))
                except ValueError as e:
                    raise ApiError(str(e))
        if "upi_name" in patch:
            name = " ".join(str(patch["upi_name"] or "").split())[:30]
            if len(name) < 2:
                raise ApiError("Enter the name students will see when they pay.")
            meta_set(conn, "upi_name", name)
        bump(conn)
    return {"accepting": accepting(conn), "capacity": capacity(conn)}


# ------------------------------------------------------------------ demand forecast page
def label_hour(h):
    return f"{h % 12 or 12} {'AM' if h < 12 else 'PM'}"


def forecast_view(conn):
    day = today(conn)
    t = now_min(conn)
    model = forecast.Model(conn, day)
    now_h = int(t // 60)
    booked = {r["hour"]: r["q"] for r in conn.execute(
        """SELECT CAST(o.slot / 60 AS INTEGER) AS hour, SUM(oi.qty) AS q FROM order_items oi JOIN orders o ON o.id=oi.order_id
           WHERE o.day=? AND o.status IN ('new','preparing','ready','picked_up') GROUP BY hour""", (day,))}
    hours = []
    for h in HOURS:
        pred = sum(model.item_hour(m["id"], h) for m in CFG["menu"])
        hours.append({"hour": h, "label": label_hour(h), "actual": booked.get(h, 0), "predicted": round(pred), "past": h < now_h})
    h0, h1 = now_h, now_h + 3
    top = []
    for m in CFG["menu"]:
        p = model.window(m["id"], h0, h1)
        b = model.window_base(m["id"], h0, h1)
        top.append({"id": m["id"], "name": m["name"], "art": m["art"], "tint": m["tint"], "predicted": round(p),
                    "change": round((p - b) / b * 100) if b >= 1 else 0})
    top.sort(key=lambda x: -x["predicted"])
    totals = [h["predicted"] for h in hours]
    peak = max(totals) if totals else 0
    avg = sum(totals) / len(totals) if totals else 0
    peak_hours = [h["hour"] for h in hours if peak and h["predicted"] >= 0.85 * peak]
    ratio = peak / avg if avg else 0
    level = "Busy" if ratio > 1.6 else "Moderate" if ratio > 1.25 else "Quiet"
    n_orders = conn.execute("SELECT COUNT(*) AS n FROM orders WHERE day=? AND status IN ('new','preparing','ready','picked_up')", (day,)).fetchone()["n"]
    n_items = sum(booked.values())
    per_order = (n_items / n_orders) if n_orders >= 10 else 2.2
    window = f"{fmt_time(min(peak_hours) * 60)} to {fmt_time((max(peak_hours) + 1) * 60)}" if peak_hours else "none"
    inv = {m["id"]: m for m in menu(conn)}
    recs = []
    if top and top[0]["predicted"] > 0:
        recs.append({"kind": "prep", "text": f"Prepare extra {top[0]['name']}: about {top[0]['predicted']} in the next 3 hours."})
    for m in top:
        left = inv[m["id"]]["left"]
        if m["predicted"] > left and m["predicted"] > 0:
            recs.append({"kind": "restock", "text": f"Stock up on {m['name']}: {m['predicted']} expected, only {left} left in today's plan."})
            if sum(1 for r in recs if r["kind"] == "restock") >= 2:
                break
    drinks = [m for m in top if next(c for c in CFG["menu"] if c["id"] == m["id"])["category"] == "Drinks" and m["predicted"] > 0]
    if drinks:
        recs.append({"kind": "ready", "text": f"Keep {drinks[0]['name']} ready before {fmt_time(peak_hours[0] * 60) if peak_hours else 'the rush'}."})
    if level != "Quiet" and peak_hours:
        recs.append({"kind": "crowd", "text": f"Expect the rush {window}. Start cooking early for {top[0]['name'] if top else 'popular items'}."})
    return {"day": day, "weekday": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"][model.wd],
            "now_hour": now_h, "hours": hours, "top": top[:5], "window_hours": [h0, h1],
            "crowd": {"level": level, "window": window, "orders_per_hour": round(peak / per_order) if peak else 0},
            "recs": recs[:5], "load": load_next(conn, t), "history_days": CFG["history_days"]}


# ------------------------------------------------------------------ demo helpers
def insert_paid_order(conn, day, name, section, items, slot, status, placed, method, label, times=None, gateway_kind="test"):
    """Add an order that is already paid (used for demo data and the 'rush' button)."""
    times = dict(times or {})
    times.setdefault("paid_at", placed + 0.5)
    if status in ("preparing", "ready", "picked_up"):
        times.setdefault("prep_at", max(times["paid_at"] + 1, slot - 8))
    if status in ("ready", "picked_up"):
        times.setdefault("ready_at", times["prep_at"] + 4)
    if status == "picked_up":
        times.setdefault("picked_at", max(slot, times["ready_at"]) + 1)
    with tx(conn):
        oid, total = _insert_order(conn, day, _next_token(conn), name, section, slot, status, items, placed, times=times)
        conn.execute(
            "INSERT INTO payments(id,order_id,status,method,label,last4,amount,gateway,ref,created,captured_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            ("pay_" + secrets.token_hex(6), oid, "captured", method, label, None, total, gateway_kind,
             "pay_demo_" + secrets.token_hex(5), db.now_ts(), db.now_ts()))
        bump(conn)
    return oid
