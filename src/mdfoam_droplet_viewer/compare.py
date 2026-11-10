"""Load saved results for side-by-side comparison (time series trimmed to the plotted quantities)."""

from __future__ import annotations

import math
from pathlib import Path

from . import saved, sim
from .analysis import derive_gas_density, derive_normalized

ROW_KEYS = ("time", "status", "volume", "r_eq", "contact_angle", "contact_radius", "temp_mean", "gas_p", "gas_T", "gas_N",
            "gas_volume", "gas_rho", "gas_ndens", "n_liquid_cells", "droplet_height", "temp_N", "gas_reason", "sphere_R",
            "r_norm", "V_norm")
CURRENT = "__current__"  # the results loaded on screen (not saved)
SIM_CASES = 8             # cases per saved result whose system/ settings are read


def _clean(v):
    return None if isinstance(v, float) and not math.isfinite(v) else v


def _short(v):
    """A row value for the browser: 7 significant digits (plenty for plots and means, and a hundred runs stay
    small enough to parse quickly)."""
    if isinstance(v, float):
        return float(f"{v:.7g}") if math.isfinite(v) else None
    return v


def load(cache_root: Path, names: list[str]) -> list[dict]:
    """[{name, saved_at, source, settings, cases: {case: {rows, summary, ctx, meta, sim}}}] in the order given."""
    return [from_payload(saved.load(cache_root, name), name) for name in names]


def _fill_ctx(cases: dict, where: dict, settings: dict) -> None:
    """Recover missing context from that case only; never borrow another material's context."""
    from .case import CaseData
    from .settings import Settings
    from .analysis import case_ctx
    for name, result in cases.items():
        if result["ctx"]:
            continue
        location = where.get(name) or {}
        try:
            root = Path(location["root"])
            case = CaseData.open(name, root, Path(location.get("data_dir") or root), lazy=True)
            result["ctx"] = case_ctx(case, Settings.from_dict(settings), _CACHE_ROOT[0] if _CACHE_ROOT else None)
        except Exception:
            pass  # Missing context remains missing, rather than inventing another case's values.


_CACHE_ROOT: list = []  # set by the server: where meshes are cached


def from_payload(p: dict, name: str) -> dict:
    """One comparison item from a saved payload (or the loaded results in the same shape)."""
    res = p.get("results") or {}
    where = {c["name"]: c for c in p.get("cases", [])}
    cases = {}
    for cname, r in res.items():
        mass = (r.get("ctx") or {}).get("mass")
        rows = []
        if r.get("rows") and "r_norm" not in r["rows"][0]:  # results saved before r/r0 existed
            derive_normalized(sorted(r["rows"], key=lambda x: x.get("time", 0)))
        for row in r.get("rows") or []:
            if "gas_ndens" not in row:
                derive_gas_density(row, mass)
            rows.append({k: _short(row.get(k)) for k in ROW_KEYS if k in row})
        if not rows:
            continue  # e.g. a stage without the density field: nothing to compare
        info = None
        ddir = (where.get(cname) or {}).get("data_dir")
        # simulation settings of the first cases only: the runs of an ensemble share them, and reading
        # system/ of a hundred runs on a network / WSL path takes seconds
        if ddir and len(cases) < SIM_CASES and Path(ddir).is_dir():
            try:
                s = sim.read_sim(ddir)
                info = {k: s[k] for k in ("target_T", "target_text", "thermostat", "initial", "control")}
            except Exception:
                info = None
        cases[cname] = {"rows": rows, "summary": {k: _clean(v) for k, v in (r.get("summary") or {}).items()},
                        "ctx": r.get("ctx") or {}, "meta": r.get("meta") or {}, "status": r.get("status"),
                        "n_skipped": len(r.get("skipped") or []), "sim": info}
    _fill_ctx(cases, where, (p.get("run_meta") or {}).get("settings") or {})
    return {"name": p.get("name", name), "saved_at": p.get("saved_at"), "source": p.get("source") or {},
            "settings": (p.get("run_meta") or {}).get("settings") or {}, "cases": cases}
