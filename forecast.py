"""Demand forecast: how many of each item will sell in each hour today.

Plain-language method (no machine-learning library needed):
  1. Look at the last 6 weeks of sales for each item and hour.
  2. Learn the weekday effect (Fridays are busier, Saturdays quieter) and remove it.
  3. Smooth the last 14 days with an exponential moving average, so recent days count more.
  4. Put today's weekday effect back.
The spread of the last 14 days gives the "plus or minus" shown to the kitchen.
"""
import math
from datetime import date

from db import CFG, HOURS

ALPHA = 0.3


def _wd(day):
    return date.fromisoformat(day).weekday()


def load_series(conn):
    """{(item_id, hour): [(day, qty), ...]} oldest first."""
    series = {}
    for r in conn.execute("SELECT day,item_id,hour,qty FROM history ORDER BY day"):
        series.setdefault((r["item_id"], r["hour"]), []).append((r["day"], r["qty"]))
    return series


def weekday_factors(series):
    sums = [0.0] * 7
    cnt = [0] * 7
    for ser in series.values():
        m = sum(q for _, q in ser) / len(ser)
        if m <= 0:
            continue
        for d, q in ser:
            w = _wd(d)
            sums[w] += q / m
            cnt[w] += 1
    return [sums[i] / cnt[i] if cnt[i] else 1.0 for i in range(7)]


class Model:
    def __init__(self, conn, day):
        series = load_series(conn)
        self.wdf = weekday_factors(series)
        self.wd = _wd(day)
        f = self.wdf[self.wd]
        self.mean, self.sd, self.base = {}, {}, {}
        for key, ser in series.items():
            norm = [q / self.wdf[_wd(d)] if self.wdf[_wd(d)] > 0.05 else q for d, q in ser]
            recent = norm[-14:]
            m = recent[0]
            for v in recent[1:]:
                m = ALPHA * v + (1 - ALPHA) * m
            avg = sum(recent) / len(recent)
            s = math.sqrt(sum((v - avg) ** 2 for v in recent) / len(recent))
            self.mean[key] = m * f
            self.sd[key] = s * f
            same = [q for d, q in ser if _wd(d) == self.wd][-4:]
            self.base[key] = sum(same) / len(same) if same else 0.0

    def item_hour(self, item, hour):
        return self.mean.get((item, hour), 0.0)

    def day_total(self, item):
        return sum(self.item_hour(item, h) for h in HOURS)

    def window(self, item, h0, h1):
        """Predicted items for hours h0 <= h < h1."""
        return sum(self.item_hour(item, h) for h in HOURS if h0 <= h < h1)

    def window_base(self, item, h0, h1):
        return sum(self.base.get((item, h), 0.0) for h in HOURS if h0 <= h < h1)

    def day_sd(self, item):
        return math.sqrt(sum(self.sd.get((item, h), 0.0) ** 2 for h in HOURS))
