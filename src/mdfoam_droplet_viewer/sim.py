"""Simulation settings from a case's system/ dictionaries: thermostat targets, initial state, run control.

Read-only and tolerant: unknown or malformed files are listed with an error instead of failing.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

from . import foam

MAX_BYTES = 64 * 1024
_TOKEN_RE = re.compile(r'"[^"]*"|[{}();]|[^\s{}();"]+')
TRUE = {"1", "true", "on", "yes"}


def parse_dict(text: str) -> dict:
    """OpenFOAM dictionary -> {key: value text | sub-dict}. Lists stay as text, e.g. '2 ( water Pt1 )'."""
    _, body = foam.strip_header(text)
    body = "\n".join(line for line in body.splitlines() if not line.lstrip().startswith("#"))
    tokens = _TOKEN_RE.findall(body)
    out, _ = _block(tokens, 0)
    return out


def _block(tok: list[str], i: int) -> tuple[dict, int]:
    out: dict = {}
    while i < len(tok):
        t = tok[i]
        if t == "}":
            return out, i + 1
        if t in (";", "(", ")", "{"):
            i += 1
            continue
        key, i = t.strip('"'), i + 1
        if i < len(tok) and tok[i] == "{":
            out[key], i = _block(tok, i + 1)
            continue
        vals = []
        while i < len(tok) and tok[i] not in (";", "}"):
            if tok[i] == "(":
                depth, j = 0, i
                while j < len(tok):
                    depth += tok[j] == "("
                    depth -= tok[j] == ")"
                    j += 1
                    if depth == 0:
                        break
                vals.append(" ".join(tok[i:j]))
                i = j
            elif tok[i] == "{":  # 'key value { ... }': keep the sub-dict
                sub, i = _block(tok, i + 1)
                out[key + "/" + " ".join(vals)] = sub
                vals = None
                break
            else:
                vals.append(tok[i].strip('"'))
                i += 1
        if vals is not None:
            out[key] = " ".join(vals)
        if i < len(tok) and tok[i] == ";":
            i += 1
    return out, i


def flatten(d: dict, prefix: str = "") -> list[tuple[str, str]]:
    rows = []
    for k, v in d.items():
        if isinstance(v, dict):
            rows += flatten(v, f"{prefix}{k}/")
        else:
            rows.append((prefix + k, v))
    return rows


def _num(v) -> float | None:
    try:
        return float(str(v).split()[0])
    except (ValueError, IndexError):
        return None


def _stamp(data_dir: Path) -> tuple:
    files = []
    for d in (data_dir / "system", data_dir / "constant"):
        if d.is_dir():
            for f in d.iterdir():
                if f.is_file():
                    st = f.stat()
                    files.append((f.name, st.st_size, int(st.st_mtime)))
    return tuple(sorted(files))


def read_sim(data_dir: Path | str) -> dict:
    data_dir = Path(data_dir)
    return _read_sim(str(data_dir), _stamp(data_dir))


@lru_cache(maxsize=256)
def _read_sim(data_dir: str, stamp: tuple) -> dict:
    d = Path(data_dir)
    files: dict[str, dict] = {}
    sysd = d / "system"
    if sysd.is_dir():
        for f in sorted(sysd.iterdir()):
            if not f.is_file() or f.stat().st_size > MAX_BYTES:
                continue
            try:
                files[f.name] = {"values": parse_dict(f.read_text(encoding="utf-8", errors="replace"))}
            except Exception as exc:  # keep going: one odd file must not hide the others
                files[f.name] = {"error": str(exc)}
    ids = foam.read_id_list(d / "constant" / "idList")

    def species(i: int) -> str:
        return ids[i] if 0 <= i < len(ids) else f"id {i}"

    thermostat = []
    for name in ("tVEquilibrationDict", "mdEquilibrationDict"):
        vals = files.get(name, {}).get("values")
        if not vals:
            continue
        for k, v in vals.items():
            m = re.fullmatch(r"coeff(\d+)", k)
            if m and isinstance(v, dict):
                thermostat.append({"id": int(m.group(1)), "species": species(int(m.group(1))),
                                   "fixT": str(v.get("fixT", "0")).strip() in TRUE, "T": _num(v.get("T")),
                                   "fixV": str(v.get("fixV", "0")).strip() in TRUE, "file": name})
        break
    initial = []
    init = files.get("mdInitialiseDict", {}).get("values") or {}
    for zone, v in init.items():
        if isinstance(v, dict) and "temperature" in v:
            lat = re.findall(r"[A-Za-z_]\w*", v.get("latticeIds", ""))
            initial.append({"zone": zone, "T": _num(v.get("temperature")), "massDensity": _num(v.get("massDensity")),
                            "species": sorted(set(lat))})
    cd = files.get("controlDict", {}).get("values") or {}
    control = {k: cd.get(k) for k in ("application", "deltaT", "endTime", "writeInterval", "startFrom") if k in cd}
    controlled = [t for t in thermostat if t["fixT"] and t["T"] is not None]
    return {"files": {k: ({"rows": flatten(v["values"])} if "values" in v else v) for k, v in files.items()},
            "thermostat": thermostat, "initial": initial, "control": control, "species_ids": ids,
            "target_T": sorted({t["T"] for t in controlled}),
            "target_text": ", ".join(f"{t['species']} {t['T']:g} K" for t in controlled)}
