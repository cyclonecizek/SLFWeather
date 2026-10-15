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

from .grib import Missing, earth_relative, exists, fetch, fetch_select

MS_TO_KT = 1.943844


def _fmt(tmpl: str, cycle: int, fh: int) -> str:
    g = time.gmtime(cycle)
    return tmpl.format(ymd=time.strftime("%Y%m%d", g), hh=f"{g.tm_hour:02d}", fh=fh)


def _point(url: str, cycle: int, ctx: Context) -> dict | None:
    ck = url + "#r2"                     # r2 keeps distances for every radius option; older entries are ignored
    cached = ctx.cache.get(ck)
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
        ctx.cache.put(ck, cycle, out)
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
    opts = [str(int(r)) for r in (ctx.radius_opts or [c["precip_radius_nm"]])]
    # pd / ld: nearest radius option (nm) at which the field qualifies, 99 if none. With the max within each radius
    # this is exact for every option, and the page then judges "within R" as pd <= R.
    refl = p.get("refd") or p.get("refc")
    if refl:
        have = [(int(r), refl[r]) for r in opts if refl.get(r) is not None]
        if have:
            hit = [r for r, v in have if v >= c["precip_dbz"]]
            rec["pd"] = min(hit) if hit else 99
    # Lightning only from models with an explicit lightning field (no reflectivity proxy)
    if p.get("ltng"):
        have = [(int(r), p["ltng"][r]) for r in opts if p["ltng"].get(r) is not None]
        if have:
            hit = [r for r, v in have if v > c["ltng_threshold"]]
            rec["ld"] = min(hit) if hit else 99
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


# ---------------------------------------------------------------- NBM probabilities
_TSTM = re.compile(r":TSTM:surface:(\d+)-(\d+) hour")
_VIS = re.compile(r":VIS:surface:(\d+) hour fcst:.*prob\s*<\s*([\d.]+)", re.I)
_TCDC = re.compile(r":TCDC:(surface|entire atmosphere)[^:]*:(\d+) hour fcst")      # NBM total cloud cover (%)
_TCDC_EXTRA = re.compile(r"prob|%|std|spread|ens|ave|max|min|acc", re.I)             # not the plain deterministic field


def nbm_prob(scfg: dict, ctx: Context) -> SourceResult:
    """NBM probabilities stored as one member of 0-1 values:
      q<R>     thunderstorm probability (1, 3 and 6 h periods), highest value within R nm, for every
               radius option; every hour in a period gets that period's value and keeps the highest
               overlapping one.
      vt<k>    probability of visibility below the k-th NBM threshold (ascending), at the site.
      sky      NBM total cloud cover (%), the plain TCDC field at the site."""
    cands = [c for c in (floor_hour(ctx.now) - k * 3600 for k in range(36))
             if time.gmtime(c).tm_hour in set(scfg.get("cycles", [0, 6, 12, 18]))]
    picked = _pick([scfg["base"]], scfg["file"], cands)
    if not picked:
        return SourceResult({}, status="missing", note="no recent cycle found")
    base, cycle = picked
    maxdur = int(scfg.get("max_period_h", 6))

    def select(inv):
        out, vis, sky = [], [], []
        for rec in inv:
            d = rec[3]
            m = _TCDC.search(d)
            if m and not _TCDC_EXTRA.search(d[m.end():]):
                sky.append((0 if m.group(1) == "surface" else 1, rec))
            m = _TSTM.search(d)
            if m and "prob" in d.lower():
                a, b = int(m.group(1)), int(m.group(2))
                if 0 < b - a <= maxdur:
                    out.append((f"t{a}-{b}", rec, "area"))
            m = _VIS.search(d)
            if m:
                vis.append((float(m.group(2)), rec))
        for thr, rec in vis:                       # every threshold NBM publishes, not just the lowest
            out.append((f"vis<{thr:g}", rec, "point"))
        if sky:
            out.append(("sky", min(sky, key=lambda x: x[0])[1], "point"))
        return out

    def run(fh):
        url = f"{base}/{_fmt(scfg['file'], cycle, fh)}"
        key = url + "#nbmp3"                 # v3 keeps every visibility threshold and every radius; older entries are ignored
        got = ctx.cache.get(key)
        if got is None:
            try:
                vals, _ = fetch_select(url, select, ctx.lat, ctx.lon, ctx.radii)
            except Missing:
                return None
            except Exception as e:
                log.warning("%s f%03d: %s", scfg["id"], fh, e)
                return None
            got = {}
            for k, v in vals.items():
                if k.startswith("t") and isinstance(v, dict):
                    got[k] = {str(int(r)): x for r, x in v.items()}
                elif k.startswith("vis<") and v is not None:
                    got[k] = v
                elif k == "sky" and v is not None and 0.0 <= v <= 100.0:
                    got[k] = float(v)
            ctx.cache.put(key, cycle, got)
        return fh, got

    fhs = [fh for fh in range(1, int(scfg.get("max_fh", 192)) + 1) if ctx.in_window(cycle + fh * 3600)]
    series: dict[int, dict] = {}
    opts = [int(r) for r in (ctx.radius_opts or [30, 10])]
    with ThreadPoolExecutor(16) as ex:
        results = [res for res in ex.map(run, fhs) if res]
    thresholds = sorted({float(k[4:]) for _, got in results for k in got if k.startswith("vis<")})
    tidx = {f"{x:g}": i for i, x in enumerate(thresholds)}
    for fh, got in results:
        for name, vals in got.items():
            if name == "sky":
                t = cycle + fh * 3600
                if ctx.in_window(t):
                    series.setdefault(t, {})["sky"] = vals
                continue
            if name.startswith("vis<"):
                t = cycle + fh * 3600
                if ctx.in_window(t):
                    series.setdefault(t, {})[f"vt{tidx[name[4:]]}"] = min(1.0, max(0.0, vals / 100.0))
                continue
            a, b = map(int, name[1:].split("-"))
            for h in range(a + 1, b + 1):
                t = cycle + h * 3600
                if not ctx.in_window(t):
                    continue
                rec = series.setdefault(t, {})
                for r in opts:
                    v = vals.get(str(r))
                    if v is not None:
                        rec[f"q{r}"] = max(rec.get(f"q{r}", 0.0), min(1.0, max(0.0, v / 100.0)))
    series = {t: r for t, r in series.items() if r}
    ctx.vis_thresholds = thresholds
    if thresholds:
        ctx.vis_thr_m = thresholds[0]                       # lowest threshold: the default, shared with other visibility sources
    ctx.vis_hours = {t for t, r in series.items() if "vt0" in r}
    vis_note = f"; visibility below {', '.join(f'{x:g}' for x in thresholds)} m" if thresholds else "; no visibility probability found"
    return SourceResult({"m00": series} if series else {}, cycle=iso(cycle),
                        note="thunder probability, highest within each radius" + vis_note + ("; sky cover" if any("sky" in r for r in series.values()) else "; no sky cover found"),
                        status="ok" if series else "missing")
