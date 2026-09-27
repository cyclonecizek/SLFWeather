"""Turn member-hour records into 7-Day window probabilities and the Excel CSV.

The dashboard runs the same logic in JavaScript (docs/index.html, function
windowTable) so a CSV downloaded from the page matches the published one when
all sources are on. Keep the two in step.

For each window and constraint:
  a member "hits" if any hour it has in the window exceeds the limit;
  a member counts only if it covers >= min_window_coverage of the window;
  source probability = hits / counted members;
  window probability = weight-averaged over sources that have counted members,
  rounded to the nearest 5 %.
Low = weighted median of members' minimum 2 m temperature in the 00-11L window;
High = weighted median of members' maximum in the 11-18L window.
"""
from __future__ import annotations

import csv
import io
import math
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

PROB_COLS = [("precip30", "p30"), ("ltng30", "l30"), ("ltng10", "l10"),
             ("xwind", "xw"), ("headtail", "ht"), ("rh98", "rhx")]
HEADER = ["key", "local_date", "window", "window_label", "local_start", "precip30", "ltng30", "ltng10",
          "xwind", "headtail", "vis200", "rh98", "low_f", "high_f", "sources", "generated_utc"]


def window_list(now: int, tz: str, spans, days: int):
    """[(key, date_str, win_no, label, start_unix, end_unix)] from yesterday (local) onward."""
    z = ZoneInfo(tz)
    d0 = datetime.fromtimestamp(now, z).date() - timedelta(days=1)
    out = []
    for d in range(days + 1):
        day = d0 + timedelta(days=d)
        for w, (h0, h1) in enumerate(spans, 1):
            a = datetime(day.year, day.month, day.day, tzinfo=z) + timedelta(hours=h0)
            b = datetime(day.year, day.month, day.day, tzinfo=z) + timedelta(hours=h1)
            key = int(f"{a:%Y%m%d%H}")
            out.append((key, f"{day:%Y-%m-%d}", w, f"{h0:02d}-{h1:02d}L", int(a.timestamp()), int(b.timestamp())))
    return out


def exceed(var: str, rec_vals: dict, i: int, cons: dict, rwy: float):
    """1/0 if the member-hour can be judged for this constraint, else None."""
    if var == "p30":
        v = rec_vals.get(var)
        return None if v is None or v[i] is None else int(v[i] >= 1)
    if var in ("l30", "l10"):                       # 0/1 for lightning fields, 0-1 for NBM probability
        v = rec_vals.get(var)
        return None if v is None or v[i] is None else float(v[i])
    if var == "rhx":
        v = rec_vals.get("rh")
        return None if v is None or v[i] is None else int(v[i] > cons["rh_pct"])
    g, d = rec_vals.get("gst"), rec_vals.get("dir")
    if g is None or d is None or g[i] is None or d[i] is None:
        return None
    a = math.radians(d[i] - rwy)
    if var == "xw":
        return int(g[i] * abs(math.sin(a)) > cons["crosswind_kt"])
    return int(abs(g[i] * math.cos(a)) > cons["headtail_kt"])


def _step_hours(times, arr):
    idx = [t for t, v in zip(times, arr) if v is not None]
    if len(idx) < 2:
        return 1.0
    gaps = sorted((b - a) / 3600 for a, b in zip(idx, idx[1:]))
    return min(6.0, max(1.0, gaps[len(gaps) // 2]))


def _wmedian(pairs):
    if not pairs:
        return None
    pairs.sort()
    tot = sum(w for _, w in pairs)
    c = 0.0
    for v, w in pairs:
        c += w
        if c >= tot / 2 - 1e-9:
            return v
    return pairs[-1][0]


def table(data: dict, cfg: dict, enabled: set | None = None) -> list[dict]:
    cons = cfg["constraints"]
    rwy = float(cfg["runway_heading_true"])
    cov = float(cfg.get("min_window_coverage", 0.5))
    times = data["times"]
    tindex = {t: i for i, t in enumerate(times)}
    wins = window_list(data["generated_unix"], cfg["site"]["display_tz"], cfg["windows"], int(cfg["days"]))
    srcs = [s for s in data["sources"] if s["members"] and (enabled is None or s["id"] in enabled)]
    steps = {}
    for s in srcs:
        for m in s["members"]:
            for var in ("gst", "rh", "p30", "l30", "l10", "t"):
                arr = m["v"].get(var)
                steps[(s["id"], m["id"], var)] = _step_hours(times, arr) if arr else 1.0

    rows = []
    for key, date, w, label, a, b in wins:
        hours = [tindex[t] for t in range(a, b, 3600) if t in tindex]
        nh = (b - a) / 3600
        row = {"key": key, "local_date": date, "window": w, "window_label": label, "local_start": a}
        used = set()
        for col, var in PROB_COLS:
            base = "gst" if var in ("xw", "ht") else ("rh" if var == "rhx" else var)
            num = den = 0.0
            for s in srcs:
                hit = n = 0
                for m in s["members"]:
                    vals = [exceed(var, m["v"], i, cons, rwy) for i in hours]
                    vals = [v for v in vals if v is not None]
                    need = max(1, math.floor(cov * nh / steps[(s["id"], m["id"], base)]))
                    if len(vals) >= need:
                        n += 1
                        hit += max(vals)
                if n:
                    num += s["weight"] * hit / n
                    den += s["weight"]
                    used.add(s["id"])
            row[col] = int(math.floor(100 * num / den / 5 + 0.5) * 5) if den else None   # half-up, same as JS
        for col, want, fn in (("low_f", 1, min), ("high_f", 2, max)):
            row[col] = None
            if w != want:
                continue
            pairs = []
            for s in srcs:
                vals_m = []
                for m in s["members"]:
                    t = m["v"].get("t")
                    if not t:
                        continue
                    v = [t[i] for i in hours if t[i] is not None]
                    if len(v) >= max(1, math.floor(cov * nh / steps[(s["id"], m["id"], "t")])):
                        vals_m.append(fn(v))
                pairs += [(v, s["weight"] / len(vals_m)) for v in vals_m]
            med = _wmedian(pairs)
            row[col] = None if med is None else int(math.floor(med + 0.5))
        row["vis200"] = None
        row["sources"] = len(used)
        rows.append(row)
    return rows


def to_csv(rows: list[dict], generated: str, tz: str) -> str:
    z = ZoneInfo(tz)
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerow(HEADER)
    for r in rows:
        w.writerow([r["key"], r["local_date"], r["window"], r["window_label"],
                    datetime.fromtimestamp(r["local_start"], z).strftime("%Y-%m-%d %H:%M"),
                    *["" if r[c] is None else r[c] for c in
                      ("precip30", "ltng30", "ltng10", "xwind", "headtail", "vis200", "rh98", "low_f", "high_f")],
                    r["sources"], generated])
    return buf.getvalue()
