"""Wind CSVs in the same layout as the GSL 1D-viewer NBM point file.

The forecast workbook finds its columns by header name, so either file can be
loaded through the workbook's normal NBM import with no workbook changes.

  <name>_NBM_wind.csv  NBM alone (Open-Meteo NBM CONUS), a straight stand-in for GSL.
  <name>_ENS_wind.csv  every source on the board, blended with the board's source
                       weights (each source's weight split across its members):
                       WIND / GUST = weighted median, WDIR = direction of the weighted
                       mean wind vector, percentile columns = weighted percentiles
                       across all members.

Units match GSL: wind and gust in m/s, RH in %, temperature in K. ValidTime is
yyyymmddhh UTC. Rows start at the current hour. Each member's own gaps of up to
6 hours are filled by linear interpolation (u/v for direction) so 3- or 6-hourly
sources don't make the blend jump from hour to hour.
"""
from __future__ import annotations

import csv
import io
import math
import time

KT_PER_MS = 1.943844
LEVELS = list(range(5, 100, 5))
MAX_GAP_H = 6
W = "WIND_10 m above ground"
G = "GUST_10 m above ground"
D = "WDIR_10 m above ground"
RH = "RH_2 m above ground"
T = "TMP_2 m above ground"


def _interp(times, arr):
    """Fill None gaps of <= MAX_GAP_H hours between known values (linear in time)."""
    if not arr:
        return None
    out = list(arr)
    known = [i for i, x in enumerate(arr) if x is not None]
    for a, b in zip(known, known[1:]):
        gap = (times[b] - times[a]) / 3600
        if 1 < gap <= MAX_GAP_H:
            for i in range(a + 1, b):
                w = (times[i] - times[a]) / (times[b] - times[a])
                out[i] = arr[a] + (arr[b] - arr[a]) * w
    return out


def _member(times, v):
    """Per-member hourly arrays (knots/deg/%/F) with small gaps filled."""
    spd, gst, drc = v.get("spd"), v.get("gst"), v.get("dir")
    if not gst or not drc:
        return None
    n = len(times)
    if not spd:
        spd = [None] * n
    u = [None] * n
    vv = [None] * n
    for i in range(n):
        s = spd[i] if spd[i] is not None else gst[i]
        if s is not None and drc[i] is not None:
            r = math.radians(drc[i])
            u[i], vv[i] = -s * math.sin(r), -s * math.cos(r)
    return {"spd": _interp(times, spd), "gst": _interp(times, gst), "u": _interp(times, u),
            "v": _interp(times, vv), "rh": _interp(times, v.get("rh")), "t": _interp(times, v.get("t"))}


def _dir(u, v):
    if u is None or v is None or math.hypot(u, v) < 1e-6:
        return None
    return (math.degrees(math.atan2(-u, -v)) + 360.0) % 360.0


def wpercentile(pairs, p):
    """Weighted percentile (0-100) of [(value, weight)], linear between weight midpoints."""
    pairs = sorted((x, w) for x, w in pairs if x is not None and w > 0)
    if not pairs:
        return None
    if len(pairs) == 1:
        return pairs[0][0]
    tot = sum(w for _, w in pairs)
    c, mids = 0.0, []
    for x, w in pairs:
        mids.append((c + w / 2) / tot)
        c += w
    q = p / 100.0
    if q <= mids[0]:
        return pairs[0][0]
    if q >= mids[-1]:
        return pairs[-1][0]
    for k in range(1, len(mids)):
        if q <= mids[k]:
            f = (q - mids[k - 1]) / (mids[k] - mids[k - 1])
            return pairs[k - 1][0] + f * (pairs[k][0] - pairs[k - 1][0])
    return pairs[-1][0]


def _ms(kt):
    return None if kt is None else round(kt / KT_PER_MS, 2)


def _k(f):
    return None if f is None else round((f - 32) * 5 / 9 + 273.15, 2)


