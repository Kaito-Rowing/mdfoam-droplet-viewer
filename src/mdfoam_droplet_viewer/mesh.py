"""polyMesh geometry: cell volumes/centres, neighbour pairs (incl. cyclic), zones, periodicity."""

from __future__ import annotations

import hashlib
import os
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from . import foam


@dataclass
class Mesh:
    n_cells: int
    cell_volumes: np.ndarray          # (n,) m^3
    cell_centres: np.ndarray          # (n, 3) m
    bounds_min: np.ndarray            # (3,)
    bounds_max: np.ndarray            # (3,)
    pair_a: np.ndarray                # neighbour pairs (internal + cyclic), cell index a
    pair_b: np.ndarray                # cell index b
    pair_shift: np.ndarray            # (npairs, 3) shift added to centre of b to sit next to a
    zones: dict[str, np.ndarray] = field(default_factory=dict)
    patches: list[dict] = field(default_factory=list)
    periodic: dict[str, float] = field(default_factory=dict)  # axis -> period length detected from cyclic patches
    warnings: list[str] = field(default_factory=list)

    @property
    def dims(self) -> np.ndarray:
        return self.bounds_max - self.bounds_min

    def summary(self) -> dict:
        v = self.cell_volumes
        d = self.dims
        return {
            "n_cells": int(self.n_cells),
            "bounds_min": self.bounds_min.tolist(),
            "bounds_max": self.bounds_max.tolist(),
            "dims": d.tolist(),
            "bbox_volume": float(np.prod(d)),
            "mesh_volume": float(v.sum()),
            "cell_volume_min": float(v.min()),
            "cell_volume_max": float(v.max()),
            "uniform_cells": bool(np.isclose(v.min(), v.max(), rtol=1e-6, atol=0)),
            "zones": {k: int(len(z)) for k, z in self.zones.items()},
            "zone_z_ranges": {
                k: [float(self.cell_centres[z, 2].min()), float(self.cell_centres[z, 2].max())]
                for k, z in self.zones.items() if len(z)
            },
            "patches": [
                {"name": p["name"], "type": p.get("type", ""), "nFaces": p.get("nFaces", 0)}
                for p in self.patches
            ],
            "periodic": self.periodic,
            "n_pairs": int(self.pair_a.size),
            "warnings": self.warnings,
        }

    def zone_mask(self, names: list[str] | None) -> np.ndarray:
        """Boolean cell mask for the union of zones (None/empty = all cells)."""
        if not names:
            return np.ones(self.n_cells, bool)
        mask = np.zeros(self.n_cells, bool)
        for n in names:
            if n in self.zones:
                mask[self.zones[n]] = True
        return mask


def _face_geometry(points: np.ndarray, faces: list[np.ndarray]) -> tuple[np.ndarray, np.ndarray]:
    """Face area vectors and centres (OpenFOAM-style triangle decomposition), vectorised by face size."""
    nf = len(faces)
    sf = np.zeros((nf, 3))
    cf = np.zeros((nf, 3))
    sizes = np.fromiter((len(f) for f in faces), np.int64, nf)
    for k in np.unique(sizes):
        idx = np.nonzero(sizes == k)[0]
        verts = np.stack([faces[i] for i in idx])          # (m, k)
        p = points[verts]                                   # (m, k, 3)
        if k == 3:
            n = 0.5 * np.cross(p[:, 1] - p[:, 0], p[:, 2] - p[:, 0])
            sf[idx] = n
            cf[idx] = p.mean(axis=1)
            continue
        c0 = p.mean(axis=1)                                 # (m, 3)
        pn = np.roll(p, -1, axis=1)
        tri_n = 0.5 * np.cross(pn - p, c0[:, None, :] - p)  # (m, k, 3)
        tri_c = (p + pn + c0[:, None, :]) / 3.0
        sum_n = tri_n.sum(axis=1)
        mag_sum = np.linalg.norm(sum_n, axis=1, keepdims=True)
        unit = sum_n / np.where(mag_sum > 0, mag_sum, 1.0)
        a = np.einsum("mkj,mj->mk", tri_n, unit)             # signed projected areas
        asum = a.sum(axis=1, keepdims=True)
        cf[idx] = np.einsum("mk,mkj->mj", a, tri_c) / np.where(asum != 0, asum, 1.0)
        sf[idx] = sum_n
    return sf, cf


