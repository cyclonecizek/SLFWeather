"""Build docs/data/board.json and docs/data/constraints.csv.

    python -m pipeline.run [--only hrrr,gefs]
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time

import yaml

from . import src_ncep, src_web, windows
from .common import Context, PointCache, SourceResult, floor_hour, iso, log

KINDS = {
    "tle": src_ncep.tle,
    "nbm_prob": src_ncep.nbm_prob,
    "multi_model": src_ncep.multi_model,
    "openmeteo_ens": src_web.openmeteo_ens,
    "openmeteo_det": src_web.openmeteo_det,
    "nws_grid": src_web.nws_grid,
}
VARS = ("dir", "gst", "rh", "t", "p30", "l30", "l10", "vis")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _round(var, x):
    if x is None:
        return None
    if var == "p30":
        return int(x)
    if var in ("l30", "l10", "vis"):
        return round(float(x), 2)       # 0/1 from lightning fields, 0-1 from NBM probabilities
    if var == "dir":
        return round(x) % 360
    return round(x, 1)


def build(cfg: dict, only: set | None = None) -> dict:
    now = int(time.time())
    days = int(cfg["days"])
    t_start = floor_hour(now) - 36 * 3600
    t_end = floor_hour(now) + (days + 1) * 86400
    timeline = list(range(t_start, t_end + 1, 3600))
    idx = {t: i for i, t in enumerate(timeline)}
    cons = cfg["constraints"]
    radii = sorted({float(cons["precip_radius_nm"]), *map(float, cons["lightning_radii_nm"])})

    cache = PointCache(os.path.join(ROOT, "cache", "points.json"))
    cache.prune(now - 4 * 86400)
    ctx = Context(now=now, lat=float(cfg["site"]["lat"]), lon=float(cfg["site"]["lon"]),
                  t_start=t_start, t_end=t_end, cache=cache, cons=cons, radii=radii, root=ROOT,
                  om_refresh_h=float(cfg.get("openmeteo_refresh_hours", 3)))

    sources = []
    for scfg in cfg["sources"]:
        if (only and scfg["id"] not in only) or not scfg.get("enabled", True):
            continue
        t0 = time.time()
        try:
            res = KINDS[scfg["kind"]](scfg, ctx)
        except Exception as e:
            log.exception("%s failed", scfg["id"])
            res = SourceResult({}, status="error", note=f"{type(e).__name__}: {e}"[:200])
        members = []
        for mid, series in sorted(res.members.items()):
            v = {k: [None] * len(timeline) for k in VARS}
            for t, rec in series.items():
                i = idx.get(int(t))
                if i is None:
                    continue
                for k in VARS:
                    if k in rec:
                        v[k][i] = _round(k, rec[k])
            v = {k: a for k, a in v.items() if any(x is not None for x in a)}
            if v:
                members.append({"id": mid, "v": v})
        el = round(time.time() - t0, 1)
        log.info("%-10s %-8s %3d members %6.1fs  %s", scfg["id"], res.status, len(members), el, res.note)
        sources.append({"id": scfg["id"], "label": scfg.get("label", scfg["id"]),
                        "family": scfg.get("family", "global"), "weight": float(scfg.get("weight", 1)),
                        "status": res.status, "cycle": res.cycle, "note": res.note,
                        "seconds": el, "members": members})
    cache.save()
    return {"generated": iso(now), "generated_unix": now, "site": cfg["site"],
            "runway_heading_true": cfg["runway_heading_true"], "constraints": cons,
            "windows": cfg["windows"], "days": days,
            "min_window_coverage": cfg.get("min_window_coverage", 0.5),
            "times": timeline, "sources": sources}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=os.path.join(ROOT, "config.yaml"))
    ap.add_argument("--only", default="")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    with open(a.config) as f:
        cfg = yaml.safe_load(f)
    data = build(cfg, set(filter(None, a.only.split(","))) or None)
    if not any(s["members"] for s in data["sources"]):
        log.error("no members from any source; keeping previous output")
        return 1
    out = os.path.join(ROOT, "docs", "data")
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, "board.json.tmp"), "w") as f:
        json.dump(data, f, separators=(",", ":"))
    os.replace(os.path.join(out, "board.json.tmp"), os.path.join(out, "board.json"))
    rows = windows.table(data, cfg)
    with open(os.path.join(out, "constraints.csv"), "w", newline="") as f:
        f.write(windows.to_csv(rows, data["generated"], cfg["site"]["display_tz"]))
    log.info("wrote board.json (%.0f kB) and constraints.csv (%d windows)",
             os.path.getsize(os.path.join(out, "board.json")) / 1024, len(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
