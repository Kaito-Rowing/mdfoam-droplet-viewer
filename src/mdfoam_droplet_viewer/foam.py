"""Minimal, fast readers for OpenFOAM ASCII files (fields, lists, polyMesh, dictionaries)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import numpy as np


class FoamFormatError(ValueError):
    """Raised when a file is not in a supported OpenFOAM ASCII format."""

    def __init__(self, path: Path | str, reason: str):
        super().__init__(f"{path}: {reason}")
        self.path = str(path)
        self.reason = reason


_HEADER_RE = re.compile(r"FoamFile\s*\{.*?\}", re.S)
_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.S)
_LINE_COMMENT_RE = re.compile(r"//[^\n]*")


def read_text(path: Path | str) -> str:
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        return fh.read()


def strip_header(text: str) -> tuple[dict, str]:
    """Remove comments and the FoamFile header; return (header dict, body)."""
    text = _BLOCK_COMMENT_RE.sub(" ", text)
    header: dict = {}
    m = _HEADER_RE.search(text)
    if m:
        for line in m.group(0).splitlines()[1:]:
            line = line.strip().rstrip(";")
            if not line or line in "{}":
                continue
            parts = line.split(None, 1)
            if len(parts) == 2:
                header[parts[0]] = parts[1].strip().strip('"')
        text = text[: m.start()] + text[m.end():]
    text = _LINE_COMMENT_RE.sub("", text)
    if header.get("format", "ascii") != "ascii":
        raise FoamFormatError("?", f"binary format is not supported (format={header.get('format')})")
    return header, text


def _matching_paren(text: str, start: int) -> int:
    """Index of the ')' matching the '(' at text[start]."""
    depth = 0
    i = start
    n = len(text)
    while i < n:
        c = text[i]
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    raise ValueError("unbalanced parentheses")


_LIST_START_RE = re.compile(r"(\d+)\s*(\(|\{)")


def _parse_list_at(text: str, pos: int, kind: str, path) -> tuple[np.ndarray, int]:
    """Parse 'N ( ... )' or 'N{value}' starting at or after pos. kind: scalar|label|vector|face."""
    m = _LIST_START_RE.search(text, pos)
    if not m:
        raise FoamFormatError(path, "list start 'N (' not found")
    n = int(m.group(1))
    if m.group(2) == "{":
        end = text.index("}", m.end())
        val = text[m.end():end].strip()
        if kind == "vector":
            v = np.array(val.strip("()").split(), float)
            return np.tile(v, (n, 1)), end + 1
        dtype = np.int64 if kind == "label" else float
        return np.full(n, dtype(float(val)) if kind == "label" else float(val), dtype=dtype), end + 1
    open_idx = m.end() - 1
    if kind == "face":
        close = _matching_paren(text, open_idx)
        tokens = np.array(text[open_idx + 1: close].replace("(", " ").replace(")", " ").split(), np.int64)
        faces = []
        i = 0
        while i < tokens.size:
            k = int(tokens[i])
            faces.append(tokens[i + 1: i + 1 + k])
            i += 1 + k
        if len(faces) != n:
            raise FoamFormatError(path, f"declared {n} faces but found {len(faces)}")
        return faces, close + 1
    if kind == "vector":
        close = _matching_paren(text, open_idx)
        body = text[open_idx + 1: close].replace("(", " ").replace(")", " ")
        arr = np.array(body.split(), float)
        if arr.size != 3 * n:
            raise FoamFormatError(path, f"declared {n} vectors but found {arr.size / 3:g}")
        return arr.reshape(n, 3), close + 1
    close = text.index(")", open_idx)
    body = text[open_idx + 1: close]
    try:
        arr = np.array(body.split(), np.int64 if kind == "label" else float)
    except ValueError as exc:
        raise FoamFormatError(path, f"non-numeric value in list ({exc})") from exc
    if arr.size != n:
        raise FoamFormatError(path, f"declared {n} values but found {arr.size}")
    return arr, close + 1


def read_list(path: Path | str, kind: str):
    """Read a plain list file (points, owner, faces, cellProcAddressing, lagrangian ...)."""
    try:
        header, body = strip_header(read_text(path))
    except FoamFormatError as exc:
        raise FoamFormatError(path, exc.reason) from None
    arr, _ = _parse_list_at(body, 0, kind, path)
    return arr


@dataclass
class FieldData:
    values: np.ndarray
    dimensions: str
    uniform: bool
    field_class: str


_DIM_RE = re.compile(r"dimensions\s*\[([^\]]*)\]")
_INTERNAL_RE = re.compile(r"internalField\s+(uniform|nonuniform)\s*")


def read_scalar_field(path: Path | str, n_cells: int | None = None) -> FieldData:
    """Read a volScalarField internalField (uniform or nonuniform List<scalar>)."""
    path = Path(path)
    text = read_text(path)
    # Only the part before boundaryField is needed; avoid regex over boundary data.
    cut = text.find("boundaryField")
    if cut > 0:
        text = text[:cut]
    try:
        header, body = strip_header(text)
    except FoamFormatError as exc:
        raise FoamFormatError(path, exc.reason) from None
    cls = header.get("class", "")
    if cls and cls != "volScalarField":
        raise FoamFormatError(path, f"unsupported field class '{cls}' (expected volScalarField)")
    dm = _DIM_RE.search(body)
    dims = " ".join(dm.group(1).split()) if dm else ""
    m = _INTERNAL_RE.search(body)
    if not m:
        raise FoamFormatError(path, "internalField not found")
    if m.group(1) == "uniform":
        tok = body[m.end():].split(";", 1)[0].strip()
        try:
            val = float(tok)
        except ValueError:
            raise FoamFormatError(path, f"unsupported uniform value '{tok}'") from None
        if n_cells is None:
            raise FoamFormatError(path, "uniform field needs the mesh cell count")
        return FieldData(np.full(n_cells, val), dims, True, cls)
    rest = body[m.end():]
    if not rest.startswith("List<scalar>"):
        raise FoamFormatError(path, f"unsupported internalField type '{rest[:20].strip()}'")
    arr, _ = _parse_list_at(rest, len("List<scalar>"), "scalar", path)
    if n_cells is not None and arr.size != n_cells:
        raise FoamFormatError(path, f"field has {arr.size} values but mesh has {n_cells} cells")
    if not np.all(np.isfinite(arr)):
        raise FoamFormatError(path, "field contains non-finite values")
    return FieldData(arr, dims, False, cls)


def read_field_dimensions(path: Path | str) -> str:
    """Read only the dimensions entry of a field file (cheap)."""
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        head = fh.read(4096)
    m = _DIM_RE.search(head)
    return " ".join(m.group(1).split()) if m else ""


def read_boundary(path: Path | str) -> list[dict]:
    """Parse constant/polyMesh/boundary into a list of patch dicts."""
    _, body = strip_header(read_text(path))
    m = re.search(r"(\d+)\s*\(", body)
    if not m:
        raise FoamFormatError(path, "patch list not found")
    pos = m.end()
    patches = []
    for pm in re.finditer(r"(\w[\w.\-]*)\s*\{([^{}]*)\}", body[pos:]):
        d = {"name": pm.group(1)}
        for line in pm.group(2).split(";"):
            parts = line.strip().split(None, 1)
            if len(parts) == 2:
                d[parts[0]] = parts[1].strip()
        for key in ("nFaces", "startFace"):
            if key in d:
                d[key] = int(d[key])
        patches.append(d)
    return patches


def read_cell_zones(path: Path | str) -> dict[str, np.ndarray]:
    """Parse constant/polyMesh/cellZones -> {name: cell labels}."""
    path = Path(path)
    if not path.exists():
        return {}
    _, body = strip_header(read_text(path))
    zones = {}
    for zm in re.finditer(r"(\w[\w.\-]*)\s*\{\s*type\s+cellZone\s*;\s*cellLabels\s+List<label>\s*", body):
        arr, _ = _parse_list_at(body, zm.end(), "label", path)
        zones[zm.group(1)] = arr
    return zones


def read_molecule_masses(path: Path | str) -> dict[str, float]:
    """Sum of siteMasses per species from constant/moleculeProperties."""
    path = Path(path)
    if not path.exists():
        return {}
    _, body = strip_header(read_text(path))
    out = {}
    for m in re.finditer(r"(\w+)\s*\{", body):
        name = m.group(1)
        try:
            close = body.index("}", m.end())
        except ValueError:
            continue
        block = body[m.end():close]
        sm = re.search(r"siteMasses\s*\(([^)]*)\)", block)
        if sm:
            try:
                out[name] = float(sum(float(x) for x in sm.group(1).split()))
            except ValueError:
                pass
    return out


def read_id_list(path: Path | str) -> list[str]:
    path = Path(path)
    if not path.exists():
        return []
    _, body = strip_header(read_text(path))
    m = re.search(r"idList\s+\d*\s*\(([^)]*)\)", body)
    return m.group(1).split() if m else []


def read_positions(path: Path | str) -> tuple[np.ndarray, np.ndarray]:
    """Lagrangian 'positions' file (legacy '(x y z) celli' format) -> (xyz, cell)."""
    _, body = strip_header(read_text(path))
    m = _LIST_START_RE.search(body)
    if not m:
        raise FoamFormatError(path, "list start not found")
    n = int(m.group(1))
    if n == 0:
        return np.zeros((0, 3)), np.zeros(0, np.int64)
    open_idx = m.end() - 1
    close = _matching_paren(body, open_idx)
    tokens = body[open_idx + 1: close].replace("(", " ").replace(")", " ").split()
    arr = np.array(tokens, float)
    if arr.size == 4 * n:
        arr = arr.reshape(n, 4)
        return arr[:, :3], arr[:, 3].astype(np.int64)
    if arr.size == 3 * n:
        return arr.reshape(n, 3), np.full(n, -1, np.int64)
    raise FoamFormatError(path, f"unrecognised positions layout ({arr.size} tokens for {n} particles)")


def is_time_name(name: str) -> bool:
    try:
        float(name)
    except ValueError:
        return False
    return True
