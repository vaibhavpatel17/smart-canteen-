"""Demo data: a believable morning of finished orders plus a lunch rush in progress.

Only used in demo mode. In live mode the canteen starts empty and the forecast learns from real days.
"""
import db
import logic
import forecast
from db import CFG, HOURS, half_up, mulberry32

SLOT = CFG["slot_minutes"]

# name, class, items, slot, status, placed (minutes), extra times
LIVE_ORDERS = [
    ("Kabir", "3A", [("samosa", 2), ("chai", 1)], 760, "ready", 742, {"prep_at": 751, "ready_at": 757}),
    ("Diya", "3A", [("chai", 2), ("vada", 1)], 765, "ready", 746, {"prep_at": 758, "ready_at": 764}),
    ("Rohan", "3A", [("maggi", 1), ("coffee", 1)], 765, "preparing", 748, {"prep_at": 762}),
    ("Meera", "3A", [("dosa", 1)], 775, "preparing", 752, {"prep_at": 766}),
    ("Aarav", "3A", [("sandwich", 2), ("samosa", 2)], 780, "preparing", 755, {"prep_at": 768}),
    ("Isha", "3A", [("roll", 1), ("coffee", 1)], 775, "new", 757, {}),
    ("Kiara", "5A", [("roll", 2)], 765, "new", 750, {}),
    ("Vihaan", "5A", [("maggi", 2), ("samosa", 2), ("chai", 2)], 780, "new", 760, {}),
    ("Ananya", "5A", [("dosa", 2), ("chai", 1)], 785, "new", 761, {}),
    ("Arjun", "5A", [("sandwich", 1), ("vada", 2)], 785, "new", 763, {}),
    ("Saanvi", "7A", [("chai", 1), ("maggi", 1)], 795, "new", 765, {}),
    ("Advait", "7A", [("coffee", 2), ("samosa", 1)], 800, "new", 767, {}),
]


def _label(rnd, name):
    r = rnd()
    if r < 0.68:
        return "upi", f"UPI · {name[:2].lower()}••••@upi"
    if r < 0.86:
        return "card", f"{'Visa' if rnd() < 0.6 else 'Mastercard'} •••• {1000 + int(rnd() * 9000)}"
    return "netbanking", f"Netbanking · {CFG['banks'][int(rnd() * len(CFG['banks']))]}"


def seed_orders(conn):
    """Finished orders for this morning (so the forecast chart has real 'actual' bars), then the live rush."""
    day = logic.today(conn)
    now = float(CFG["demo_start"])
    cutoff = now - 10
    rnd = mulberry32(777)
    model = forecast.Model(conn, day)
    rows = []
    for h in HOURS:
        if h * 60 >= cutoff:
            continue
        frac = min(1.0, (cutoff - h * 60) / 60)
        left = []
        for m in CFG["menu"]:
            q = half_up(model.item_hour(m["id"], h) * (0.88 + rnd() * 0.24) * frac)
            if q > 0:
                left.append([m["id"], q])
        span = int(min(60, cutoff - h * 60) // SLOT)
        while any(q > 0 for _, q in left):
            avail = [e for e in left if e[1] > 0]
            k = min(len(avail), 1 + int(rnd() * 3))
            items = []
            for _ in range(k):
                e = avail.pop(int(rnd() * len(avail)))
                q = min(e[1], 1 + int(rnd() * 2))
                e[1] -= q
                items.append({"id": e[0], "qty": q})
            rows.append((h * 60 + SLOT * int(rnd() * span), items, CFG["demo_names"][int(rnd() * len(CFG["demo_names"]))],
                         list(CFG["sections"])[int(rnd() * 3)], rnd()))
    rows.sort(key=lambda r: r[0])
    for slot, items, name, section, r in rows:
        method, label = _label(mulberry32(int(r * 1e9)), name)
        placed = max(CFG["day_start"], slot - 6 - int(r * 12))
        logic.insert_paid_order(conn, day, name, section, items, slot, "picked_up", placed, method, label)
    for name, section, items, slot, status, placed, times in LIVE_ORDERS:
        method, label = _label(mulberry32(sum(map(ord, name))), name)
        logic.insert_paid_order(conn, day, name, section, [{"id": i, "qty": q} for i, q in items], slot, status, placed, method, label, times)
    db.bump(conn)


def rush(conn, n=8):
    """Pretend n students just paid. Respects stock and the suggested (least busy) slot."""
    import random
    day = logic.today(conn)
    made = 0
    for _ in range(n):
        orderable = [m for m in logic.menu(conn) if m["orderable"]]
        if not orderable:
            break
        picked = random.sample(orderable, min(len(orderable), random.randint(1, 3)))
        items = [{"id": m["id"], "qty": min(m["left"], random.randint(1, 2))} for m in picked]
        section = random.choice(list(CFG["sections"]))
        sug = logic.suggest_slots(conn, None, items)
        if sug.get("closed") or not sug.get("best"):
            continue
        method, label = _label(random.random, "ab")
        t = logic.now_min(conn)
        logic.insert_paid_order(conn, day, random.choice(CFG["demo_names"]), section, items, sug["best"]["start"], "new", t, method, label,
                                {"paid_at": t})
        made += 1
    return made


def reset(conn):
    db.reset_db(conn, demo=True)
    seed_orders(conn)
