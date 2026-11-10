"""Per-time analysis (droplet, temperature, gas pressure) and per-case summary."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from . import foam
from .case import CaseData
from .droplet import contact_for_settings, droplet_points, droplet_volume, equivalent_radius
from .mesh import Mesh
from .settings import K_B, Settings, density_kind, resolve_region_zones, species_from_field

NAN = math.nan


@dataclass
class Context:
    mesh: Mesh
    kind: str                     # density kind of the phase field: mass | number | unknown
    mass: float                   # molecule mass (kg), NaN if unknown
    species: str
    region_mask: np.ndarray
    droplet_mask: np.ndarray | None
    region_zones: list[str]
    periods: dict[str, float]
    warnings: list[str] = field(default_factory=list)


def build_context(case: CaseData, mesh: Mesh, s: Settings) -> Context:
    warnings = []
    dims = case.field_dimensions(s.density_field)
    kind = density_kind(dims, s.density_field)
    species = s.species or species_from_field(s.density_field)
    mass = s.molecule_mass
    if not mass:
        masses = foam.read_molecule_masses(case.data_dir / "constant" / "moleculeProperties")
        mass = masses.get(species, NAN)
        if math.isnan(mass) and species:
            warnings.append(f"molecule mass for species '{species}' not found in moleculeProperties")
    zones = resolve_region_zones(s.region_zones, list(mesh.zones))
    missing = [z for z in zones if z not in mesh.zones]
    if missing:
        raise ValueError(f"zones not in mesh: {missing}")
    missing_droplet = [z for z in s.droplet_zones if z not in mesh.zones]
    if missing_droplet:
        raise ValueError(f"droplet zones not in mesh: {missing_droplet}")
    periods = dict(mesh.periodic)
    for k, v in (s.periodic_override or {}).items():
        if v is None or v == "":
            continue
        v = float(v)
        if v > 0:
            periods[k] = v
        else:
            periods.pop(k, None)
    periods = {k: v for k, v in periods.items() if k != s.substrate_normal[-1]}  # spec: XY unwrap (z is substrate normal)
    droplet_mask = mesh.zone_mask(s.droplet_zones) if s.droplet_zones else None
    return Context(mesh, kind, mass, species, mesh.zone_mask(zones), droplet_mask, zones, periods, warnings)


def _number_density(case: CaseData, time: str, s: Settings, ctx: Context, rho: np.ndarray) -> tuple[np.ndarray | None, str]:
    """Molecule number density per cell (1/m^3) and a note on how it was obtained."""
    if s.number_density_field:
        try:
            return case.read_field(time, s.number_density_field, ctx.mesh).values, s.number_density_field
        except (FileNotFoundError, foam.FoamFormatError):
            pass
    if ctx.kind == "number":
        return rho, s.density_field
    if ctx.kind == "mass" and ctx.mass > 0:
        return rho / ctx.mass, f"{s.density_field}/m"
    return None, "no number density available"


def gas_cells(rho: np.ndarray, s: Settings, ctx: Context) -> tuple[np.ndarray, np.ndarray]:
    """(gas, margin) masks inside the gas/temperature zones.

    density mode: gas_min <= rho <= gas_max; margin: gas_max < rho < liquid_min.
    distance mode: rho < liquid_min and at least gas_min_distance from the nearest droplet (liquid) cell
    centre (periodic in x/y) and gas_min_height above the bottom of the zones; the rest below liquid_min
    is margin (interface and the layer next to the wall, excluded).
    """
    reg = ctx.region_mask
    if s.gas_mode != "distance":
        return reg & (rho >= s.gas_min) & (rho <= s.gas_max), reg & (rho > s.gas_max) & (rho < s.liquid_min)
    mesh = ctx.mesh
    cand = reg & (rho < s.liquid_min)
    if not cand.any():
        return cand, cand.copy()
    cc = mesh.cell_centres
    liq = rho >= s.liquid_min
    if ctx.droplet_mask is not None:
        liq &= ctx.droplet_mask
    ok = np.ones(rho.size, bool)
    if liq.any() and s.gas_min_distance > 0:
        from scipy.spatial import cKDTree
        lo = mesh.bounds_min
        span = mesh.bounds_max - lo
        box = np.array([ctx.periods.get(a) or 0.0 for a in "xyz"])
        big = 4.0 * span.max() + 1.0  # non-periodic axes: a box large enough never to wrap
        size = np.where(box > 0, box, big)
        p = np.mod(cc - lo + np.where(box > 0, 0.0, span.max()), size)
        d, _ = cKDTree(p[liq], boxsize=size).query(p[cand], distance_upper_bound=s.gas_min_distance)
        far = np.zeros(rho.size, bool)
        far[np.flatnonzero(cand)] = ~np.isfinite(d) | (d >= s.gas_min_distance)
        ok &= far
    if s.gas_min_height > 0:
        hz = np.cbrt(mesh.cell_volumes)
        axis = "xyz".index(s.substrate_normal[-1])
        sign = -1 if s.substrate_normal[0] == "-" else 1
        height = sign * cc[:, axis]
        bottom = (sign * s.substrate_position if s.substrate_position is not None else
                  float(np.min(height[reg] - 0.5 * hz[reg])) if reg.any() else float(height.min()))
        ok &= (height - bottom) >= s.gas_min_height
    gas = cand & ok
    return gas, cand & ~gas


def analyze_time(case: CaseData, time: str, s: Settings, ctx: Context) -> dict:
    mesh = ctx.mesh
    te = case.time_entry(time)
    row: dict = {"time": te.value, "time_name": time, "source": te.source, "status": "ok", "reason": "",
                 "warnings": ""}
    try:
        rho = case.read_field(time, s.density_field, mesh).values
    except (FileNotFoundError, foam.FoamFormatError, ValueError) as exc:
        row.update(status="skipped", reason=str(exc))
        return row
    V = mesh.cell_volumes
    warns = []
    if "Average" in s.density_field:
        warns.append("time-averaged density field")

    # ---- droplet volume / radius
    vol, ncell = droplet_volume(rho, V, s.liquid_min, ctx.droplet_mask)
    row.update(volume=vol, n_liquid_cells=ncell, r_eq=equivalent_radius(vol))

    # ---- phase classification (inside region zones) for the record
    reg = ctx.region_mask
    liq = reg & (rho >= s.liquid_min)
    gas, mar = gas_cells(rho, s, ctx)
    row.update(region_liquid_volume=float(V[liq].sum()), region_gas_volume=float(V[gas].sum()),
               region_margin_volume=float(V[mar].sum()), n_margin_cells=int(mar.sum()))

    # ---- contact angle
    pts = droplet_points(mesh, rho, s.liquid_min, ctx.periods, s.periodic_unwrap, ctx.droplet_mask)
    cr = contact_for_settings(pts, s)
    row.update(contact_angle=cr.angle_deg, contact_radius=cr.contact_radius, contact_reason=cr.reason,
               n_iso_points=cr.n_iso, n_fit_points=cr.fit.n_points, z_base=cr.z_base,
               droplet_height=(cr.z_max - cr.z_min) if cr.n_iso else NAN,
               sphere_R=cr.fit.R, sphere_zc=cr.fit.zc, fit_rms=cr.fit.rms)

    # ---- temperature / pressure need T and N
    T = None
    t_reason = ""
    if s.temperature_enabled or s.pressure_enabled:
        try:
            T = case.read_field(time, s.temperature_field, mesh).values
        except (FileNotFoundError, foam.FoamFormatError) as exc:
            t_reason = f"temperature unavailable: {exc}"
    n, n_note = _number_density(case, time, s, ctx, rho)
    Ni = n * V if n is not None else None

    if s.temperature_enabled:
        tsel = reg & (rho >= s.temp_min)
        if s.temp_rho_max is not None:
            tsel &= rho <= s.temp_rho_max
        row.update(temp_n_cells=int(tsel.sum()), temp_volume=float(V[tsel].sum()))
        if T is None:
            row.update(temp_mean=NAN, temp_reason=t_reason)
        elif not tsel.any():
            row.update(temp_mean=NAN, temp_reason="no cells in temperature density range")
        else:
            if s.temp_average == "number":
                if Ni is None:
                    row.update(temp_mean=NAN, temp_reason=n_note)
                else:
                    w = Ni[tsel]
                    row.update(temp_mean=float((w * T[tsel]).sum() / w.sum()) if w.sum() > 0 else NAN,
                               temp_N=float(w.sum()), temp_reason="" if w.sum() > 0 else "no molecules")
            else:
                occupied = tsel & ((Ni > 0) if Ni is not None else (T > 0))
                w = V[occupied] if s.temp_average == "volume" else np.ones(int(occupied.sum()))
                row.update(temp_mean=float((w * T[occupied]).sum() / w.sum()) if w.size else NAN,
                           temp_excluded_empty=int(tsel.sum() - occupied.sum()),
                           temp_reason="" if w.size else "no occupied cells")

    if s.pressure_enabled:
        Vg = float(V[gas].sum())
        row.update(gas_n_cells=int(gas.sum()), gas_volume=Vg)
        if not gas.any():
            row.update(gas_N=NAN, gas_T=NAN, gas_p=NAN, gas_reason="no gas cells (V=0)" if s.gas_mode == "distance"
                       else "no cells in gas density range (V=0)")
        elif Ni is None:
            row.update(gas_N=NAN, gas_T=NAN, gas_p=NAN, gas_reason=n_note)
        else:
            Ng = float(Ni[gas].sum())
            row["gas_N"] = Ng
            # temperature used for p = k_B N T / V: the cells' own (cell), the droplet's (liquid) or a fixed value
            Tg, why = NAN, ""
            if s.gas_temp == "fixed":
                Tg = float(s.gas_temp_fixed)
            elif T is None:
                why = t_reason
            elif s.gas_temp == "liquid":
                lw = Ni[liq] if Ni is not None else None
                if lw is None or not liq.any() or lw.sum() <= 0:
                    why = "no liquid cells for the gas temperature"
                else:
                    Tg = float((lw * T[liq]).sum() / lw.sum())
            elif Ng > 0:
                Tg = float((Ni[gas] * T[gas]).sum()) / Ng
            if Ng <= 0:
                row.update(gas_T=Tg if s.gas_temp != "cell" else NAN, gas_p=0.0, gas_reason="V>0 but no molecules (N=0)")
            elif not math.isfinite(Tg):
                row.update(gas_T=NAN, gas_p=NAN, gas_reason=why or "no gas temperature")
            else:
                row.update(gas_T=Tg, gas_p=K_B * Ng * Tg / Vg, gas_reason="")
            row["gas_T_source"] = s.gas_temp
        row["gas_N_source"] = n_note + (" (time-averaged)" if "Average" in n_note else "")

    row["warnings"] = "; ".join(warns)
    return row


# --------------------------------------------------------------------------- summary
def evaporation_time(rows: list[dict], tol: float, consecutive: int) -> float:
    """First time of the first run of `consecutive` zero-volume rows; skipped rows break runs."""
    run_start = None
    run_len = 0
    for r in rows:
        if r.get("status") != "ok":
            run_start, run_len = None, 0
            continue
        if r["volume"] <= tol:
            if run_len == 0:
                run_start = r["time"]
            run_len += 1
            if run_len >= consecutive:
                return run_start
        else:
            run_start, run_len = None, 0
    return NAN


def summarize(rows: list[dict], s: Settings) -> dict:
    ok = [r for r in rows if r.get("status") == "ok"]
    out = {"n_times": len(ok), "n_skipped": len(rows) - len(ok)}
    if not ok:
        out.update(max_volume=NAN, final_volume=NAN, evaporation_time=NAN, evaporated=False,
                   initial_angle=NAN, final_angle=NAN, mean_angle=NAN, initial_contact_radius=NAN,
                   final_contact_radius=NAN, initial_volume=NAN, initial_r_eq=NAN, mean_temp=NAN, mean_gas_p=NAN,
                   mean_angle_sd=NAN, mean_temp_sd=NAN, mean_gas_p_sd=NAN)
        return out
    vols = np.array([r["volume"] for r in ok])
    et = evaporation_time(rows, s.zero_tolerance, s.zero_consecutive)
    ang = np.array([r.get("contact_angle", NAN) for r in ok], float)
    rad = np.array([r.get("contact_radius", NAN) for r in ok], float)

    def last_valid(a):
        v = a[~np.isnan(a)]
        return float(v[-1]) if v.size else NAN

    k = max(1, math.ceil(len(ok) * s.mean_angle_percent / 100.0))
    head = ang[:k]
    head = head[~np.isnan(head)]
    temps = np.array([r.get("temp_mean", NAN) for r in ok], float)
    ps = np.array([r.get("gas_p", NAN) for r in ok], float)
    def sd(a):
        """Sample standard deviation (n-1) of the finite values; NaN with fewer than 2 values."""
        v = a[np.isfinite(a)]
        return float(v.std(ddof=1)) if v.size >= 2 else NAN

    out.update(
        initial_time=ok[0]["time"], final_time=ok[-1]["time"],
        initial_volume=float(vols[0]), initial_r_eq=ok[0]["r_eq"],
        max_volume=float(vols.max()), final_volume=float(vols[-1]),
        evaporation_time=et, evaporated=not math.isnan(et),
        initial_angle=float(ang[0]), final_angle=last_valid(ang),
        mean_angle=float(head.mean()) if head.size else NAN, mean_angle_rows=k,
        initial_contact_radius=float(rad[0]), final_contact_radius=last_valid(rad),
        mean_temp=float(np.nanmean(temps)) if np.isfinite(temps).any() else NAN,
        mean_gas_p=float(np.nanmean(ps)) if np.isfinite(ps).any() else NAN,
        # spread over time within the case (same rows as the corresponding mean)
        mean_angle_sd=sd(head), mean_temp_sd=sd(temps), mean_gas_p_sd=sd(ps),
    )
    return out


def derive_gas_density(row: dict, mass: float | None) -> None:
    """Gas number density N/V and mass density m N/V (derived, so cached rows get them too)."""
    N, V = row.get("gas_N"), row.get("gas_volume")
    ok = isinstance(N, (int, float)) and isinstance(V, (int, float)) and math.isfinite(N) and V and V > 0
    n = N / V if ok else NAN
    row["gas_ndens"] = n
    row["gas_rho"] = n * mass if ok and mass else NAN


NORMALIZED = {"r_norm": "r_eq", "V_norm": "volume"}  # derived quantity -> base quantity


def derive_normalized(rows: list[dict]) -> None:
    """r/r0 and V/V0: each run divided by its first valid value (rows in time order, status ok)."""
    for key, base in NORMALIZED.items():
        ref = next((r[base] for r in rows if r.get("status") == "ok" and isinstance(r.get(base), (int, float))
                    and math.isfinite(r[base]) and r[base] > 0), None)
        for r in rows:
            v = r.get(base)
            r[key] = v / ref if ref and isinstance(v, (int, float)) and math.isfinite(v) else NAN


def process_case_times(name: str, root: str, data_dir: str, times: list[str], settings: dict,
                       cache_dir: str | None) -> dict:
    """Worker entry point: analyse a chunk of times of one case. Returns rows and context info."""
    s = Settings.from_dict(settings)
    case = CaseData.open(name, Path(root), Path(data_dir))
    mesh = case.load_mesh(Path(cache_dir) if cache_dir else None)
    ctx = build_context(case, mesh, s)
    rows = []
    for t in times:
        try:
            rows.append(analyze_time(case, t, s, ctx))
        except Exception as exc:  # keep going: one bad time must not kill the case
            rows.append({"time": float(t), "time_name": t, "status": "skipped", "reason": f"error: {exc}"})
    return {"rows": rows, "ctx": ctx_summary(ctx)}


def ctx_summary(ctx: Context) -> dict:
    """What the results keep of the analysis context (molecule mass, box periods, ...)."""
    return {"kind": ctx.kind, "mass": ctx.mass, "species": ctx.species, "region_zones": ctx.region_zones,
            "periods": ctx.periods, "warnings": ctx.warnings + ctx.mesh.warnings}


def case_ctx(case: CaseData, s: Settings, cache_dir: str | Path | None) -> dict:
    """The context of a case without analysing a time (cases whose rows all came from the cache)."""
    mesh = case.load_mesh(Path(cache_dir) if cache_dir else None)
    return ctx_summary(build_context(case, mesh, s))
