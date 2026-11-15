"""Student accounts and timetables.

A student signs up once with a name, a mobile number and a password. The session cookie then keeps them signed in.
Passwords are stored as salted PBKDF2 hashes (never the password itself).

The timetable is the student's free time for each weekday: the gaps between classes when they can walk to the
canteen. Pickup slots are only offered inside those gaps. It is stored as JSON: {"0": [[750, 795]], "1": [], ...}
where "0" is Monday and a gap is [start, end] in minutes since midnight (750 = 12:30 pm).
"""
import json
import re
import secrets
from datetime import date

from werkzeug.security import check_password_hash, generate_password_hash

import db
from db import CFG, tx

ITERATIONS = 600_000          # tests lower this so they run fast
TT_MAX_PER_DAY = 8
FAILS = {}                    # login throttle: key -> (failures, first failure time)


class ApiError(Exception):
    """A problem the student or kitchen should be told about. app.py turns it into JSON; logic.py re-exports it."""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.message, self.status = message, status


def clean_phone(raw):
    d = re.sub(r"\D", "", str(raw or ""))
    if len(d) == 12 and d.startswith("91"):
        d = d[2:]
    elif len(d) == 11 and d.startswith("0"):
        d = d[1:]
    if not re.fullmatch(r"[6-9]\d{9}", d):
        raise ApiError("Enter a 10 digit mobile number, like 98765 43210.")
    return d


def clean_name(raw):
    name = " ".join(str(raw or "").split())
    if len(name) < 2:
        raise ApiError("Enter your name.")
    return name[:24]


def clean_password(raw):
    pw = str(raw or "")
    if len(pw) < 6:
        raise ApiError("Use a password with at least 6 characters.")
    if len(pw) > 100:
        raise ApiError("That password is too long.")
    return pw


# ------------------------------------------------------------------ timetable
def clean_timetable(raw):
    """Check what the app sent and return a tidy timetable: every weekday present, gaps sorted and merged."""
    if not isinstance(raw, dict):
        raise ApiError("That timetable is not valid.")
    out = {}
    for d in range(7):
        rows = raw.get(str(d), raw.get(d)) or []
        if not isinstance(rows, list) or len(rows) > TT_MAX_PER_DAY:
            raise ApiError(f"You can add up to {TT_MAX_PER_DAY} breaks a day.")
        gaps = []
        for w in rows:
            ok = isinstance(w, (list, tuple)) and len(w) == 2 and all(isinstance(x, int) and not isinstance(x, bool) for x in w)
            if not ok:
                raise ApiError("Each break needs a start and an end time.")
            s, e = w
            if not 0 <= s < e <= 1440:
                raise ApiError("A break has to end after it starts.")
            if e - s < 5:
                raise ApiError("A break has to be at least 5 minutes long.")
            gaps.append([s, e])
        gaps.sort()
        merged = []
        for s, e in gaps:
            if merged and s <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], e)
            else:
                merged.append([s, e])
        out[str(d)] = merged
    return out


def timetable_of(row):
    if not row["timetable"]:
        return None
    try:
        return json.loads(row["timetable"])
    except ValueError:
        return None


def has_timetable(tt):
    return bool(tt) and any(tt.get(str(d)) for d in range(7))


def user_json(row):
    tt = timetable_of(row)
    return {"id": row["id"], "name": row["name"], "phone": row["phone"], "timetable": tt, "tt_set": has_timetable(tt)}


# ------------------------------------------------------------------ sign up, sign in
def get_user(conn, uid):
    if not uid:
        return None
    return conn.execute("SELECT * FROM users WHERE id=?", (str(uid),)).fetchone()


def signup(conn, name, phone, password):
    name, phone, pw = clean_name(name), clean_phone(phone), clean_password(password)
    with tx(conn):
        if conn.execute("SELECT 1 FROM users WHERE phone=?", (phone,)).fetchone():
            raise ApiError("This number already has an account. Sign in instead.", 409)
        uid = "usr_" + secrets.token_hex(6)
        conn.execute("INSERT INTO users(id,name,phone,pw,timetable,created) VALUES(?,?,?,?,NULL,?)",
                     (uid, name, phone, generate_password_hash(pw, method=f"pbkdf2:sha256:{ITERATIONS}"), db.now_ts()))
    return get_user(conn, uid)


def login(conn, phone, password, who="?"):
    phone = clean_phone(phone)
    key = f"{who}|{phone}"
    n, first = FAILS.get(key, (0, db.now_ts()))
    if db.now_ts() - first > 60:
        n, first = 0, db.now_ts()
    if n >= 5:
        raise ApiError("Too many tries. Wait a minute and try again.", 429)
    row = conn.execute("SELECT * FROM users WHERE phone=?", (phone,)).fetchone()
    if row is None or not check_password_hash(row["pw"], str(password or "")):
        FAILS[key] = (n + 1, first)
        raise ApiError("That number and password do not match.", 401)
    FAILS.pop(key, None)
    return row


def save_timetable(conn, uid, raw):
    tt = clean_timetable(raw)
    with tx(conn):
        conn.execute("UPDATE users SET timetable=? WHERE id=?", (json.dumps(tt, separators=(",", ":")), uid))
    return get_user(conn, uid)


def gaps_for(user_row, day_iso):
    """The student's free gaps for one date, clipped to the hours the canteen is open. Empty if none are set."""
    tt = timetable_of(user_row) if user_row is not None else None
    if not tt:
        return []
    wd = date.fromisoformat(day_iso).weekday()
    out = []
    for s, e in tt.get(str(wd), []):
        s, e = max(s, CFG["day_start"]), min(e, CFG["day_end"])
        if e - s >= 5:
            out.append([s, e])
    return out
