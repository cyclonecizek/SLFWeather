"""Open-Meteo (global ensembles, NBM) sampled at a ring of points, and NWS gridpoints (NDFD).

Open-Meteo is asked for the site plus two rings of points (10 nm and 25 nm) so
precip can be judged "within" the radius, not just at the site. These models have
no explicit lightning parameter, so they don't contribute to the lightning rows.
"""
from __future__ import annotations

import json
import math
import os
import re
import time
from datetime import datetime, timedelta

from .common import SESSION, Context, SourceResult, log

ENS_URL = "https://ensemble-api.open-meteo.com/v1/ensemble"
DET_URL = "https://api.open-meteo.com/v1/forecast"
VARS = ["wind_speed_10m", "wind_direction_10m", "wind_gusts_10m", "relative_humidity_2m",
        "temperature_2m", "precipitation"]
_KEY = re.compile(r"^(" + "|".join(VARS) + r")(?:_member(\d+))?$")


def ring_points(lat, lon):
    """[(lat, lon, dist_nm)]: the site, 6 points at 10 nm, 6 at 25 nm."""
    pts = [(lat, lon, 0.0)]
    for dist, start in ((10, 0), (25, 30)):
        for k in range(6):
            b = math.radians(start + 60 * k)
            dlat = dist / 60.0 * math.cos(b)
            dlon = dist / 60.0 * math.sin(b) / math.cos(math.radians(lat))
            pts.append((round(lat + dlat, 4), round(lon + dlon, 4), float(dist)))
    return pts


def _params(model, ctx, pts, variables):
    days = max(1, math.ceil((ctx.t_end - ctx.now) / 86400) + 1)
    return {
        "latitude": ",".join(str(p[0]) for p in pts), "longitude": ",".join(str(p[1]) for p in pts),
        "models": model, "hourly": ",".join(variables),
        "wind_speed_unit": "kn", "temperature_unit": "fahrenheit", "precipitation_unit": "inch",
        "timeformat": "unixtime", "timezone": "GMT", "past_days": 1, "forecast_days": min(days, 16),
    }


def _fetch(url, model, ctx, pts):
    variables = list(VARS)
    for drop in (None, "wind_gusts_10m"):
        if drop:
            variables.remove(drop)
        r = SESSION.get(url, params=_params(model, ctx, pts, variables), timeout=120)
        if r.status_code != 400:
            break
        log.info("%s: %s rejected, retrying without gusts", model, r.text[:100])
    r.raise_for_status()
    js = r.json()
    return js if isinstance(js, list) else [js]


def _members(locs, pts, ctx):
    c = ctx.cons
    near30 = [i for i, p in enumerate(pts) if p[2] <= c["precip_radius_nm"]]
    times = locs[0]["hourly"]["time"]
    per: dict[str, dict] = {}                  # member -> var -> [loc arrays]
    for li, loc in enumerate(locs):
        for key, arr in loc["hourly"].items():
            m = _KEY.match(key)
            if not m:
                continue
            mid = f"m{int(m.group(2)):02d}" if m.group(2) else "m00"
            per.setdefault(mid, {}).setdefault(m.group(1), [None] * len(locs))[li] = arr

    def at(fields, var, li, i):
        a = fields.get(var)
        if not a or a[li] is None:
            return None
        return a[li][i]

    out = {}
    for mid, f in per.items():
        series = {}
        for i, t in enumerate(times):
            if not ctx.in_window(t):
                continue
            rec = {}
            s, d, g = at(f, "wind_speed_10m", 0, i), at(f, "wind_direction_10m", 0, i), at(f, "wind_gusts_10m", 0, i)
            if s is not None and d is not None:
                rec["spd"], rec["dir"] = s, d
                rec["gst"] = max(g, s) if g is not None else s
            if at(f, "relative_humidity_2m", 0, i) is not None:
                rec["rh"] = at(f, "relative_humidity_2m", 0, i)
            if at(f, "temperature_2m", 0, i) is not None:
                rec["t"] = at(f, "temperature_2m", 0, i)
            pr = [at(f, "precipitation", li, i) for li in range(len(pts))]
            if any(pr[li] is not None for li in near30):
                rec["p30"] = int(any(pr[li] is not None and pr[li] >= c["precip_in"] for li in near30))
            # no lightning here: these models have no explicit lightning parameter
            if rec:
                series[int(t)] = rec
        if series:
            out[mid] = series
    return out


