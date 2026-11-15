"""Smart Canteen server.

    python app.py              start the canteen for real: India time (IST), real UPI payments, empty orders
    python app.py --demo       fast demo clock, sample orders and simulated payments (for showing it off)
    python app.py rollup       add today's orders to the sales history the forecast learns from
    python app.py reset        wipe orders and start over (add --demo for the demo data; accounts are kept)

Student app:  http://<this computer>:5000/
Kitchen app:  http://<this computer>:5000/kitchen   (PIN is printed when the server starts; KITCHEN_PIN sets your own)
"""
import argparse
import os
import secrets
import socket
import time

from flask import Flask, g, jsonify, render_template, request, session

import db
import demo
import logic
import payments
import accounts
from db import CFG
from logic import ApiError

ASSET_VERSION = str(int(time.time()))


def create_app(db_path=None, demo_mode=False, kitchen_pin=None, gateway=None, behind_proxy=None):
    app = Flask(__name__, static_folder="static", template_folder="templates")
    # Hosted behind an HTTPS proxy (Fly, Render, Railway, nginx...): trust its forwarded address/scheme, and only send the login cookie over HTTPS.
    if behind_proxy is None:
        behind_proxy = os.environ.get("BEHIND_HTTPS_PROXY", "").strip().lower() in ("1", "true", "yes")
    if behind_proxy:
        from werkzeug.middleware.proxy_fix import ProxyFix
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
    path = db_path or db.DB_PATH
    app.config.update(DB_PATH=path, SEND_FILE_MAX_AGE_DEFAULT=0, SESSION_COOKIE_SAMESITE="Lax",
                      PERMANENT_SESSION_LIFETIME=60 * 60 * 24 * 60)
    boot = db.connect(path)
    fresh = db.init_db(boot, demo=demo_mode)
    if fresh and demo_mode:
        demo.seed_orders(boot)
    key = db.meta_get(boot, "secret_key")
    if not key:
        key = secrets.token_hex(24)
        db.meta_set(boot, "secret_key", key)
    app.secret_key = key
    if behind_proxy:
        app.config["SESSION_COOKIE_SECURE"] = True
    app.config["DEMO"] = logic.is_demo(boot)
    # Kitchen PIN: the one you pass in, else KITCHEN_PIN, else 1234 in demo mode, else a random 6 digits kept in the database.
    pin = kitchen_pin or os.environ.get("KITCHEN_PIN")
    if not pin:
        if app.config["DEMO"]:
            pin = "1234"
        else:
            pin = db.meta_get(boot, "kitchen_pin")
            if not pin:
                pin = f"{secrets.randbelow(900000) + 100000}"
                db.meta_set(boot, "kitchen_pin", pin)
    app.config["KITCHEN_PIN"] = str(pin)
    boot.close()
    login_fails = {}

    def gw_for(c):
        return gateway or payments.get_gateway(logic.is_demo(c))

    def conn():
        if "db" not in g:
            g.db = db.connect(app.config["DB_PATH"])
        return g.db

    @app.teardown_appcontext
    def _close(_exc):
        c = g.pop("db", None)
        if c is not None:
            c.close()

    @app.before_request
    def _roll_day():
        if request.path.startswith("/api/"):
            logic.ensure_day(conn())

    @app.errorhandler(ApiError)
    def _api_error(e):
        return jsonify(error=e.message), e.status

    @app.errorhandler(404)
    def _404(_e):
        if request.path.startswith("/api/"):
            return jsonify(error="Not found."), 404
        return "Not found", 404

    @app.errorhandler(405)
    def _405(_e):
        return jsonify(error="Method not allowed."), 405

    @app.errorhandler(Exception)
    def _boom(e):
        app.logger.exception("Unhandled error")
        if request.path.startswith("/api/"):
            return jsonify(error="Something went wrong on the server. Please try again."), 500
        raise e

    def body():
        return request.get_json(silent=True) or {}

    def kitchen_only():
        if not session.get("kitchen"):
            raise ApiError("Please sign in to the kitchen screen.", 401)

    def current_user():
        return accounts.get_user(conn(), session.get("uid"))

    def demo_only():
        if not logic.is_demo(conn()):
            raise ApiError("Demo controls are off in live mode.", 403)

    # ------------------------------------------------------------ pages
    @app.get("/")
    def page_student():
        return render_template("student.html", v=ASSET_VERSION)

    @app.get("/kitchen")
    def page_kitchen():
        return render_template("kitchen.html", v=ASSET_VERSION)

    @app.get("/healthz")
    def healthz():
        conn().execute("SELECT 1").fetchone()       # proves the database is reachable, not just that Python is up
        return jsonify(ok=True)

    # ------------------------------------------------------------ installable app (PWA): Add to Home Screen gives a full-screen app with an icon
    def manifest(name, short, start, theme):
        icons = [{"src": f"/static/icons/{n}", "sizes": z, "type": "image/png", "purpose": pur}
                 for n, z, pur in (("icon-192.png", "192x192", "any"), ("icon-512.png", "512x512", "any"), ("icon-maskable-512.png", "512x512", "maskable"))]
        data = {"name": name, "short_name": short, "id": start, "start_url": start, "scope": start, "display": "standalone", "orientation": "portrait",
                "background_color": theme, "theme_color": theme, "lang": "en-IN", "icons": icons}
        resp = jsonify(data)
        resp.mimetype = "application/manifest+json"
        return resp

    @app.get("/manifest.webmanifest")
    def manifest_student():
        return manifest(CFG["name"], "Canteen", "/", "#FFD23F")

    @app.get("/kitchen.webmanifest")
    def manifest_kitchen():
        return manifest(CFG["name"] + " Kitchen", "Kitchen", "/kitchen", "#0D1A13")

    @app.get("/sw.js")
    def service_worker():
        # served from the site root so it can control every page; never cached by the browser so updates arrive at once
        resp = app.send_static_file("sw.js")
        resp.headers["Cache-Control"] = "no-cache"
        resp.headers["Service-Worker-Allowed"] = "/"
        return resp

    # ------------------------------------------------------------ shared / student API
    @app.get("/api/config")
    def api_config():
        c = conn()
        demo_on = logic.is_demo(c)
        return jsonify({
            "name": CFG["name"], "demo": demo_on, "slot_minutes": CFG["slot_minutes"], "sessions": CFG["sessions"],
            "banks": CFG["banks"], "hold_seconds": CFG["hold_seconds"], "day_start": CFG["day_start"], "day_end": CFG["day_end"],
            "hours": CFG["hours"], "stock_buffer": CFG["stock_buffer"], "menu_art": {m["id"]: {"art": m["art"], "tint": m["tint"]} for m in CFG["menu"]},
            "gateway": {"kind": gw_for(c).kind, "key_id": getattr(gw_for(c), "key_id", None), "test": bool(getattr(gw_for(c), "is_test", False))}, "pay": logic.pay_info(c, gw_for(c)),
            "hold_seconds_upi": CFG["hold_seconds_upi"], "timezone": "Asia/Kolkata",
            "pin_hint": app.config["KITCHEN_PIN"] if demo_on and app.config["KITCHEN_PIN"] == "1234" else None,
        })

    @app.get("/api/rev")
    def api_rev():
        c = conn()
        logic.expire_holds(c)
        return jsonify({"rev": logic.rev(c), **logic.clock_info(c)})

    @app.get("/api/menu")
    def api_menu():
        c = conn()
        return jsonify({"items": logic.menu(c), "accepting": logic.accepting(c), "capacity": logic.capacity(c), "pay": logic.pay_info(c, gw_for(c))})

    # ------------------------------------------------------------ accounts and timetable
    @app.get("/api/auth/me")
    def auth_me():
        u = current_user()
        return jsonify(user=accounts.user_json(u) if u else None)

    @app.post("/api/auth/signup")
    def auth_signup():
        b = body()
        u = accounts.signup(conn(), b.get("name"), b.get("phone"), b.get("password"))
        session.permanent = True
        session["uid"] = u["id"]
        return jsonify(user=accounts.user_json(u)), 201

    @app.post("/api/auth/login")
    def auth_login():
        b = body()
        u = accounts.login(conn(), b.get("phone"), b.get("password"), who=request.remote_addr or "?")
        session.permanent = True
        session["uid"] = u["id"]
        return jsonify(user=accounts.user_json(u))

    @app.post("/api/auth/logout")
    def auth_logout():
        session.pop("uid", None)
        return jsonify(ok=True)

    @app.put("/api/timetable")
    def put_timetable():
        u = current_user()
        if u is None:
            raise ApiError("Please sign in first.", 401)
        return jsonify(user=accounts.user_json(accounts.save_timetable(conn(), u["id"], body().get("timetable"))))

    # ------------------------------------------------------------ ordering
    @app.post("/api/slots")
    def api_slots():
        return jsonify(logic.suggest_slots(conn(), current_user(), body().get("items")))

    @app.post("/api/orders")
    def api_create_order():
        b = body()
        c = conn()
        return jsonify(logic.create_order(c, gw_for(c), current_user(), b.get("items"), b.get("slot"))), 201

    @app.get("/api/orders/mine")
    def api_my_orders():
        return jsonify({"orders": logic.my_orders(conn(), current_user())})

    @app.post("/api/orders/<oid>/cancel")
    def api_cancel(oid):
        c = conn()
        return jsonify(logic.cancel_order(c, gw_for(c), oid, user=current_user()))

    @app.post("/api/payments/<pid>/pay")
    def api_pay(pid):
        b = body()
        c = conn()
        return jsonify(logic.pay(c, gw_for(c), current_user(), pid, b.get("method"), b.get("details")))

    @app.post("/api/payments/<pid>/utr")
    def api_utr(pid):
        return jsonify(logic.submit_utr(conn(), current_user(), pid, body().get("utr")))

    @app.post("/api/payments/<pid>/razorpay")
    def api_razorpay(pid):
        b = body()
        c = conn()
        return jsonify(logic.razorpay_verify(c, gw_for(c), current_user(), pid, b.get("razorpay_order_id"),
                                             b.get("razorpay_payment_id"), b.get("razorpay_signature")))

    @app.post("/api/razorpay/webhook")
    def api_razorpay_webhook():
        c = conn()
        return jsonify(logic.razorpay_webhook(c, gw_for(c), request.get_data(), request.headers.get("X-Razorpay-Signature")))

    # ------------------------------------------------------------ kitchen API
    @app.post("/api/kitchen/login")
    def k_login():
        ip = request.remote_addr or "?"
        n, first = login_fails.get(ip, (0, time.time()))
        if time.time() - first > 60:
            n, first = 0, time.time()
        if n >= 5:
            raise ApiError("Too many tries. Wait a minute and try again.", 429)
        if str(body().get("pin", "")) != app.config["KITCHEN_PIN"]:
            login_fails[ip] = (n + 1, first)
            raise ApiError("That PIN is not right.", 401)
        login_fails.pop(ip, None)
        session["kitchen"] = True
        return jsonify(ok=True)

    @app.post("/api/kitchen/logout")
    def k_logout():
        session.pop("kitchen", None)
        return jsonify(ok=True)

    @app.get("/api/kitchen/me")
    def k_me():
        return jsonify(ok=bool(session.get("kitchen")))

    @app.get("/api/kitchen/snapshot")
    def k_snapshot():
        kitchen_only()
        c = conn()
        return jsonify({**logic.snapshot(c), "pay": logic.pay_info(c, gw_for(c), kitchen=True)})

    @app.patch("/api/kitchen/orders/<oid>")
    def k_status(oid):
        kitchen_only()
        return jsonify(logic.set_status(conn(), oid, body().get("status")))

    @app.post("/api/kitchen/orders/<oid>/cancel")
    def k_cancel(oid):
        kitchen_only()
        c = conn()
        return jsonify(logic.cancel_order(c, gw_for(c), oid, by_kitchen=True, reason=str(body().get("reason") or "")[:80] or None))

    @app.post("/api/kitchen/orders/<oid>/payment-received")
    def k_confirm(oid):
        kitchen_only()
        return jsonify(logic.confirm_payment(conn(), oid))

    @app.post("/api/kitchen/orders/<oid>/payment-missing")
    def k_reject(oid):
        kitchen_only()
        return jsonify(logic.reject_payment(conn(), oid))

    @app.post("/api/kitchen/orders/<oid>/refunded")
    def k_refunded(oid):
        kitchen_only()
        return jsonify(logic.mark_refunded(conn(), oid))

    @app.post("/api/kitchen/verify")
    def k_verify():
        kitchen_only()
        b = body()
        return jsonify(logic.verify_token(conn(), b.get("token"), b.get("pin")))

    @app.get("/api/kitchen/history")
    def k_history():
        kitchen_only()
        a = request.args
        return jsonify(logic.history(conn(), a.get("q", ""), a.get("status", ""), a.get("limit", 40), a.get("offset", 0)))

    @app.get("/api/kitchen/forecast")
    def k_forecast():
        kitchen_only()
        return jsonify(logic.forecast_view(conn()))

    @app.get("/api/kitchen/inventory")
    def k_inventory():
        kitchen_only()
        return jsonify({"items": logic.inventory(conn())})

    @app.post("/api/kitchen/inventory/apply-forecast")
    def k_apply():
        kitchen_only()
        ids = body().get("ids")
        logic.apply_forecast_stock(conn(), set(ids) if isinstance(ids, list) else None)
        return jsonify({"items": logic.inventory(conn())})

    @app.patch("/api/kitchen/menu/<iid>")
    def k_menu(iid):
        kitchen_only()
        return jsonify({"item": logic.patch_menu(conn(), iid, body())})

    @app.patch("/api/kitchen/settings")
    def k_settings():
        kitchen_only()
        c = conn()
        out = logic.patch_settings(c, body())
        return jsonify({**out, "pay": logic.pay_info(c, gw_for(c), kitchen=True)})

    # ------------------------------------------------------------ demo controls
    @app.post("/api/demo/clock")
    def d_clock():
        demo_only()
        c, b = conn(), body()
        act, t = b.get("action"), logic.now_min(conn())
        if act == "pause":
            logic.clock_set(c, t, False)
        elif act == "resume":
            logic.clock_set(c, t, True)
        elif act == "plus":
            logic.clock_set(c, t + float(b.get("minutes", 5)))
        elif act == "jump":
            logic.clock_set(c, float(b.get("minutes", 770)))
        else:
            raise ApiError("Unknown clock action.")
        return jsonify(logic.clock_info(c))

    @app.post("/api/demo/rush")
    def d_rush():
        demo_only()
        return jsonify(made=demo.rush(conn(), int(body().get("n", 8))))

    @app.post("/api/demo/reset")
    def d_reset():
        demo_only()
        demo.reset(conn())
        return jsonify(ok=True)

    return app


def lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "127.0.0.1"


def main():
    ap = argparse.ArgumentParser(description="Smart Canteen server")
    ap.add_argument("command", nargs="?", default="run", choices=["run", "rollup", "reset"])
    ap.add_argument("--port", type=int, default=5000)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--demo", action="store_true", help="fast demo clock, sample orders, simulated payments (applies when the database is created)")
    ap.add_argument("--live", action="store_true", help="same as the default; kept so older instructions still work")
    args = ap.parse_args()
    payments.load_env_file()          # RAZORPAY_* and UPI_* can live in a .env file next to app.py
    app = create_app(demo_mode=args.demo and not args.live)
    if args.command == "rollup":
        c = db.connect()
        with db.tx(c):
            n = logic.rollup(c, logic.today(c))
        print(f"Added {logic.today(c)} to the sales history ({n} item-hours with sales).")
        return
    if args.command == "reset":
        c = db.connect()
        if args.demo and not args.live:
            demo.reset(c)
            print("Database reset. Demo data restored (accounts were kept).")
        else:
            db.reset_db(c, demo=False)
            print("Database reset. Orders cleared, real clock (accounts were kept).")
        return
    ip = lan_ip()
    gw = payments.get_gateway(app.config["DEMO"])
    kind = gw.kind
    print(f"\n  Smart Canteen is running.\n  Students:  http://{ip}:{args.port}/\n  Kitchen:   http://{ip}:{args.port}/kitchen   (PIN {app.config['KITCHEN_PIN']})")
    if kind == "razorpay":
        pay_line = ("RAZORPAY in TEST mode, no real money moves" if gw.is_test else "RAZORPAY LIVE, real money") + \
                   (", webhook on" if gw.webhook_secret else ", webhook off (set RAZORPAY_WEBHOOK_SECRET)")
    elif kind == "test":
        pay_line = "built-in test gateway, no real money moves"
    else:
        pay_line = "real UPI to the owner. Add your UPI ID in Kitchen > Settings (or set UPI_ID)."
    print("  Payments:  " + pay_line)
    if kind == "razorpay":
        print(f"  Webhook:   https://<your public address>/api/razorpay/webhook   (add it in Razorpay > Webhooks, events payment.authorized + payment.captured)")
    print(f"  Mode:      {'demo (fast clock, sample orders)' if app.config['DEMO'] else 'live, India time (IST): ' + db.ist_now().strftime('%a %d %b %I:%M %p')}\n")
    app.run(host=args.host, port=args.port, threaded=True, debug=False)


if __name__ == "__main__":
    main()