def _vt(t):
    return time.strftime("%Y%m%d%H", time.gmtime(t))


def _write(header, rows):
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerow(header)
    for r in rows:
        w.writerow(["" if x is None else (round(x, 2) if isinstance(x, float) else x) for x in r])
    return buf.getvalue()


def _start_index(times, now):
    t0 = int(now // 3600 * 3600)
    return next((i for i, t in enumerate(times) if t >= t0), len(times))


def nbm_csv(data: dict, source_id: str = "nbm") -> str | None:
    """NBM alone. Returns None when the NBM source has no wind members."""
    times = data["times"]
    src = next((s for s in data["sources"] if s["id"] == source_id and s["members"]), None)
    if not src:
        return None
    m = _member(times, src["members"][0]["v"])
    if not m:
        return None
    rows = []
    for i in range(_start_index(times, data["generated_unix"]), len(times)):
        d = _dir(m["u"][i], m["v"][i])
        s = m["spd"][i] if m["spd"][i] is not None else None
        g = m["gst"][i]
        if g is None or d is None:
            continue
        if s is None:
            s = math.hypot(m["u"][i], m["v"][i])
        rows.append([_vt(times[i]), round(d), _ms(s), _ms(g),
                     None if not m["rh"] or m["rh"][i] is None else round(m["rh"][i], 1),
                     None if not m["t"] else _k(m["t"][i])])
    return _write(["ValidTime", D, W, G, RH, T], rows)


def ens_csv(data: dict) -> str | None:
    """All sources blended with the board's weights."""
    times = data["times"]
    members = []                                   # (source_id, weight, arrays)
    for s in data["sources"]:
        for mm in s["members"]:
            arr = _member(times, mm["v"])
            if arr:
                members.append((s["id"], float(s["weight"]), arr))
    if not members:
        return None
    header = (["ValidTime", D, W, G] + [f"{G}_{p}% level" for p in LEVELS] + [f"{W}_{p}% level" for p in LEVELS]
              + [RH] + [f"{RH}_{p}% level" for p in LEVELS] + [T, "SOURCES", "MEMBERS"])
    rows = []
    for i in range(_start_index(times, data["generated_unix"]), len(times)):
        present = [(sid, w, a) for sid, w, a in members if a["gst"][i] is not None and a["u"][i] is not None]
        if not present:
            continue
        count = {}
        for sid, _, _ in present:
            count[sid] = count.get(sid, 0) + 1
        wt = [(w / count[sid], a) for sid, w, a in present]
        gst = [(a["gst"][i], w) for w, a in wt]
        spd = [(a["spd"][i] if a["spd"][i] is not None else math.hypot(a["u"][i], a["v"][i]), w) for w, a in wt]
        tot = sum(w for w, _ in wt)
        d = _dir(sum(w * a["u"][i] for w, a in wt) / tot, sum(w * a["v"][i] for w, a in wt) / tot)
        if d is None:                              # perfectly cancelling vectors: fall back to the median member
            d = _dir(wpercentile([(a["u"][i], w) for w, a in wt], 50), wpercentile([(a["v"][i], w) for w, a in wt], 50)) or 0.0
        rhp = [(a["rh"][i], w) for w, a in wt if a["rh"] and a["rh"][i] is not None]
        tp = [(a["t"][i], w) for w, a in wt if a["t"] and a["t"][i] is not None]
        row = [_vt(times[i]), round(d), _ms(wpercentile(spd, 50)), _ms(wpercentile(gst, 50))]
        row += [_ms(wpercentile(gst, p)) for p in LEVELS]
        row += [_ms(wpercentile(spd, p)) for p in LEVELS]
        row += [None if not rhp else round(wpercentile(rhp, 50), 1)]
        row += [None if not rhp else round(min(100.0, wpercentile(rhp, p)), 1) for p in LEVELS]
        row += [None if not tp else _k(wpercentile(tp, 50)), len(count), len(present)]
        rows.append(row)
    return _write(header, rows)
