"""Case discovery, time directories, and field access for reconstructed and processor-split data."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import numpy as np

from . import foam
from .mesh import Mesh, compute_mesh, load_mesh

META_FILES = ("case_info.txt", "batch_status.txt")
CLOUD_NAMES = ("moleculeCloud",)


def _natural_key(s: str):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


def read_kv_file(path: Path) -> dict[str, str]:
    out = {}
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
    except OSError:
        pass
    return out


def has_case_data(d: Path) -> bool:
    return (d / "constant" / "polyMesh" / "owner").exists() or (d / "processor0" / "constant" / "polyMesh" / "owner").exists()


def resolve_data_dir(case_root: Path, subpath: str = "auto") -> Path | None:
    """Locate the OpenFOAM case directory inside a case root ('auto' tries main/ then the root)."""
    if subpath and subpath != "auto":
        d = case_root / subpath
        return d if has_case_data(d) else None
    for cand in (case_root / "main", case_root):
        if has_case_data(cand):
            return cand
    return None


def discover_cases(path: Path, subpath: str = "auto") -> list[tuple[str, Path, Path]]:
    """Return (name, case_root, data_dir) for a single case or every case under a parent folder."""
    path = Path(path)
    if not path.is_dir():
        raise FileNotFoundError(f"folder not found: {path}")
    # The folder itself is a case (its data in place, or in main/ / the given sub path).
    # A case root such as run001/ with main/, initial/, initial_a/ is one case, not a parent.
    own = resolve_data_dir(path, subpath)
    if own is not None:
        return [(path.name, path, own)]
    found = _scan_children(path, subpath, "")
    if not found:
        # one level deeper, e.g. distance_recheck_100runs/1.0A/run001_x001 -> name "1.0A/run001_x001"
        for p in _subdirs(path):
            found += _scan_children(p, subpath, p.name + "/")
    return found


def _subdirs(path: Path) -> list[Path]:
    return sorted((p for p in path.iterdir() if p.is_dir() and not p.name.startswith(".") and not p.name.startswith("processor")
                   and p.name not in ("constant", "system") and not foam.is_time_name(p.name)),
                  key=lambda p: _natural_key(p.name))


def _scan_children(path: Path, subpath: str, prefix: str) -> list[tuple[str, Path, Path | None]]:
    found = []
    for p in _subdirs(path):
        d = resolve_data_dir(p, subpath)
        if d is not None:
            found.append((prefix + p.name, p, d))
        elif (p / "main").is_dir() or any((p / m).exists() for m in META_FILES):
            found.append((prefix + p.name, p, None))  # case root without results yet (e.g. not started)
    return found


@dataclass
class TimeEntry:
    name: str
    value: float
    source: str           # "reconstructed" | "processor"
    fields: list[str]


@dataclass
class CaseData:
    """Lightweight view on one case. Heavy objects (mesh, addressing) are loaded lazily."""

    name: str
    root: Path
    data_dir: Path
    meta: dict[str, str] = field(default_factory=dict)
    _processors: list[Path] | None = None   # None = not looked at yet (see lazy in open)
    _times: list[TimeEntry] | None = None
    warnings: list[str] = field(default_factory=list)
    saved_info: dict | None = None          # info stored with a saved result (used until the folders are scanned)

    @classmethod
    def open(cls, name: str, root: Path, data_dir: Path | None, lazy: bool = False) -> "CaseData":
        """lazy: look at processor*/ and the time folders only when first needed. Listing them on a slow path
        (WSL network paths, network drives) takes seconds per case, which opening a saved result must not wait for."""
        meta = {}
        for mf in META_FILES:
            meta.update(read_kv_file(root / mf))
        if data_dir is None:
            return cls(name=name, root=root, data_dir=root, meta=meta, warnings=["no mesh / results found"], _processors=[], _times=[])
        c = cls(name=name, root=root, data_dir=data_dir, meta=meta)
        if not lazy:
            c.scan_times()
        return c

    @property
    def processors(self) -> list[Path]:
        if self._processors is None:
            self._processors = sorted((p for p in self.data_dir.glob("processor*") if p.is_dir() and p.name[9:].isdigit()),
                                      key=lambda p: int(p.name[9:]))
        return self._processors

    @processors.setter
    def processors(self, v: list[Path]) -> None:
        self._processors = v

    @property
    def times(self) -> list[TimeEntry]:
        if self._times is None:
            self.scan_times()
        return self._times

    @times.setter
    def times(self, v: list[TimeEntry]) -> None:
        self._times = v

    @property
    def scanned(self) -> bool:
        return self._times is not None

    # ---------------------------------------------------------------- times
    def scan_times(self) -> None:
        entries: dict[str, TimeEntry] = {}
        for d in self.data_dir.iterdir():
            if d.is_dir() and foam.is_time_name(d.name):
                flds = sorted(f.name for f in d.iterdir() if f.is_file())
                entries[d.name] = TimeEntry(d.name, float(d.name), "reconstructed", flds)
        if self.processors:
            p0 = self.processors[0]
            for d in p0.iterdir():
                if not (d.is_dir() and foam.is_time_name(d.name)):
                    continue
                flds = sorted(f.name for f in d.iterdir() if f.is_file())
                existing = entries.get(d.name)
                # Prefer reconstructed data when it already has field files: never count a time twice.
                if existing is not None and existing.fields:
                    continue
                if not flds:
                    continue
                # Require the time dir in every processor.
                if all((p / d.name).is_dir() for p in self.processors[1:]):
                    entries[d.name] = TimeEntry(d.name, float(d.name), "processor", flds)
                else:
                    self.warnings.append(f"time {d.name}: missing in some processor dirs (skipped)")
        self.times = sorted(entries.values(), key=lambda t: t.value)

    def available_fields(self) -> list[str]:
        names: set[str] = set()
        for t in self.times:
            names.update(t.fields)
        return sorted(names)

    def time_entry(self, name: str) -> TimeEntry:
        for t in self.times:
            if t.name == name:
                return t
        raise KeyError(name)

    # ---------------------------------------------------------------- mesh
    def mesh_dir(self) -> Path:
        return self.data_dir / "constant" / "polyMesh"

    def load_mesh(self, cache_dir: Path | None = None) -> Mesh:
        return _cached_mesh(str(self.data_dir), str(cache_dir) if cache_dir else "")

    def proc_addressing(self) -> list[np.ndarray]:
        return _cached_addressing(str(self.data_dir))

    # ---------------------------------------------------------------- fields
    def field_path_signature(self, time: str, fname: str) -> str:
        """Cheap change detector for a field at a time (size+mtime of the file(s))."""
        te = self.time_entry(time)
        if te.source == "reconstructed":
            paths = [self.data_dir / time / fname]
        else:
            paths = [p / time / fname for p in self.processors]
        parts = []
        for p in paths:
            try:
                st = p.stat()
                parts.append(f"{st.st_size}:{st.st_mtime_ns}")
            except OSError:
                parts.append("missing")
        return "|".join(parts)

    def read_field(self, time: str, fname: str, mesh: Mesh) -> foam.FieldData:
        te = self.time_entry(time)
        if te.source == "reconstructed":
            p = self.data_dir / time / fname
            if not p.exists():
                raise FileNotFoundError(f"{fname} not found at time {time}")
            return foam.read_scalar_field(p, mesh.n_cells)
        addr = self.proc_addressing()
        out = np.full(mesh.n_cells, np.nan)
        dims = ""
        for proc, a in zip(self.processors, addr):
            p = proc / time / fname
            if not p.exists():
                raise FileNotFoundError(f"{proc.name}/{time}/{fname} not found")
            fd = foam.read_scalar_field(p, a.size)
            out[a] = fd.values
            dims = fd.dimensions
        if np.isnan(out).any():
            raise foam.FoamFormatError(self.data_dir / time, "processor fields do not cover all global cells")
        return foam.FieldData(out, dims, False, "volScalarField")

    def field_dimensions(self, fname: str) -> str:
        for t in self.times:
            if fname in t.fields:
                base = self.data_dir if t.source == "reconstructed" else self.processors[0]
                return foam.read_field_dimensions(base / t.name / fname)
        return ""

    # ---------------------------------------------------------------- lagrangian
    def has_lagrangian(self, time: str) -> bool:
        te = self.time_entry(time)
        base = self.data_dir / time if te.source == "reconstructed" else self.processors[0] / time
        return any((base / "lagrangian" / c / "positions").exists() for c in CLOUD_NAMES)

    def read_molecules(self, time: str, mesh: Mesh) -> dict[str, np.ndarray]:
        """Positions, type id, (origProcId, origId) and global cell of every molecule at a time."""
        te = self.time_entry(time)
        bases = [self.data_dir / time] if te.source == "reconstructed" else [p / time for p in self.processors]
        addr = None if te.source == "reconstructed" else self.proc_addressing()
        chunks = {k: [] for k in ("pos", "type", "proc", "oid", "cell")}
        for i, b in enumerate(bases):
            cloud = None
            for c in CLOUD_NAMES:
                if (b / "lagrangian" / c / "positions").exists():
                    cloud = b / "lagrangian" / c
            if cloud is None:
                raise FileNotFoundError(f"no lagrangian cloud in {b}")
            pos, cell = foam.read_positions(cloud / "positions")
            n = pos.shape[0]
            typ = foam.read_list(cloud / "id", "label") if (cloud / "id").exists() else np.zeros(n, np.int64)
            oproc = foam.read_list(cloud / "origProcId", "label") if (cloud / "origProcId").exists() else np.full(n, -1)
            oid = foam.read_list(cloud / "origId", "label") if (cloud / "origId").exists() else np.full(n, -1)
            if addr is not None:
                cell = np.where(cell >= 0, addr[i][np.clip(cell, 0, addr[i].size - 1)], -1)
            for k, v in zip(chunks, (pos, typ, oproc, oid, cell)):
                chunks[k].append(v)
        return {k: (np.concatenate(v) if v else np.zeros(0)) for k, v in chunks.items()}

    def info(self) -> dict:
        if not self.scanned and self.saved_info:  # not looked at yet: what was stored with the saved result
            si = self.saved_info
            return {"name": self.name, "root": str(self.root), "data_dir": str(self.data_dir), "meta": self.meta,
                    "n_times": si.get("n_times", 0), "first_time": si.get("first_time"), "last_time": si.get("last_time"),
                    "n_processors": si.get("n_processors", 0), "sources": si.get("sources", []), "warnings": self.warnings}
        return {
            "name": self.name,
            "root": str(self.root),
            "data_dir": str(self.data_dir),
            "meta": self.meta,
            "n_times": len(self.times),
            "first_time": self.times[0].value if self.times else None,
            "last_time": self.times[-1].value if self.times else None,
            "n_processors": len(self.processors),
            "sources": sorted({t.source for t in self.times}),
            "warnings": self.warnings,
        }


@lru_cache(maxsize=8)
def _cached_mesh(data_dir: str, cache_dir: str) -> Mesh:
    d = Path(data_dir)
    poly = d / "constant" / "polyMesh"
    cdir = Path(cache_dir) if cache_dir else None
    if (poly / "owner").exists():
        return load_mesh(poly, cdir)
    return _mesh_from_processors(d)


@lru_cache(maxsize=8)
def _cached_addressing(data_dir: str) -> list[np.ndarray]:
    d = Path(data_dir)
    procs = sorted((p for p in d.glob("processor*") if p.is_dir() and p.name[9:].isdigit()), key=lambda p: int(p.name[9:]))
    out = []
    for p in procs:
        f = p / "constant" / "polyMesh" / "cellProcAddressing"
        if not f.exists():
            raise FileNotFoundError(f"{f} missing: cannot map processor data to the global mesh")
        out.append(foam.read_list(f, "label"))
    allc = np.concatenate(out) if out else np.zeros(0, np.int64)
    if allc.size and np.unique(allc).size != allc.size:
        raise foam.FoamFormatError(d, "cellProcAddressing assigns a global cell to more than one processor")
    return out


def _mesh_from_processors(d: Path) -> Mesh:
    """Assemble cell geometry from processor meshes when no reconstructed polyMesh exists.

    Cell volumes/centres are exact; neighbour pairs across processor boundaries are NOT restored,
    so iso-surface points near those boundaries are missing (recorded as a warning).
    """
    addr = _cached_addressing(str(d))
    procs = sorted((p for p in d.glob("processor*") if p.is_dir() and p.name[9:].isdigit()), key=lambda p: int(p.name[9:]))
    n = int(max(a.max() for a in addr) + 1)
    vol = np.zeros(n)
    cc = np.zeros((n, 3))
    pa, pb, ps = [], [], []
    bmin, bmax = np.full(3, np.inf), np.full(3, -np.inf)
    zones: dict[str, list] = {}
    periodic: dict[str, float] = {}
    for p, a in zip(procs, addr):
        m = load_mesh(p / "constant" / "polyMesh")
        vol[a] = m.cell_volumes
        cc[a] = m.cell_centres
        pa.append(a[m.pair_a]); pb.append(a[m.pair_b]); ps.append(m.pair_shift)
        bmin = np.minimum(bmin, m.bounds_min); bmax = np.maximum(bmax, m.bounds_max)
        for k, z in m.zones.items():
            zones.setdefault(k, []).append(a[z])
        periodic.update(m.periodic)
    return Mesh(n, vol, cc, bmin, bmax, np.concatenate(pa), np.concatenate(pb), np.concatenate(ps),
                {k: np.concatenate(v) for k, v in zones.items()}, [], periodic,
                ["no reconstructed polyMesh: adjacency across processor boundaries not restored "
                 "(iso-surface point cloud is incomplete there)"])
