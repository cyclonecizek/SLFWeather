"""Constraint limits the user can change, applied to the stored data.

The sources store distances and probabilities rather than yes/no answers:
  pd / ld    nearest distance (nm, 99 = none) at which precipitation / lightning occurs
  q<R>       NBM thunder probability within R nm
  vt<k>      NBM visibility probability below the k-th threshold in board["vis_thresholds_m"]
  vism       raw visibility in metres (ECMWF HRES)
derive() turns those into the p30 / l30 / l10 / vis series the rest of the code judges, for the radii and
visibility threshold in `limits`, and returns the wind / RH limits to use. docs/index.html does the same thing
in JavaScript (deriveLimits); the two must stay in step. Boards written before this existed carry p30 / l30 /
l10 / vis directly and pass through unchanged.

limits keys (all optional; a missing key means the standard limit):
  crosswind_kt, headtail_kt, rh_pct   numbers
  precip_nm, ltng_nm, pot_nm          radius in nm (precipitation, lightning, lightning potential)
  vis_m                               one of board["vis_thresholds_m"]
"""
from __future__ import annotations


def standard(data: dict, cons: dict) -> dict:
    """The standard limits, in the same keys as `limits`."""
    r = cons["lightning_radii_nm"]
    return {"crosswind_kt": cons["crosswind_kt"], "headtail_kt": cons["headtail_kt"], "rh_pct": cons["rh_pct"],
            "precip_nm": int(cons["precip_radius_nm"]), "ltng_nm": int(r[0]), "pot_nm": int(r[1]),
            "vis_m": data.get("vis_default_m") or cons.get("vis_fallback_m", 400)}


def derive(data: dict, cons: dict, limits: dict | None) -> tuple[dict, dict]:
    """Returns (data with p30/l30/l10/vis derived for `limits`, constraints with the wind/RH limits applied)."""
    std = standard(data, cons)
    use = {k: (limits[k] if limits and limits.get(k) is not None else std[k]) for k in std}
    cons2 = dict(cons)
    for k in ("crosswind_kt", "headtail_kt", "rh_pct"):
        cons2[k] = float(use[k])
    rp, rl, rx = int(use["precip_nm"]), int(use["ltng_nm"]), int(use["pot_nm"])
    thr = float(use["vis_m"])
    ths = data.get("vis_thresholds_m") or []
    k = next((i for i, x in enumerate(ths) if abs(x - thr) < 0.5), None)
    sources = []
    for s in data["sources"]:
        members = []
        for m in s["members"]:
            v, nv = m["v"], dict(m["v"])
            if "pd" in v:
                nv["p30"] = [None if x is None else int(x <= rp) for x in v["pd"]]
            if "ld" in v:
                nv["l30"] = [None if x is None else int(x <= rl) for x in v["ld"]]
                nv["l10"] = [None if x is None else int(x <= rx) for x in v["ld"]]
            else:
                if f"q{rl}" in v:
                    nv["l30"] = v[f"q{rl}"]
                if f"q{rx}" in v:
                    nv["l10"] = v[f"q{rx}"]
            if "vism" in v:
                nv["vis"] = [None if x is None else int(x < thr) for x in v["vism"]]
            elif k is not None and f"vt{k}" in v:
                nv["vis"] = v[f"vt{k}"]
            members.append({**m, "v": nv})
        sources.append({**s, "members": members})
    return {**data, "sources": sources}, cons2
