"""Convection-allowing NCEP guidance: HRRR time-lagged and multi-model member sets
(HREF membership, NAM 3 km). Each member-hour becomes one record:

    spd, dir, gst  10 m wind (kt, deg true)
    rh, t          2 m RH (%) and temperature (F)
    p30            1 if 1 km reflectivity >= precip_dbz anywhere within the precip radius
    l30, l10       1 if lightning (LTNG, else a reflectivity proxy) within each radius
"""
from __future__ import annotations

import math
import time
from concurrent.futures import ThreadPoolExecutor

from .common import Context, SourceResult, floor_hour, iso, log
import re

from .grib import Missing, earth_relative, exists, fetch, fetch_area

MS_TO_KT = 1.943844


def _fmt(tmpl: str, cycle: int, fh: int) -> str:
    g = time.gmtime(cycle)
    return tmpl.format(ymd=time.strftime("%Y%m%d", g), hh=f"{g.tm_hour:02d}", fh=fh)


def _point(url: str, cycle: int, ctx: Context) -> dict | None:
    cached = ctx.cache.get(url)
    if cached is not None:
        return cached
    try:
        vals, meta = fetch(url, ctx.lat, ctx.lon, ctx.radii)
    except Missing:
        return None
    out = {}
    if vals.get("u10") is not None and vals.get("v10") is not None:
        uv = earth_relative(vals["u10"], vals["v10"], meta.get("u10"), ctx.lon)
        if uv:
            out["u"], out["v"] = uv[0] * MS_TO_KT, uv[1] * MS_TO_KT
    if vals.get("g0") is not None:
        out["g"] = vals["g0"] * MS_TO_KT
    if vals.get("rh") is not None:
        out["rh"] = vals["rh"]
    if vals.get("t") is not None:
        out["t"] = (vals["t"] - 273.15) * 9 / 5 + 32
    for k in ("refd", "refc", "ltng"):
        if isinstance(vals.get(k), dict):
            out[k] = {str(int(r)): v for r, v in vals[k].items()}
    if out:
        ctx.cache.put(url, cycle, out)
    return out


def record(p: dict, ctx: Context) -> dict | None:
    if not p:
        return None
    c = ctx.cons
    rec = {}
    if "u" in p and "v" in p:
        u, v = p["u"], p["v"]
        rec["spd"] = math.hypot(u, v)
        rec["dir"] = (math.degrees(math.atan2(-u, -v)) + 360) % 360
        rec["gst"] = max(p.get("g", rec["spd"]), rec["spd"])
    if "rh" in p:
        rec["rh"] = p["rh"]
    if "t" in p:
        rec["t"] = p["t"]
    rp = str(int(c["precip_radius_nm"]))
    refl = p.get("refd") or p.get("refc")
    if refl and refl.get(rp) is not None:
        rec["p30"] = int(refl[rp] >= c["precip_dbz"])
    # Lightning only from models with an explicit lightning field (no reflectivity proxy)
    r30, r10 = (str(int(r)) for r in c["lightning_radii_nm"])
    if p.get("ltng"):
        for key, r in (("l30", r30), ("l10", r10)):
            if p["ltng"].get(r) is not None:
                rec[key] = int(p["ltng"][r] > c["ltng_threshold"])
    return rec or None


def _pick(bases, tmpl, cycles):
    for base in bases:
        for c in cycles:
            if exists(f"{base}/{_fmt(tmpl, c, 1)}.idx"):
                return base, c
    return None


def _run_tasks(scfg, ctx, tasks, workers):
    """tasks: [(member_id, url, cycle, valid)] -> {member_id: {valid: record}}"""
    def run(task):
        mid, url, c, valid = task
        try:
            return task, record(_point(url, c, ctx), ctx)
        except Exception as e:
            log.warning("%s %s: %s", scfg["id"], url.rsplit("/", 1)[-1], e)
            return task, None

    members: dict[str, dict] = {}
    with ThreadPoolExecutor(workers) as ex:
        for (mid, _, _, valid), rec in ex.map(run, tasks):
            if rec:
                members.setdefault(mid, {})[valid] = rec
    return members


def tle(scfg: dict, ctx: Context) -> SourceResult:
    """Time-lagged ensemble of one hourly-cycling model (HRRR)."""
    recent = [floor_hour(ctx.now) - k * 3600 for k in range(10)]
    picked = _pick([scfg["base"]], scfg["file"], recent)
    if not picked:
        return SourceResult({}, status="missing", note="no recent cycle found")
    base, latest = picked
    cycles = [latest - k * 3600 for k in range(int(scfg.get("lag_cycles", 6)))]
    cycles += [c for c in (latest - k * 3600 for k in range(30))
               if time.gmtime(c).tm_hour % 6 == 0 and c not in cycles][: int(scfg.get("synoptic_extra", 2))]
    tasks = []
    for c in cycles:
        mx = scfg.get("long_fh", 48) if time.gmtime(c).tm_hour % 6 == 0 else scfg.get("short_fh", 18)
        for fh in range(0, mx + 1):
            if ctx.in_window(c + fh * 3600):
                tasks.append((time.strftime("%d/%HZ", time.gmtime(c)), f"{base}/{_fmt(scfg['file'], c, fh)}", c, c + fh * 3600))
    members = _run_tasks(scfg, ctx, tasks, 16)
    return SourceResult(members, cycle=iso(latest), note=f"{len(members)}/{len(cycles)} cycles",
                        status="ok" if len(members) == len(cycles) else ("partial" if members else "missing"))


