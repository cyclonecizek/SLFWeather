"""GFS upper-air files for the GFS model-data workbook, published next to the board data.

  gfs3_<stn>.buf          Penn State BUFKIT sounding (default station XMR), copied unchanged.
                          Import it on the workbook's BUFKIT Import tab and set GFS Calc B2 to "BUFKIT file".
  GFS_upper_air_<name>.csv  Open-Meteo GFS surface + pressure-level CSV, built exactly as the workbook's
                          macro builds it (requests split under 1,900 characters and merged on time).
                          Import it on the GFS Data tab and set GFS Calc B2 to "Open-Meteo".
  gfs_meta.json           What the page needs to label its buttons (run time, when fetched, any error).

Each product is refreshed every `refresh_hours` and only replaced by a complete, valid download, so a
failed fetch never overwrites a good file. Nothing here can stop the board from updating.
"""
from __future__ import annotations

import json
import os
import re
import time

from .common import SESSION, log

LEVELS = [1000, 975, 950, 925, 900, 850, 800, 750, 700, 650, 600, 550, 500, 450, 400, 350, 300, 250, 200, 150, 100,
          70, 50, 40, 30, 20, 15, 10]
SURFACE = ["temperature_2m", "relative_humidity_2m", "wind_speed_10m", "wind_direction_10m", "surface_pressure"]
PER_LEVEL = ["temperature", "relative_humidity", "wind_speed", "wind_direction", "geopotential_height"]
MAX_URL = 1900                      # same limit the workbook's macro uses
MIN_BUFKIT_BLOCKS = 80              # a full file has 100+ hourly/3-hourly soundings; anything shorter is a bad download
_STIM = re.compile(r"TIME\s*=\s*(\d{6})/(\d{4})")


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%MZ", time.gmtime(ts))


def _get(url: str, timeout: int = 60) -> bytes:
    r = SESSION.get(url, timeout=timeout, headers={"Cache-Control": "no-cache"})
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code} from {url.split('?')[0]}")
    return r.content


# ---------------------------------------------------------------- BUFKIT
def bufkit_run(text: str) -> str | None:
    """ISO time of the model run (the first TIME = yymmdd/hhmm line), or None."""
    m = _STIM.search(text)
    if not m:
        return None
    d, h = m.group(1), m.group(2)
    return f"20{d[:2]}-{d[2:4]}-{d[4:6]}T{h[:2]}:{h[2:]}Z"


def bufkit_valid(text: str) -> bool:
    return "SNPARM" in text and bufkit_run(text) is not None and text.count("STID") >= MIN_BUFKIT_BLOCKS


def fetch_bufkit(c: dict) -> tuple[bytes, str]:
    """Newest valid file: the latest link first, then the four run folders. Returns (bytes, run ISO)."""
    stn = str(c.get("station", "xmr")).lower()
    latest = c.get("latest") or "http://www.meteo.psu.edu/bufkit/data/GFS/latest/gfs3_{stn}.buf"
    cycle = c.get("cycle") or "http://www.meteo.psu.edu/bufkit/data/GFS/{cyc}/gfs3_{stn}.buf"
    errors = []
    try:
        raw = _get(latest.replace("{stn}", stn))
        text = raw.decode("utf-8", "replace")
        if bufkit_valid(text):
            return raw, bufkit_run(text)
        errors.append("latest link did not return a complete BUFKIT file")
    except Exception as e:
        errors.append(f"latest link: {e}")
    best = None
    for cyc in ("00", "06", "12", "18"):
        try:
            raw = _get(cycle.replace("{stn}", stn).replace("{cyc}", cyc))
            text = raw.decode("utf-8", "replace")
            if bufkit_valid(text):
                run = bufkit_run(text)
                if best is None or run > best[1]:
                    best = (raw, run)
        except Exception as e:
            errors.append(f"{cyc}Z folder: {e}")
    if best:
        return best
    raise RuntimeError("; ".join(errors))


# ---------------------------------------------------------------- Open-Meteo CSV
def open_meteo_url(c: dict) -> str:
    hourly = list(SURFACE) + [f"{v}_{p}hPa" for p in LEVELS for v in PER_LEVEL]
    return ("https://api.open-meteo.com/v1/gfs?latitude={lat}&longitude={lon}&models=gfs_global&forecast_days={days}"
            "&wind_speed_unit=kn&cell_selection=nearest&format=csv&hourly={vars}").format(
        lat=c.get("lat", 28.64406), lon=c.get("lon", -80.73782), days=int(c.get("forecast_days", 8)), vars=",".join(hourly))