def compute_mesh(points, faces, owner, neighbour, patches, zones) -> Mesh:
    n_cells = int(max(owner.max(), neighbour.max() if neighbour.size else -1) + 1)
    n_internal = neighbour.size
    sf, cf = _face_geometry(points, faces)

    # Estimated cell centre: average of face centres.
    cnt = np.bincount(owner, minlength=n_cells) + np.bincount(neighbour, minlength=n_cells)
    cest = np.zeros((n_cells, 3))
    for d in range(3):
        cest[:, d] = (np.bincount(owner, cf[:, d], n_cells)
                      + np.bincount(neighbour, cf[:n_internal, d], n_cells)) / np.maximum(cnt, 1)

    # Pyramid decomposition.
    pyr_own = np.einsum("ij,ij->i", sf, cf - cest[owner]) / 3.0
    pyr_nei = np.einsum("ij,ij->i", sf[:n_internal], cest[neighbour] - cf[:n_internal]) / 3.0
    vol = np.bincount(owner, pyr_own, n_cells) + np.bincount(neighbour, pyr_nei, n_cells)
    cc = np.zeros((n_cells, 3))
    pc_own = 0.75 * cf + 0.25 * cest[owner]
    pc_nei = 0.75 * cf[:n_internal] + 0.25 * cest[neighbour]
    for d in range(3):
        cc[:, d] = (np.bincount(owner, pyr_own * pc_own[:, d], n_cells)
                    + np.bincount(neighbour, pyr_nei * pc_nei[:, d], n_cells))
    safe = np.where(np.abs(vol) > 0, vol, 1.0)
    cc /= safe[:, None]
    warnings = []
    if np.any(vol <= 0):
        warnings.append(f"{int((vol <= 0).sum())} cells have non-positive volume")
        cc[vol <= 0] = cest[vol <= 0]

    pa = [owner[:n_internal]]
    pb = [neighbour]
    shifts = [np.zeros((n_internal, 3))]
    periodic: dict[str, float] = {}
    by_name = {p["name"]: p for p in patches}
    done = set()
    for p in patches:
        if p.get("type") != "cyclic" or p["name"] in done:
            continue
        nb = by_name.get(p.get("neighbourPatch", ""))
        if nb is None:
            warnings.append(f"cyclic patch {p['name']} has no neighbour patch")
            continue
        done.update({p["name"], nb["name"]})
        if p["nFaces"] != nb["nFaces"]:
            warnings.append(f"cyclic patches {p['name']}/{nb['name']} have different face counts")
            continue
        fa = np.arange(p["startFace"], p["startFace"] + p["nFaces"])
        fb = np.arange(nb["startFace"], nb["startFace"] + nb["nFaces"])
        if fa.size == 0:
            continue
        sep = cf[fa] - cf[fb]  # translation that maps face b onto face a
        pa.append(owner[fa])
        pb.append(owner[fb])
        shifts.append(sep)
        mean_sep = sep.mean(axis=0)
        if np.allclose(sep, mean_sep, atol=1e-6 * np.abs(mean_sep).max()):
            axis = int(np.argmax(np.abs(mean_sep)))
            off_axis = np.delete(np.abs(mean_sep), axis)
            if off_axis.max() < 1e-6 * abs(mean_sep[axis]):
                periodic["xyz"[axis]] = float(abs(mean_sep[axis]))
        else:
            warnings.append(f"cyclic patch {p['name']} has non-uniform (rotational?) transform; pairs kept, no period detected")

    mesh = Mesh(
        n_cells=n_cells,
        cell_volumes=vol,
        cell_centres=cc,
        bounds_min=points.min(axis=0),
        bounds_max=points.max(axis=0),
        pair_a=np.concatenate(pa),
        pair_b=np.concatenate(pb),
        pair_shift=np.concatenate(shifts),
        zones=zones,
        patches=patches,
        periodic=periodic,
        warnings=warnings,
    )
    return mesh


