"""Droplet geometry: volume, iso-density point cloud, periodic unwrap, sphere fit, contact angle."""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from .mesh import Mesh


def droplet_volume(rho: np.ndarray, vol: np.ndarray, threshold: float, mask: np.ndarray | None = None) -> tuple[float, int]:
    sel = rho >= threshold
    if mask is not None:
        sel &= mask
    return float(vol[sel].sum()), int(sel.sum())


def equivalent_radius(volume: float) -> float:
    return (3.0 * volume / (4.0 * math.pi)) ** (1.0 / 3.0) if volume > 0 else 0.0


def iso_points(mesh: Mesh, rho: np.ndarray, threshold: float, mask: np.ndarray | None = None) -> np.ndarray:
    """Points where the density crosses `threshold` between neighbouring cell centres (linear interpolation)."""
    a, b = mesh.pair_a, mesh.pair_b
    ra, rb = rho[a], rho[b]
    cross = (ra >= threshold) != (rb >= threshold)
    if mask is not None:
        cross &= mask[a] & mask[b]
    if not cross.any():
        return np.zeros((0, 3))
    a, b, ra, rb = a[cross], b[cross], ra[cross], rb[cross]
    t = (threshold - ra) / (rb - ra)
    ca = mesh.cell_centres[a]
    cb = mesh.cell_centres[b] + mesh.pair_shift[cross]
    return ca + t[:, None] * (cb - ca)


def unwrap_periodic(points: np.ndarray, periods: dict[str, float], origin: np.ndarray) -> np.ndarray:
    """Make a periodic point cloud contiguous: centre each periodic axis on its circular mean."""
    if points.size == 0:
        return points
    out = points.copy()
    for axis, L in periods.items():
        if not L or L <= 0:
            continue
        k = "xyz".index(axis)
        x = out[:, k] - origin[k]
        ang = 2.0 * math.pi * x / L
        c = math.atan2(np.sin(ang).mean(), np.cos(ang).mean()) * L / (2.0 * math.pi)
        out[:, k] = origin[k] + c + np.mod(x - c + 0.5 * L, L) - 0.5 * L
    return out


@dataclass
class SphereFit:
    ok: bool
    reason: str = ""
    xc: float = math.nan
    yc: float = math.nan
    zc: float = math.nan
    R: float = math.nan
    rms: float = math.nan
    n_points: int = 0


def fit_sphere(p: np.ndarray, min_radius: float = 1e-12) -> SphereFit:
    """Algebraic least-squares sphere fit x²+y²+z²+Dx+Ey+Fz+G=0 on shifted/scaled coordinates."""
    n = p.shape[0]
    if n < 4:
        return SphereFit(False, f"fewer than 4 fit points ({n})", n_points=n)
    c0 = p.mean(axis=0)
    q = p - c0
    s = math.sqrt((q ** 2).sum(axis=1).mean())
    if not s > 0:
        return SphereFit(False, "all fit points coincide", n_points=n)
    q = q / s
    A = np.column_stack([q, np.ones(n)])
    rhs = -(q ** 2).sum(axis=1)
    sv = np.linalg.svd(A, compute_uv=False)
    if sv[-1] <= 1e-9 * sv[0]:
        return SphereFit(False, "degenerate point set (e.g. coplanar/collinear): sphere not determined", n_points=n)
    sol, *_ = np.linalg.lstsq(A, rhs, rcond=None)
    D, E, F, G = sol
    cen = -0.5 * np.array([D, E, F])
    r2 = cen @ cen - G
    if not np.isfinite(r2) or r2 <= 0:
        return SphereFit(False, "fit produced non-positive radius²", n_points=n)
    R = math.sqrt(r2) * s
    cen = cen * s + c0
    if R < min_radius:
        return SphereFit(False, f"sphere radius below {min_radius:g} m", n_points=n)
    resid = np.linalg.norm(p - cen, axis=1) - R
    return SphereFit(True, "", float(cen[0]), float(cen[1]), float(cen[2]), float(R),
                     float(math.sqrt((resid ** 2).mean())), n)


@dataclass
class ContactResult:
    ok: bool
    reason: str
    angle_deg: float
    contact_radius: float
    z_min: float
    z_max: float
    z_base: float
    fit: SphereFit
    n_iso: int
    fit_mask: np.ndarray | None = None


def contact_angle(points: np.ndarray, fit_lower: float, fit_upper: float, base_fraction: float = 0.05, base_position: float | None = None) -> ContactResult:
    nan = math.nan
    n = points.shape[0]
    if n == 0:
        return ContactResult(False, "no iso-density points", nan, nan, nan, nan, nan, SphereFit(False, "no points"), 0)
    z = points[:, 2]
    zmin, zmax = float(z.min()), float(z.max())
    H = zmax - zmin
    if not H > 0:
        return ContactResult(False, "droplet height is zero", nan, nan, zmin, zmax, nan, SphereFit(False, "H=0"), n)
    zbase = float(z[z <= zmin + base_fraction * H].mean()) if base_position is None else float(base_position)
    m = (z >= zmin + fit_lower * H) & (z <= zmin + fit_upper * H)
    fit = fit_sphere(points[m])
    if not fit.ok:
        return ContactResult(False, fit.reason, nan, nan, zmin, zmax, zbase, fit, n, m)
    arg = max(-1.0, min(1.0, (zbase - fit.zc) / fit.R))
    theta = math.acos(arg)
    return ContactResult(True, "", math.degrees(theta), fit.R * math.sin(theta), zmin, zmax, zbase, fit, n, m)


def droplet_points(mesh: Mesh, rho: np.ndarray, threshold: float, periods: dict[str, float],
                   unwrap: bool, mask: np.ndarray | None = None) -> np.ndarray:
    pts = iso_points(mesh, rho, threshold, mask)
    if unwrap and periods:
        pts = unwrap_periodic(pts, periods, mesh.bounds_min)
    return pts


def substrate_coordinates(points: np.ndarray, normal: str) -> np.ndarray:
    """World points -> orthogonal substrate frame (normal is local +z)."""
    axis = "xyz".index(normal[-1])
    tangents = [k for k in range(3) if k != axis]
    result = np.asarray(points, dtype=float).reshape(-1, 3)[:, tangents + [axis]].copy()
    result[:, 2] *= -1 if normal[0] == "-" else 1
    return result

def contact_for_settings(points, settings):
    sign = -1 if settings.substrate_normal[0] == "-" else 1
    base = None if settings.substrate_position is None else sign * settings.substrate_position
    return contact_angle(substrate_coordinates(points, settings.substrate_normal), settings.fit_lower,
                         settings.fit_upper, settings.base_fraction, base)