def chunk_urls(url: str) -> list[str]:
    """Split the hourly= list so every request stays under MAX_URL characters (same rule as the macro)."""
    head, rest = url.split("hourly=", 1)
    tail = ""
    if "&" in rest:
        rest, tail = rest.split("&", 1)
        tail = "&" + tail
    tmpl = head + "hourly={V}" + tail
    out, cur = [], ""
    for v in rest.split(","):
        if not cur:
            cur = v
        elif len(tmpl.replace("{V}", cur + "," + v)) > MAX_URL:
            out.append(tmpl.replace("{V}", cur))
            cur = v
        else:
            cur += "," + v
    out.append(tmpl.replace("{V}", cur))
    return out


def _lines(body: str) -> list[str]:
    a = body.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    while len(a) > 1 and not a[-1].strip():
        a.pop()
    return a


def _header(lines: list[str]) -> int:
    for i, ln in enumerate(lines):
        if ln.startswith("time,"):
            return i
    return -1


def merge_chunks(bodies: list[str]) -> str:
    """First part supplies the metadata, header and time column; later parts add their columns."""
    first = _lines(bodies[0])
    h1 = _header(first)
    if h1 < 0:
        raise ValueError("the download did not look like a GFS CSV file")
    for i, body in enumerate(bodies[1:], 2):
        other = _lines(body)
        h2 = _header(other)
        if h2 < 0:
            raise ValueError(f"part {i} did not look like a GFS CSV file")
        if len(other) - h2 != len(first) - h1:
            raise ValueError("the download parts have different numbers of rows")
        for j in range(len(first) - h1):
            t1, t2 = first[h1 + j].split(",", 1)[0], other[h2 + j].split(",", 1)[0]
            if t1 != t2:
                raise ValueError(f"part {i} has different times (row {j})")
            p = other[h2 + j].find(",")
            if p >= 0:
                first[h1 + j] += other[h2 + j][p:]
    return "\n".join(first) + "\n"


def fetch_open_meteo_csv(c: dict) -> str:
    bodies = []
    for u in chunk_urls(open_meteo_url(c)):
        body = _get(u, timeout=90).decode("utf-8", "replace")
        if body.lstrip().startswith("{"):
            raise RuntimeError("the server sent an error message instead of data: " + body[:200])
        bodies.append(body)
    return merge_chunks(bodies)


# ---------------------------------------------------------------- publish
def _atomic(path: str, data: bytes) -> None:
    with open(path + ".tmp", "wb") as f:
        f.write(data)
    os.replace(path + ".tmp", path)


def _load(path: str) -> dict:
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {}


def publish(cfg: dict, out_dir: str, now: float) -> dict:
    """Refresh whichever products are due. Returns the metadata written. Never raises for fetch problems."""
    c = cfg.get("gfs_upper_air") or {}
    if not c.get("enabled", True):
        return {}
    meta_path = os.path.join(out_dir, "gfs_meta.json")
    meta = _load(meta_path)
    refresh = float(c.get("refresh_hours", 3)) * 3600
    jobs = []
    b = c.get("bufkit") or {}
    if b.get("enabled", True):
        stn = str(b.get("station", "xmr")).lower()
        jobs.append(("bufkit", f"gfs3_{stn}.buf", lambda: fetch_bufkit(b), {"station": stn.upper()}))
    o = c.get("open_meteo_csv") or {}
    if o.get("enabled", True):
        name = str(o.get("name", "KTTS"))
        jobs.append(("csv", f"GFS_upper_air_{name}.csv", lambda: (fetch_open_meteo_csv(o).encode("utf-8"), None), {"site": name}))
    for key, fname, fetch, extra in jobs:
        st = meta.get(key) or {}
        path = os.path.join(out_dir, fname)
        if st.get("ok") and st.get("file") == fname and os.path.exists(path) and now - st.get("fetched_unix", 0) < refresh:
            continue
        try:
            raw, run = fetch()
            _atomic(path, raw)
            if key == "csv":                       # first valid time in the file, for the page's label
                m = re.search(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}),", raw.decode("utf-8", "replace"), re.M)
                run = (m.group(1) + "Z") if m else None
            st = dict(extra, file=fname, run=run, fetched=_iso(now), fetched_unix=int(now), ok=True, error=None)
            log.info("GFS upper air %s: wrote %s (%d kB%s)", key, fname, len(raw) // 1024, f", run {run}" if run else "")
        except Exception as e:
            log.warning("GFS upper air %s: %s; keeping the previous file", key, e)
            st = dict(st, **extra, file=fname, ok=False, error=str(e)[:300], last_try=_iso(now))
        st["available"] = os.path.exists(path)
        meta[key] = st
    meta["generated"] = _iso(now)
    _atomic(meta_path, json.dumps(meta, indent=1).encode("utf-8"))
    return meta