MESH_FILES = ("points", "faces", "owner", "neighbour", "boundary", "cellZones")


def mesh_signature(poly_dir: Path) -> str:
    h = hashlib.sha1()
    for name in MESH_FILES:
        p = poly_dir / name
        if p.exists():
            st = p.stat()
            h.update(f"{name}:{st.st_size}:{st.st_mtime_ns};".encode())
    return h.hexdigest()[:16]


def load_mesh(poly_dir: Path, cache_dir: Path | None = None) -> Mesh:
    """Load polyMesh from disk (with an npz cache keyed by file signatures)."""
    poly_dir = Path(poly_dir)
    cache_file = None
    if cache_dir is not None:
        cache_file = Path(cache_dir) / "mesh" / f"{mesh_signature(poly_dir)}.npz"
        if cache_file.exists():
            try:
                return _mesh_from_npz(cache_file, poly_dir)
            except Exception:
                cache_file.unlink(missing_ok=True)
    points = foam.read_list(poly_dir / "points", "vector")
    faces = foam.read_list(poly_dir / "faces", "face")
    owner = foam.read_list(poly_dir / "owner", "label")
    neighbour = foam.read_list(poly_dir / "neighbour", "label")
    patches = foam.read_boundary(poly_dir / "boundary")
    zones = foam.read_cell_zones(poly_dir / "cellZones")
    mesh = compute_mesh(points, faces, owner, neighbour, patches, zones)
    if cache_file is not None:
        try:
            _save_mesh_cache(mesh, zones, cache_file)
        except OSError:
            pass  # cache is an optimisation; another worker may be writing the same file
    return mesh


def _save_mesh_cache(mesh: Mesh, zones: dict, cache_file: Path) -> None:
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = cache_file.with_name(f"{cache_file.stem}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp.npz")
    try:
        np.savez(
            tmp,
            vol=mesh.cell_volumes, cc=mesh.cell_centres, bmin=mesh.bounds_min, bmax=mesh.bounds_max,
            pa=mesh.pair_a, pb=mesh.pair_b, ps=mesh.pair_shift,
            zone_names=np.array(list(zones.keys()), dtype=object),
            **{f"zone_{i}": z for i, z in enumerate(zones.values())},
            periodic=np.array([[ "xyz".index(k), v] for k, v in mesh.periodic.items()], float).reshape(-1, 2),
            warnings=np.array(mesh.warnings, dtype=object),
        )
        if not cache_file.exists():
            tmp.replace(cache_file)
    finally:
        tmp.unlink(missing_ok=True)


def _mesh_from_npz(path: Path, poly_dir: Path) -> Mesh:
    with np.load(path, allow_pickle=True) as z:
        names = list(z["zone_names"])
        zones = {n: z[f"zone_{i}"] for i, n in enumerate(names)}
        periodic = {"xyz"[int(a)]: float(v) for a, v in z["periodic"]}
        mesh = Mesh(
            n_cells=int(z["vol"].size), cell_volumes=z["vol"], cell_centres=z["cc"],
            bounds_min=z["bmin"], bounds_max=z["bmax"], pair_a=z["pa"], pair_b=z["pb"],
            pair_shift=z["ps"], zones=zones, periodic=periodic, warnings=list(z["warnings"]),
        )
    mesh.patches = foam.read_boundary(poly_dir / "boundary")
    return mesh