def multi_model(scfg: dict, ctx: Context) -> SourceResult:
    tasks, notes, missing = [], [], []
    for comp in scfg["components"]:
        hours = set(comp.get("cycles", [0, 6, 12, 18]))
        cands = [c for c in (floor_hour(ctx.now) - k * 3600 for k in range(60)) if time.gmtime(c).tm_hour in hours]
        picked = _pick(comp.get("bases") or [comp["base"]], comp["file"], cands)
        if not picked:
            missing.append(comp["id"])
            continue
        base, latest = picked
        use = [c for c in cands if c <= latest][: int(comp.get("lag", 2))]
        notes.append(f"{comp['id']} " + ", ".join(time.strftime("%HZ", time.gmtime(c)) for c in use))
        for c in use:
            for fh in range(0, int(comp.get("max_fh", 48)) + 1):
                if ctx.in_window(c + fh * 3600):
                    tasks.append((f"{comp['id']} {time.strftime('%d/%HZ', time.gmtime(c))}",
                                  f"{base}/{_fmt(comp['file'], c, fh)}", c, c + fh * 3600))
    members = _run_tasks(scfg, ctx, tasks, int(scfg.get("workers", 4)))
    note = "; ".join(notes + ([f"missing: {', '.join(missing)}"] if missing else []))
    status = "missing" if not members else ("partial" if missing else "ok")
    return SourceResult(members, note=note, status=status)


# ---------------------------------------------------------------- NBM thunder probability
_TSTM = re.compile(r":TSTM:surface:(\d+)-(\d+) hour")


def nbm_prob(scfg: dict, ctx: Context) -> SourceResult:
    """NBM thunderstorm probabilities (1, 3 and 6 h periods), highest value within each
    lightning radius. Stored as one member whose l30/l10 are probabilities (0-1); every hour
    in a period gets that period's value, and each hour keeps the highest overlapping one."""
    cands = [c for c in (floor_hour(ctx.now) - k * 3600 for k in range(36))
             if time.gmtime(c).tm_hour in set(scfg.get("cycles", [0, 6, 12, 18]))]
    picked = _pick([scfg["base"]], scfg["file"], cands)
    if not picked:
        return SourceResult({}, status="missing", note="no recent cycle found")
    base, cycle = picked
    maxdur = int(scfg.get("max_period_h", 6))

    def pick(desc):
        m = _TSTM.search(desc)
        if not m or "prob" not in desc.lower():
            return None
        a, b = int(m.group(1)), int(m.group(2))
        return f"{a}-{b}" if 0 < b - a <= maxdur else None

    def run(fh):
        url = f"{base}/{_fmt(scfg['file'], cycle, fh)}"
        key = url + "#tstm"
        got = ctx.cache.get(key)
        if got is None:
            try:
                got = {k: {str(int(r)): v for r, v in d.items()} for k, d in
                       fetch_area(url, pick, ctx.lat, ctx.lon, ctx.radii).items() if isinstance(d, dict)}
            except Missing:
                return None
            except Exception as e:
                log.warning("%s f%03d: %s", scfg["id"], fh, e)
                return None
            ctx.cache.put(key, cycle, got)
        return got

    fhs = [fh for fh in range(1, int(scfg.get("max_fh", 192)) + 1) if ctx.in_window(cycle + fh * 3600)]
    series: dict[int, dict] = {}
    r30, r10 = (str(int(r)) for r in ctx.cons["lightning_radii_nm"])
    with ThreadPoolExecutor(16) as ex:
        for got in ex.map(run, fhs):
            for period, vals in (got or {}).items():
                a, b = map(int, period.split("-"))
                for h in range(a + 1, b + 1):
                    t = cycle + h * 3600
                    if not ctx.in_window(t):
                        continue
                    rec = series.setdefault(t, {})
                    for key, r in (("l30", r30), ("l10", r10)):
                        v = vals.get(r)
                        if v is not None:
                            rec[key] = max(rec.get(key, 0.0), min(1.0, max(0.0, v / 100.0)))
    series = {t: r for t, r in series.items() if r}
    return SourceResult({"m00": series} if series else {}, cycle=iso(cycle),
                        note="thunder probability, highest within each radius",
                        status="ok" if series else "missing")
