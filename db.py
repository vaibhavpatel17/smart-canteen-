"""Database layer: SQLite connection, schema and first-run seed data.

Everything the canteen remembers lives in one file (instance/canteen.db).
Delete that file to start fresh.

Times: order times (slot, placed, paid_at ...) are "minutes since midnight" as floats,
for example 12:50 pm is 770. They always mean Indian Standard Time (IST, UTC+05:30), whatever the
timezone of the computer running the server. Real clock time is also used for payment holds.
"""
import json
import os
import sqlite3
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(BASE_DIR, "config.json"), encoding="utf-8") as _f:
    CFG = json.load(_f)
DB_PATH = os.environ.get("CANTEEN_DB", os.path.join(BASE_DIR, "instance", "canteen.db"))
HOURS = CFG["hours"]


IST = timezone(timedelta(hours=5, minutes=30), "IST")   # India has no daylight saving, so a fixed offset is exact


def now_ts():
    """Real wall-clock seconds. Used for payment holds and the live clock. Tests replace this."""
    return time.time()


def ist_now():
    return datetime.fromtimestamp(now_ts(), IST)


def ist_today():
    """Today's date in India, as YYYY-MM-DD."""
    return ist_now().date().isoformat()


def ist_minutes():
    """Minutes since midnight in India right now, with seconds as a fraction."""
    n = ist_now()
    return n.hour * 60 + n.minute + n.second / 60


SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS items (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, category TEXT NOT NULL, price INTEGER NOT NULL,
  prep INTEGER NOT NULL, art TEXT NOT NULL, tint TEXT NOT NULL,
  available INTEGER NOT NULL DEFAULT 1, stock INTEGER NOT NULL DEFAULT 0, sort INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS history (
  day TEXT NOT NULL, item_id TEXT NOT NULL, hour INTEGER NOT NULL, qty INTEGER NOT NULL,
  PRIMARY KEY (day, item_id, hour)
);
CREATE TABLE IF NOT EXISTS users (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, phone TEXT NOT NULL UNIQUE, pw TEXT NOT NULL,
  timetable TEXT, created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS orders (
  id TEXT PRIMARY KEY, day TEXT NOT NULL, token_no INTEGER NOT NULL, pin TEXT NOT NULL, secret TEXT NOT NULL,
  name TEXT NOT NULL, section TEXT NOT NULL, slot INTEGER NOT NULL, status TEXT NOT NULL, total INTEGER NOT NULL,
  hold_until REAL, placed REAL, paid_at REAL, prep_at REAL, ready_at REAL, picked_at REAL, cancelled_at REAL,
  cancel_reason TEXT, user_id TEXT
);
CREATE INDEX IF NOT EXISTS orders_day_status ON orders (day, status);
CREATE UNIQUE INDEX IF NOT EXISTS orders_day_token ON orders (day, token_no);
CREATE TABLE IF NOT EXISTS order_items (
  order_id TEXT NOT NULL, item_id TEXT NOT NULL, name TEXT NOT NULL, price INTEGER NOT NULL, qty INTEGER NOT NULL,
  PRIMARY KEY (order_id, item_id)
);
CREATE TABLE IF NOT EXISTS payments (
  id TEXT PRIMARY KEY, order_id TEXT NOT NULL, status TEXT NOT NULL, method TEXT, label TEXT, last4 TEXT,
  amount INTEGER NOT NULL, gateway TEXT NOT NULL, ref TEXT, rp_order_id TEXT, error TEXT,
  attempts INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL, captured_at REAL, refunded_at REAL, refund_ref TEXT
);
CREATE INDEX IF NOT EXISTS payments_order ON payments (order_id);
CREATE UNIQUE INDEX IF NOT EXISTS payments_upi_ref ON payments (ref) WHERE gateway='upi' AND ref IS NOT NULL;
"""


def migrate(conn):
    """Bring a database made by an older version up to date (adds the columns that were added later)."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(orders)")}
    if "user_id" not in cols:
        conn.execute("ALTER TABLE orders ADD COLUMN user_id TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS orders_user ON orders (user_id, day)")