def _cached(scfg, ctx, url):
    """Re-use the last Open-Meteo pull for a few hours to stay inside the free call limit."""
    path = os.path.join(ctx.root, "cache", f"om_{scfg['id']}.json")
    try:
        with open(path) as f:
            old = json.load(f)
        if ctx.now - old["fetched"] < ctx.om_refresh_h * 3600:
            mem = {m: {int(t): r for t, r in s.items() if ctx.in_window(int(t))} for m, s in old["members"].items()}
            return mem, time.strftime("%H:%MZ", time.gmtime(old["fetched"])) + " (cached)"
    except (OSError, ValueError, KeyError):
        pass
    pts = ring_points(ctx.lat, ctx.lon)
    mem = _members(_fetch(url, scfg["model"], ctx, pts), pts, ctx)
    if mem:
        with open(path, "w") as f:
            json.dump({"fetched": ctx.now, "members": mem}, f, separators=(",", ":"))
    return mem, time.strftime("%H:%MZ", time.gmtime(ctx.now))


def openmeteo_ens(scfg, ctx):
    mem, when = _cached(scfg, ctx, ENS_URL)
    return SourceResult(mem, cycle=when, note=f"{len(mem)} members via Open-Meteo",
                        status="ok" if mem else "missing")


def openmeteo_det(scfg, ctx):
    mem, when = _cached(scfg, ctx, DET_URL)
    return SourceResult(mem, cycle=when, note="via Open-Meteo", status="ok" if mem else "missing")


# ---------------------------------------------------------------- NWS / NDFD
_DUR = re.compile(r"P(?:(\d+)D)?(?:T(?:(\d+)H)?)?")


def _expand(prop):
    out = {}
    for v in (prop or {}).get("values", []):
        start, dur = v["validTime"].split("/")
        t0 = datetime.fromisoformat(start)
        m = _DUR.match(dur)
        hours = int(m.group(1) or 0) * 24 + int(m.group(2) or 0) if m else 1
        for k in range(max(hours, 1)):
            out[int((t0 + timedelta(hours=k)).timestamp())] = v["value"]
    return out, (prop or {}).get("uom", "")


def _kt(x, uom):
    if x is None:
        return None
    if "km_h" in uom:
        return x * 0.539957
    if "m_s" in uom:
        return x * 1.943844
    return x


def nws_grid(scfg, ctx):
    hdr = {"Accept": "application/geo+json"}
    pt = SESSION.get(f"https://api.weather.gov/points/{ctx.lat:.4f},{ctx.lon:.4f}", headers=hdr, timeout=30)
    pt.raise_for_status()
    g = SESSION.get(pt.json()["properties"]["forecastGridData"], headers=hdr, timeout=60)
    g.raise_for_status()
    p = g.json()["properties"]
    spd, su = _expand(p.get("windSpeed"))
    dirs, _ = _expand(p.get("windDirection"))
    gst, gu = _expand(p.get("windGust"))
    rh, _ = _expand(p.get("relativeHumidity"))
    tmp, tu = _expand(p.get("temperature"))
    series = {}
    for t in sorted(set(spd) | set(rh) | set(tmp)):
        if not ctx.in_window(t):
            continue
        rec = {}
        s, d = _kt(spd.get(t), su), dirs.get(t)
        if s is not None and d is not None:
            gg = _kt(gst.get(t), gu)
            rec.update(spd=s, dir=d, gst=max(gg, s) if gg is not None else s)
        if rh.get(t) is not None:
            rec["rh"] = rh[t]
        if tmp.get(t) is not None:
            rec["t"] = tmp[t] * 9 / 5 + 32 if "degC" in tu else tmp[t]
        if rec:
            series[t] = rec
    return SourceResult({"m00": series} if series else {}, cycle=p.get("updateTime", "")[:16].replace("T", " "),
                        note="wind, RH and temperature only", status="ok" if series else "missing")
