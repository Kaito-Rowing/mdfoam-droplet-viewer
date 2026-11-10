"""Data for 2D contact-angle diagnostics and 3D overviews of one case at one time."""

from __future__ import annotations

import math

import numpy as np

from .analysis import build_context, gas_cells
from .case import CaseData
from .droplet import contact_for_settings, droplet_points, substrate_coordinates
from .settings import Settings


def _subsample(a: np.ndarray, limit: int, seed: int = 0) -> np.ndarray:
    if limit and a.shape[0] > limit:
        idx = np.random.default_rng(seed).choice(a.shape[0], limit, replace=False)
        return a[np.sort(idx)]
    return a


# phase codes for the cross-section view (same rules as the analysis)
PHASE_OUTSIDE, PHASE_BELOW_GAS, PHASE_GAS, PHASE_MARGIN, PHASE_LIQUID = 0, 1, 2, 3, 4


def phase_codes(rho: np.ndarray, s: Settings, region: np.ndarray, droplet: np.ndarray | None,
                gas: np.ndarray | None = None, margin: np.ndarray | None = None) -> np.ndarray:
    """Per-cell phase: liquid (droplet cells) wins; other cells outside the gas/temperature zones are
    'outside'; inside them: gas, margin (excluded), or below the gas lower limit (density mode)."""
    if gas is None:
        gas = region & (rho >= s.gas_min) & (rho <= s.gas_max)
        margin = region & (rho > s.gas_max) & (rho < s.liquid_min)
    code = np.full(rho.size, PHASE_OUTSIDE, np.int8)
    code[region] = PHASE_BELOW_GAS
    code[gas] = PHASE_GAS
    code[margin] = PHASE_MARGIN
    liq = rho >= s.liquid_min
    if droplet is not None:
        liq &= droplet
    code[liq] = PHASE_LIQUID
    return code


def frame(case: CaseData, time: str, s: Settings, cache_dir=None, *, molecules: bool = False,
          cells: bool = True, max_points: int = 20000, grid: bool = False) -> dict:
    mesh = case.load_mesh(cache_dir)
    ctx = build_context(case, mesh, s)
    rho = case.read_field(time, s.density_field, mesh).values
    pts = droplet_points(mesh, rho, s.liquid_min, ctx.periods, s.periodic_unwrap, ctx.droplet_mask)
    cr = contact_for_settings(pts, s)
    out = {
        "case": case.name, "time": float(time), "time_name": time,
        "bounds_min": mesh.bounds_min.tolist(), "bounds_max": mesh.bounds_max.tolist(),
        "periods": ctx.periods,
        "iso": pts.tolist(),
        "fit_mask": cr.fit_mask.tolist() if cr.fit_mask is not None else [],
        "contact": {"ok": cr.ok, "reason": cr.reason, "angle_deg": _f(cr.angle_deg),
                    "contact_radius": _f(cr.contact_radius), "z_base": _f(cr.z_base), "z_min": _f(cr.z_min),
                    "z_max": _f(cr.z_max), "xc": _f(cr.fit.xc), "yc": _f(cr.fit.yc), "zc": _f(cr.fit.zc),
                    "R": _f(cr.fit.R), "rms": _f(cr.fit.rms), "n_fit": cr.fit.n_points,
                    "fit_lower_z": _f(cr.z_min + s.fit_lower * (cr.z_max - cr.z_min)) if cr.n_iso else None,
                    "fit_upper_z": _f(cr.z_min + s.fit_upper * (cr.z_max - cr.z_min)) if cr.n_iso else None},
    }
    if cells:
        reg = ctx.region_mask
        cc = mesh.cell_centres
        liq = (rho >= s.liquid_min) & (ctx.droplet_mask if ctx.droplet_mask is not None else True)
        gas, mar = gas_cells(rho, s, ctx)
        out["cells"] = {
            "liquid": cc[liq].tolist(), "liquid_rho": rho[liq].tolist(),
            "margin": cc[mar].tolist(), "margin_rho": rho[mar].tolist(),
            "gas_occupied": cc[gas & (rho > 0)].tolist(), "gas_occupied_rho": rho[gas & (rho > 0)].tolist(),
            "n": {"liquid": int(liq.sum()), "margin": int(mar.sum()), "gas": int(gas.sum()),
                  "gas_empty": int((gas & (rho <= 0)).sum())},
            "volume": {"liquid": float(mesh.cell_volumes[liq].sum()), "margin": float(mesh.cell_volumes[mar].sum()),
                       "gas": float(mesh.cell_volumes[gas].sum())},
            "cell_size": float(np.cbrt(np.median(mesh.cell_volumes))),
        }
    if grid:
        # every cell (centre, density, phase) for cross-sections; rounded to keep the payload small
        g, mg = gas_cells(rho, s, ctx)
        code = phase_codes(rho, s, ctx.region_mask, ctx.droplet_mask, g, mg)
        out["grid"] = {
            "cc": np.round(mesh.cell_centres, 13).tolist(),
            "rho": [float(f"{v:.4g}") for v in rho],
            "phase": code.tolist(),
            "zones_outside": [z for z in mesh.zones if z not in ctx.region_zones] if ctx.region_zones else [],
            "thresholds": {"gas_min": s.gas_min, "gas_max": s.gas_max, "liquid_min": s.liquid_min, "gas_mode": s.gas_mode,
                           "gas_min_distance": s.gas_min_distance, "gas_min_height": s.gas_min_height},
            "density_kind": ctx.kind,
        }
    if molecules:
        try:
            mol = case.read_molecules(time, mesh)
            pos = mol["pos"]
            typ = mol["type"]
            keep = _subsample(np.arange(pos.shape[0]), max_points)
            out["molecules"] = {"pos": pos[keep].tolist(), "type": typ[keep].tolist(), "n_total": int(pos.shape[0])}
        except (FileNotFoundError, ValueError) as exc:
            out["molecules"] = {"error": str(exc)}
    out["coordinate_frame"] = {"normal": s.substrate_normal, "axes": "substrate tangents u,v; normal w", "units": "m"}
    transform = lambda points: substrate_coordinates(np.asarray(points).reshape(-1, 3), s.substrate_normal).tolist()
    out["iso"] = transform(out["iso"])
    bounds = np.array([out["bounds_min"], out["bounds_max"]])
    bounds = substrate_coordinates(bounds, s.substrate_normal)
    out["bounds_min"], out["bounds_max"] = bounds.min(axis=0).tolist(), bounds.max(axis=0).tolist()
    tangents = [a for a in "xyz" if a != s.substrate_normal[-1]]
    out["periods"] = {"xy"[i]: ctx.periods[a] for i,a in enumerate(tangents) if a in ctx.periods}
    if "grid" in out:
        out["grid"]["cc"] = transform(out["grid"]["cc"])
    if "cells" in out:
        for key in ("liquid", "margin", "gas_occupied"):
            out["cells"][key] = transform(out["cells"][key])
    if "molecules" in out and "pos" in out["molecules"]:
        out["molecules"]["pos"] = transform(out["molecules"]["pos"])
    return out