# ---------------------------------------------------------------- random numbers
def mulberry32(seed):
    """Tiny seeded random generator. The browser preview uses the same one, so both show the same history."""
    M = 0xFFFFFFFF
    state = [seed & M]

    def rnd():
        a = (state[0] + 0x6D2B79F5) & M
        state[0] = a
        t = ((a ^ (a >> 15)) * (1 | a)) & M
        t = ((t + (((t ^ (t >> 7)) * (61 | t)) & M)) & M) ^ t
        return ((t ^ (t >> 14)) & M) / 4294967296

    return rnd


def half_up(x):
    return int(x + 0.5) if x >= 0 else -int(-x + 0.5)


# ---------------------------------------------------------------- connection
def connect(path=None):
    path = path or DB_PATH
    if path != ":memory:":
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = sqlite3.connect(path, timeout=10, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=8000")
    return conn


@contextmanager
def tx(conn):
    """One all-or-nothing change. BEGIN IMMEDIATE stops two students grabbing the last samosa together."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def meta_get(conn, key, default=None):
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def meta_set(conn, key, value):
    conn.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))


def bump(conn):
    """Every change bumps a version number. Screens poll it to learn that something moved."""
    meta_set(conn, "rev", int(meta_get(conn, "rev", "0")) + 1)


# ---------------------------------------------------------------- seeding
def share(category, hour):
    shape = CFG["shapes"][CFG["cat_shape"][category]]
    return shape[HOURS.index(hour)] / sum(shape)


def init_db(conn, demo=True, today=None):
    """Create tables and, on first run, the menu plus six weeks of made-up sales history."""
    conn.executescript(SCHEMA)
    migrate(conn)
    if meta_get(conn, "seeded") is not None:
        return False
    day = today or ist_today()
    with tx(conn):
        for i, m in enumerate(CFG["menu"]):
            conn.execute(
                "INSERT INTO items(id,name,category,price,prep,art,tint,available,stock,sort) VALUES(?,?,?,?,?,?,?,1,0,?)",
                (m["id"], m["name"], m["category"], m["price"], m["prep"], m["art"], m["tint"], i))
        meta_set(conn, "day", day)
        meta_set(conn, "rev", 1)
        meta_set(conn, "accepting", 1)
        meta_set(conn, "capacity", CFG["default_capacity"])
        meta_set(conn, "next_token", 1001)
        meta_set(conn, "clock_mode", "demo" if demo else "real")
        meta_set(conn, "clock_base", CFG["demo_start"])
        meta_set(conn, "clock_t0", now_ts())
        meta_set(conn, "clock_running", 1)
    seed_history(conn, day)
    from logic import apply_forecast_stock  # local import: logic imports this module
    apply_forecast_stock(conn)
    meta_set(conn, "seeded", 1)
    return True


def seed_history(conn, day):
    """Six weeks of made-up sales so the forecast has something to learn from on day one.
    In real use this table fills itself: `python app.py rollup` adds each finished day."""
    rnd = mulberry32(20261008)
    today = date.fromisoformat(day)
    n = CFG["history_days"]
    with tx(conn):
        conn.execute("DELETE FROM history")
        for back in range(n, 0, -1):
            d = today - timedelta(days=back)
            f = CFG["weekday_factor"][d.weekday()]
            if f == 0:
                continue
            trend = 1 + 0.12 * (n - back) / n
            for m in CFG["menu"]:
                for h in HOURS:
                    mu = m["daily"] * share(m["category"], h) * f * trend
                    q = max(0, half_up(mu * (0.8 + rnd() * 0.4)))
                    conn.execute("INSERT INTO history VALUES(?,?,?,?)", (d.isoformat(), m["id"], h, q))


def reset_db(conn, demo=True, today=None, keep_users=True):
    """Start the canteen over. Student accounts are kept unless keep_users is False."""
    conn.execute("PRAGMA foreign_keys=OFF")
    tables = ["meta", "items", "history", "orders", "order_items", "payments"] + ([] if keep_users else ["users"])
    for t in tables:
        conn.execute(f"DROP TABLE IF EXISTS {t}")
    return init_db(conn, demo=demo, today=today)
