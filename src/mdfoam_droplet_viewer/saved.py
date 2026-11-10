"""Named snapshots of analysis results ("save as" / "open").

A snapshot holds the results, the settings they were computed with, the models comparison and
where each case's data lives (a local folder or the downloaded mirror of a remote case), so a
saved analysis can be reopened later without recomputing. Files live in <cache root>/saved/:
<id>.json.gz (everything) and <id>.meta.json (small header for the list).
"""

from __future__ import annotations

import gzip
import json
import os
import re
import time
from pathlib import Path

FORMAT = "mdfoam-droplet-viewer-results"


def default_root() -> Path:
    """The cache / saved-results root shared by the app and the command line (MDFOAM_DROPLET_CACHE overrides)."""
    return Path(os.environ.get("MDFOAM_DROPLET_CACHE", Path.home() / ".mdfoam_droplet_viewer"))


def make_payload(version: str, source: dict, run_meta: dict, results: dict, cases: list, models: dict | None = None,
                 ui: dict | None = None) -> dict:
    """What a snapshot holds. cases: CaseData-like objects (name, root, data_dir, meta, remote) of the results."""
    rows = []
    for c in cases:
        if c.name not in results:
            continue
        rows.append({"name": c.name, "root": str(c.root), "data_dir": str(c.data_dir), "meta": c.meta,
                     "remote": getattr(c, "remote", None)})
    return {"version": version, "source": source, "run_meta": run_meta, "results": results,
            "models": models or {}, "cases": rows, "ui": ui or {}}
_BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')


def file_id(name: str) -> str:
    """File-system safe id for a display name (keeps Japanese and other letters)."""
    s = _BAD.sub("_", name.strip()).strip(" ._")
    if not s:
        raise ValueError("enter a name")
    return s[:100]


def _dir(root: Path) -> Path:
    return Path(root) / "saved"


def _jsonable(o):
    if hasattr(o, "item"):  # numpy scalars
        return o.item()
    if hasattr(o, "tolist"):
        return o.tolist()
    if isinstance(o, Path):
        return str(o)
    return str(o)


def exists(root: Path, name: str) -> bool:
    return (_dir(root) / f"{file_id(name)}.json.gz").exists()


def save(root: Path, name: str, payload: dict, overwrite: bool = False) -> dict:
    fid = file_id(name)
    d = _dir(root)
    d.mkdir(parents=True, exist_ok=True)
    data_path = d / f"{fid}.json.gz"
    if data_path.exists() and not overwrite:
        raise FileExistsError(name)
    results = payload.get("results") or {}
    meta = {"format": FORMAT, "id": fid, "name": name.strip(), "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "version": payload.get("version", ""), "source": payload.get("source") or {},
            "n_cases": len(results), "n_rows": sum(len(r.get("rows") or []) for r in results.values()),
            "case_names": list(results)[:6]}
    body = json.dumps({**payload, **{k: meta[k] for k in ("format", "name", "saved_at")}}, default=_jsonable,
                      ensure_ascii=False).encode("utf-8")
    tmp = data_path.with_suffix(".tmp")
    tmp.write_bytes(gzip.compress(body, 6))
    tmp.replace(data_path)
    meta["size"] = data_path.stat().st_size
    (d / f"{fid}.meta.json").write_text(json.dumps(meta, ensure_ascii=False, default=_jsonable), encoding="utf-8")
    return meta


def list_saved(root: Path) -> list[dict]:
    d = _dir(root)
    out = []
    if not d.is_dir():
        return out
    for m in d.glob("*.meta.json"):
        try:
            meta = json.loads(m.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if (d / f"{meta.get('id', '')}.json.gz").exists():
            out.append(meta)
    return sorted(out, key=lambda x: x.get("saved_at", ""), reverse=True)


def load(root: Path, name: str) -> dict:
    p = _dir(root) / f"{file_id(name)}.json.gz"
    payload = json.loads(gzip.decompress(p.read_bytes()).decode("utf-8"))
    if payload.get("format") != FORMAT:
        raise ValueError("not a saved mdFOAM Droplet Viewer result")
    return payload


def delete(root: Path, name: str) -> None:
    fid = file_id(name)
    for suffix in (".json.gz", ".meta.json"):
        p = _dir(root) / f"{fid}{suffix}"
        if p.exists():
            p.unlink()