def anchor(case: CaseData, s: Settings, cache_dir=None, times: list[str] | None = None, n_samples: int = 12) -> dict:
    """Case-wide reference for views over time.

    slice: for each axis, the cell layer that cuts the most droplet (liquid) cells, summed over sampled
    times - the cross-section that looks most like the droplet (through its centre for xz / yz, its
    widest horizontal cut for xy). It does not move with time, so a time series or GIF keeps one plane.
    rho_max: the density scale (99.9th percentile over the sampled times) for a fixed colour range.
    """
    mesh = case.load_mesh(cache_dir)
    ctx = build_context(case, mesh, s)
    names = list(times) if times else [t.name for t in case.times if s.density_field in t.fields]
    if len(names) > n_samples:
        names = [names[round(i * (len(names) - 1) / (n_samples - 1))] for i in range(n_samples)]
    cc = np.round(substrate_coordinates(mesh.cell_centres, s.substrate_normal), 12)
    layers = [np.unique(cc[:, k]) for k in range(3)]
    idx = [np.searchsorted(layers[k], cc[:, k]) for k in range(3)]
    counts = [np.zeros(layers[k].size) for k in range(3)]
    rmax = 0.0
    for t in names:
        rho = case.read_field(t, s.density_field, mesh).values
        if rho.size:
            rmax = max(rmax, float(np.nanpercentile(rho, 99.9)))
        liq = rho >= s.liquid_min
        if ctx.droplet_mask is not None:
            liq &= ctx.droplet_mask
        for k in range(3):
            counts[k] += np.bincount(idx[k][liq], minlength=layers[k].size)
    sl = {}
    for k, a in enumerate("xyz"):
        sl[a] = float(layers[k][int(np.argmax(counts[k]))]) if counts[k].any() else float(0.5 * (layers[k][0] + layers[k][-1]))
    return {"case": case.name, "slice": sl, "rho_max": rmax or None, "n_times": len(names),
            "has_liquid": bool(any(c.any() for c in counts))}


def _f(x):
    return None if x is None or (isinstance(x, float) and not math.isfinite(x)) else float(x)


def best_layer(grid: dict, k: int) -> float | None:
    """The layer (coordinate along axis k) cutting the most droplet (liquid) cells in this frame."""
    cc = np.round(np.asarray(grid["cc"], float)[:, k], 12)
    liq = np.asarray(grid["phase"]) == PHASE_LIQUID
    if not liq.any():
        return None
    layers = np.unique(cc)
    n = np.bincount(np.searchsorted(layers, cc[liq]), minlength=layers.size)
    return float(layers[int(np.argmax(n))])


def slice_grid(grid: dict, plane: str, pos: float | None):
    """Cells of the layer nearest to `pos` along the plane normal, arranged on the in-plane grid.

    Returns (layer coordinate, xs, ys, phase[ny, nx], rho[ny, nx]) with NaN where no cell sits.
    """
    cc = np.asarray(grid["cc"], float)
    i, j = "xyz".index(plane[0]), "xyz".index(plane[1])
    k = 3 - i - j
    layers = np.unique(np.round(cc[:, k], 12))
    if pos is None:
        pos = 0.5 * (layers[0] + layers[-1])
    val = layers[np.argmin(np.abs(layers - pos))]
    sel = np.abs(cc[:, k] - val) < 1e-12 + 1e-6 * (layers[-1] - layers[0] + 1e-30)
    xs = np.unique(np.round(cc[sel, i], 12))
    ys = np.unique(np.round(cc[sel, j], 12))
    ph = np.full((ys.size, xs.size), np.nan)
    rh = np.full((ys.size, xs.size), np.nan)
    xi = np.searchsorted(xs, np.round(cc[sel, i], 12))
    yi = np.searchsorted(ys, np.round(cc[sel, j], 12))
    ph[yi, xi] = np.asarray(grid["phase"], float)[sel]
    rh[yi, xi] = np.asarray(grid["rho"], float)[sel]
    return float(val), xs, ys, ph, rh
