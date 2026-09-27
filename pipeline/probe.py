"""Check each source: where it resolved, latest cycle, and which fields exist.

    python -m pipeline.probe
"""
from __future__ import annotations

import os
import time

import yaml

from .common import SESSION, floor_hour, iso
from .grib import AREA, POINT, Missing, match_fields, read_idx
from .run import ROOT
from .src_ncep import _fmt
from .src_web import DET_URL, ENS_URL, VARS, ring_points


def probe_grib(bases, tmpl, cycles):
    for base in bases:
        for c in cycles:
            url = f"{base}/{_fmt(tmpl, c, 1)}"
            try:
                inv = read_idx(url + ".idx")
            except Missing:
                continue
            except Exception as e:
                print(f"    {base}: {e}")
                break
            print(f"    OK {iso(c)}  {url}")
            found = match_fields(inv, {**POINT, **AREA})
            for k in (*POINT, *AREA):
                print(f"       {k:5s} {found[k][3] if k in found else '-- not in inventory'}")
            return
        print(f"    -- nothing in {base}")


def main():
    with open(os.path.join(ROOT, "config.yaml")) as f:
        cfg = yaml.safe_load(f)
    now = int(time.time())
    lat, lon = cfg["site"]["lat"], cfg["site"]["lon"]
    for s in cfg["sources"]:
        if not s.get("enabled", True):
            continue
        print(f"\n[{s['id']}] {s.get('label', '')} ({s['kind']})")
        if s["kind"] == "tle":
            probe_grib([s["base"]], s["file"], [floor_hour(now) - k * 3600 for k in range(10)])
        elif s["kind"] == "nbm_prob":
            cyc = [c for c in (floor_hour(now) - k * 3600 for k in range(36)) if time.gmtime(c).tm_hour in set(s.get("cycles", [0, 6, 12, 18]))]
            for c in cyc:
                url = f"{s['base']}/{_fmt(s['file'], c, 6)}"
                try:
                    inv = read_idx(url + ".idx")
                except Missing:
                    continue
                print(f"    OK {iso(c)}  {url}")
                for r in inv:
                    if ":TSTM:" in r[3]:
                        print(f"       {r[3]}")
                break
            else:
                print("    -- no recent NBM cycle found")
        elif s["kind"] == "multi_model":
            for comp in s["components"]:
                print(f"  {comp['id']}:")
                cyc = [c for c in (floor_hour(now) - k * 3600 for k in range(36))
                       if time.gmtime(c).tm_hour in set(comp.get("cycles", [0, 6, 12, 18]))]
                probe_grib(comp.get("bases") or [comp["base"]], comp["file"], cyc)
        elif s["kind"] in ("openmeteo_ens", "openmeteo_det"):
            url = ENS_URL if s["kind"] == "openmeteo_ens" else DET_URL
            r = SESSION.get(url, timeout=60, params={"latitude": lat, "longitude": lon, "models": s["model"],
                            "hourly": ",".join(VARS), "forecast_days": 2, "timeformat": "unixtime"})
            if r.status_code != 200:
                print(f"  HTTP {r.status_code}: {r.text[:200]}")
                continue
            h = r.json()["hourly"]
            for v in VARS:
                n = sum(x is not None for x in h.get(v, []))
                print(f"    {v:22s} {'-- missing' if v not in h else f'{n} non-null hours'}")
            print(f"    members: {1 + len({k.split('_member')[1] for k in h if '_member' in k})}")
            print(f"    ring points used for area checks: {len(ring_points(lat, lon))}")
        elif s["kind"] == "nws_grid":
            r = SESSION.get(f"https://api.weather.gov/points/{lat:.4f},{lon:.4f}", timeout=30)
            print(f"    HTTP {r.status_code}")


if __name__ == "__main__":
    main()
