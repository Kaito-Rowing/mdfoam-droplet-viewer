"""Declarative figure specs: what to plot and how, rendered without the browser.

A spec (JSON or YAML) names the data (saved results, cases or ensembles of runs), the figure kind, the quantity,
the error mode and filters, and the style (the keys of export._style plus a size preset). The series are built
here - ported from the compare tab of the browser (ensemble curves and generic comparisons) - so the
screen's PNG export, the command line and agents draw the same figure from the same numbers.

Numbers in a spec are in its display units (`units`, default ns / nm / nm³ / kPa), like the screen.

    kind: compare | timeseries | overlay | section | xy
    source: {saved: NAME or [NAMES], by: saved | case, cases: [...], exclude: [...]}   or   items: [{saved, case(s), label}]

See README ("Figures from spec files") for every key.
"""

from __future__ import annotations

import json
import math
import re
from pathlib import Path

import numpy as np

from . import compare, export, saved

KINDS = ("compare", "timeseries", "overlay", "section", "xy")
COMMON_KEYS = {"kind", "description", "source", "items", "units", "style", "lint", "title", "xlabel", "ylabel", "xticks",
               "output"}
KIND_KEYS = {
    "compare": {"quantity", "band", "filter", "moving_avg"},
    "timeseries": {"quantity", "filter", "moving_avg"},
    "overlay": {"quantity", "band", "filter"},
    "section": {"time", "pick", "projection", "color", "rho_max", "slice_pos", "show_iso", "note", "note_text", "legend"},
    "xy": {"series", "xq", "yq", "xform", "yform"},
}
BAND_MODES = {"sd": "SD (spread of the runs)", "se": "SE of the mean over runs", "ci95": "95 % CI of the mean over runs"}
ERR_MODES = {"se": "SE", "sd": "SD", "ci95": "95 % CI"}
ZERO_AFTER = {"volume", "r_eq", "n_liquid_cells", "r_norm", "V_norm"}

# size presets (the browser's PNG dialog): slides 3:2, journals 4:3
PRESETS = {
    "slide-half": {"width_cm": 15, "height_cm": 10, "dpi": 300, "font_size": 18, "label_size": 18, "tick_size": 16,
                   "legend_size": 14, "line_width": 2.2, "marker_size": 7, "axes_width": 1.5, "theme": "presentation",
                   "grid": True},
    "slide-full": {"width_cm": 24, "height_cm": 16, "dpi": 300, "font_size": 22, "label_size": 22, "tick_size": 20,
                   "legend_size": 18, "line_width": 2.8, "marker_size": 9, "axes_width": 1.8, "theme": "presentation",
                   "grid": True},
    "paper-1col": {"width_cm": 8.5, "height_cm": 6.38, "dpi": 600, "font_size": 9, "label_size": 9, "tick_size": 8,
                   "legend_size": 8, "line_width": 1.0, "marker_size": 3.5, "axes_width": 0.8, "theme": "paper", "grid": False},
    "paper-2col": {"width_cm": 17.5, "height_cm": 13.1, "dpi": 600, "font_size": 9, "label_size": 9, "tick_size": 8,
                   "legend_size": 8, "line_width": 1.0, "marker_size": 3.5, "axes_width": 0.8, "theme": "paper", "grid": False},
}
DEFAULT_PRESET = "slide-half"
STYLE_KEYS = set(export._style({}).keys()) | {"preset", "width_cm", "height_cm"}


class SpecError(ValueError):
    """The spec cannot be drawn as written (the message says what to change)."""


# ------------------------------------------------------------------ spec files
def load_spec(path: str | Path) -> dict:
    """A spec from a .json / .yaml / .yml file."""
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    if p.suffix.lower() in (".yaml", ".yml"):
        try:
            import yaml
        except ImportError as exc:  # pragma: no cover - pyyaml is a dependency
            raise SpecError("YAML specs need PyYAML (pip install pyyaml), or write the spec as JSON") from exc
        spec = yaml.safe_load(text)
    else:
        spec = json.loads(text)
    if not isinstance(spec, dict):
        raise SpecError("a spec is a mapping (kind, source, ...)")
    return spec


def validate(spec: dict) -> list[dict]:
    """Errors raise SpecError; returns warnings (unknown style keys)."""
    kind = spec.get("kind")
    if kind not in KINDS:
        raise SpecError(f"kind must be one of {', '.join(KINDS)} (got {kind!r})")
    allowed = COMMON_KEYS | KIND_KEYS[kind]
    bad = sorted(set(spec) - allowed)
    if bad:
        raise SpecError(f"unknown key(s) for kind {kind}: {', '.join(bad)} (allowed: {', '.join(sorted(allowed))})")
    warns = []
    st = spec.get("style") or {}
    if not isinstance(st, dict):
        raise SpecError("style is a mapping of style keys")
    unknown = sorted(set(st) - STYLE_KEYS)
    if unknown:
        warns.append(_w("unknown_style_key", f"style key(s) not used by the renderer: {', '.join(unknown)}"))
    if st.get("preset") and st["preset"] not in PRESETS:
        raise SpecError(f"style.preset must be one of {', '.join(PRESETS)}")
    if kind != "xy" and kind != "section" and not (spec.get("source") or spec.get("items")):
        raise SpecError("say where the data comes from: source: {saved: NAME} or items: [{saved, case, label}]")
    return warns


def _w(code: str, message: str, **kw) -> dict:
    return {"code": code, "message": message, **kw}


def resolve_style(spec: dict, over: dict | None = None) -> dict:
    """Preset (default slide-half: 15 x 10 cm, 300 dpi) + the spec's style + top-level title / axis labels."""
    st = dict(spec.get("style") or {})
    st.update(over or {})
    out = {"lang": "en", "auto_title": False, "title": ""}
    out.update(PRESETS[st.pop("preset", None) or DEFAULT_PRESET])
    out.update(st)
    for k in ("title", "xlabel", "ylabel"):
        if spec.get(k) is not None:
            out[k] = spec[k]
    if "width_cm" in out:
        out["width"] = float(out.pop("width_cm")) / 2.54
    if "height_cm" in out:
        out["height"] = float(out.pop("height_cm")) / 2.54
    if out.get("title") in (None, "") and not out.get("auto_title"):
        out["title"] = ""
    return out


def resolve_units(spec: dict) -> dict:
    u = {**export.DEFAULT_UNITS, **(spec.get("units") or {})}
    for kind, name in u.items():
        if kind in export.UNITS and name not in export.UNITS[kind]:
            raise SpecError(f"units.{kind} must be one of {', '.join(export.UNITS[kind])} (got {name!r})")
    return u


# ------------------------------------------------------------------ data
class Source:
    """Saved results (and, in the app, the results on screen) for the builders. A saved result is read once and
    kept while its file is unchanged, so repeated previews of one figure do not reload a hundred runs."""

    def __init__(self, root: Path | None = None, current=None):
        self.root = Path(root) if root else saved.default_root()
        self.current = current  # callable -> payload of the results loaded on screen (compare.CURRENT)
        self._payloads: dict[str, tuple] = {}
        self._compare: dict[str, tuple] = {}
        if not compare._CACHE_ROOT:
            compare._CACHE_ROOT[:] = [self.root]

    def _stamp(self, name: str):
        if name == compare.CURRENT:
            return object()  # never reused: the screen's results change
        try:
            return (self.root / "saved" / f"{saved.file_id(name)}.json.gz").stat().st_mtime_ns
        except (OSError, ValueError):
            return None

    def payload(self, name: str) -> dict:
        stamp = self._stamp(name)
        hit = self._payloads.get(name)
        if hit and hit[0] == stamp:
            return hit[1]
        if name == compare.CURRENT:
            if self.current is None:
                raise SpecError("the results on screen are only available inside the app: name a saved result")
            p = self.current()
        else:
            try:
                p = saved.load(self.root, name)
            except FileNotFoundError:
                names = [m["name"] for m in saved.list_saved(self.root)]
                raise SpecError(f"no saved result {name!r} (saved: {', '.join(names) or 'none'})") from None
        self._payloads[name] = (stamp, p)
        return p

    def compare(self, name: str) -> dict:
        p = self.payload(name)
        hit = self._compare.get(name)
        if hit and hit[0] is p:
            return hit[1]
        d = compare.from_payload(p, name)
        self._compare[name] = (p, d)
        return d


def _natural(s: str):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


def _names(v) -> list[str]:
    if v is None:
        return []
    return [v] if isinstance(v, str) else [str(x) for x in v]


def auto_labels(items: list[dict]) -> list[str]:
    """Short names (as the compare tab): path parts shared by all cases dropped, "1p5" written as "1.5"."""
    segs = [it["case"].split("/") for it in items]
    if len(segs) > 1:
        while all(len(x) > 1 and x[-1] == segs[0][-1] for x in segs):
            segs = [x[:-1] for x in segs]
        while all(len(x) > 1 and x[0] == segs[0][0] for x in segs):
            segs = [x[1:] for x in segs]
    one = len({it["saved"] for it in items}) == 1
    out = []
    for i, it in enumerate(items):
        c = re.sub(r"(\d)p(\d)", r"\1.\2", "/".join(segs[i]))
        if one:
            out.append(c)
        else:
            out.append(f"{it['title']} / {c}" if sum(x["saved"] == it["saved"] for x in items) > 1 else it["title"])
    return out


def resolve_items(spec: dict, src: Source) -> list[dict]:
    """[{saved, cases, case, title, label, members: [compare case dicts]}] in plot order (group = index)."""
    items: list[dict] = []
    explicit = spec.get("items")
    if explicit:
        by = "saved" if any(len(_names(it.get("cases"))) > 1 for it in explicit) else "case"
        for it in explicit:
            if not isinstance(it, dict) or not it.get("saved"):
                raise SpecError("each entry of items needs saved: NAME (and case: / cases: / label:)")
            d = src.compare(str(it["saved"]))
            allc = sorted(d["cases"], key=_natural)
            cs = _names(it.get("cases")) or _names(it.get("case")) or allc
            if not it.get("cases") and not it.get("case") and len(allc) > 1:
                by = "saved"
            _check_cases(cs, allc, it["saved"])
            items.append({"saved": str(it["saved"]), "cases": cs, "case": cs[0], "title": d["name"], "label": it.get("label")})
    else:
        s = spec.get("source") or {}
        if isinstance(s, str):
            s = {"saved": s}
        names = _names(s.get("saved"))
        if not names:
            raise SpecError("source.saved: the saved result(s) to plot")
        by = s.get("by") or ("saved" if len(names) >= 2 else "case")
        if by not in ("saved", "case"):
            raise SpecError("source.by is 'saved' (each saved result = one ensemble of runs) or 'case'")
        want, drop = _names(s.get("cases")), set(_names(s.get("exclude")))
        for n in names:
            d = src.compare(n)
            allc = sorted(d["cases"], key=_natural)
            cs = [c for c in (want or allc) if c not in drop]
            if want:
                _check_cases([c for c in want if c not in drop], allc, n)
                cs = [c for c in cs if c in d["cases"]]
            if not cs:
                continue
            if by == "saved":
                items.append({"saved": n, "cases": cs, "case": cs[0], "title": d["name"]})
            else:
                items.extend({"saved": n, "cases": [c], "case": c, "title": d["name"]} for c in cs)
    if not items:
        raise SpecError("no cases with results in the source")
    auto = ([re.sub(r"(\d)p(\d)", r"\1.\2", str(it["title"])) for it in items] if by == "saved" else auto_labels(items))
    for it, a in zip(items, auto):
        it["label"] = it.get("label") or a
        d = src.compare(it["saved"])
        it["members"] = [d["cases"][c] for c in it["cases"]]
    return items


def _check_cases(cs: list[str], allc: list[str], name: str) -> None:
    miss = [c for c in cs if c not in allc]
    if miss:
        raise SpecError(f"{name}: no results for case(s) {', '.join(miss)} (cases with results: {', '.join(allc[:12])}"
                        f"{' …' if len(allc) > 12 else ''})")


def is_ensemble(it: dict) -> bool:
    return len(it["cases"]) > 1


def ok_rows(m: dict) -> list[dict]:
    return [r for r in m.get("rows") or [] if r.get("status") == "ok"]


def item_rows(it: dict) -> list[dict]:
    return [r for m in it["members"] for r in ok_rows(m)]


def window_rows(it: dict, window: dict | None, units: dict) -> list[dict]:
    """Rows inside the averaging window: all | last50 | last20 | range {t0, t1} (display units)."""
    rows = item_rows(it)
    w = window or {"mode": "all"}
    mode = w.get("mode", "range" if ("t0" in w or "t1" in w) else "all")
    if not rows or mode == "all":
        return rows
    t0, t1 = min(r["time"] for r in rows), max(r["time"] for r in rows)
    if mode == "last50":
        return [r for r in rows if r["time"] >= t0 + 0.5 * (t1 - t0)]
    if mode == "last20":
        return [r for r in rows if r["time"] >= t0 + 0.8 * (t1 - t0)]
    if mode != "range":
        raise SpecError("window.mode is all, last50, last20 or range (with t0 / t1)")
    tu = export.unit_of("time", units)[1]
    a = -math.inf if w.get("t0") in (None, "") else float(w["t0"]) * tu
    b = math.inf if w.get("t1") in (None, "") else float(w["t1"]) * tu
    return [r for r in rows if a * (1 - 1e-9) <= r["time"] <= b * (1 + 1e-9)]


# ------------------------------------------------------------------ small helpers (as on screen)
def fmt(v, digits: int = 4) -> str:
    """Number formatting of the browser (fmt in app.js)."""
    if v is None or (isinstance(v, float) and not math.isfinite(v)):
        return ""
    if v == 0:
        return "0"
    if abs(v) >= 1e5 or abs(v) < 1e-3:
        m, e = f"{v:.{digits - 1}e}".split("e")
        return f"{m}e{'+' if int(e) >= 0 else '-'}{abs(int(e))}"
    s = repr(float(f"{v:.{digits}g}"))
    return s[:-2] if s.endswith(".0") else s


def word(st: dict, en: str, ja: str) -> str:
    return ja if st.get("lang") == "ja" else en


def fu(st: dict, u: str) -> str:
    return f"({u})" if st.get("unit_style") == "paren" else f"[{u}]"


def _conv(q: str, v, units: dict):
    v = export.convert(q, v, units)
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else None


def _err(s: dict | None, mode: str):
    """Error of a stats dict {sd, se}: SD, SE or 1.96 SE."""
    if not s or not s.get("n"):
        return None
    if mode == "sd":
        return s.get("sd")
    se = s.get("se")
    return None if se is None else se * (1.96 if mode == "ci95" else 1.0)


def moving_average(x: list, y: list, w: int) -> tuple[list, list]:
    pts = [(a, b) for a, b in zip(x, y) if isinstance(b, (int, float)) and math.isfinite(b)]
    half = int(w) // 2
    ys = np.array([p[1] for p in pts], float)
    c = np.concatenate([[0.0], np.cumsum(ys)])
    out = []
    for i in range(len(pts)):
        a, b = max(0, i - half), min(len(pts), i + half + 1)
        out.append(float((c[b] - c[a]) / (b - a)))
    return [p[0] for p in pts], out


def ensemble_curve(members: list[dict], q: str, units: dict) -> dict:
    """Mean / SD / SE over runs at each output time (ensembleCurve of the browser). Quantities that vanish with the
    droplet count as 0 after a run's evaporation; times where fewer than half of the runs have a value are left
    out (the few slow runs left are not the mean)."""
    ms = [rs for rs in (ok_rows(m) for m in members) if rs]
    key = lambda t: float(f"{t:.9g}")  # noqa: E731
    ts = sorted({key(r["time"]) for rs in ms for r in rs})
    idx = {v: i for i, v in enumerate(ts)}
    s1, s2, n = np.zeros(len(ts)), np.zeros(len(ts)), np.zeros(len(ts))
    for rs in ms:
        last_k = -1
        for r in rs:
            v, k = _conv(q, r.get(q), units), idx[key(r["time"])]
            if v is not None:
                s1[k] += v
                s2[k] += v * v
                n[k] += 1
            last_k = max(last_k, k)
        if q in ZERO_AFTER and rs[-1].get(q) == 0:
            n[last_k + 1:] += 1
    need = max(2, math.ceil(0.5 * len(ms)))
    mean, sd, se = [], [], []
    for k in range(len(ts)):
        if n[k] < need and len(ms) > 1:
            mean.append(None), sd.append(None), se.append(None)
            continue
        m = s1[k] / n[k] if n[k] else None
        v = max(0.0, (s2[k] - n[k] * m * m) / (n[k] - 1)) if n[k] > 1 else None
        mean.append(m)
        sd.append(None if v is None else math.sqrt(v))
        se.append(None if v is None else math.sqrt(v / n[k]))
    return {"x": [_conv("time", t, units) for t in ts], "mean": mean, "sd": sd, "se": se, "n": [int(v) for v in n],
            "n_runs": len(ms)}


def band_of(c: dict, mode: str) -> tuple[list, list]:
    def e(k):
        if mode == "sd":
            return c["sd"][k]
        return None if c["se"][k] is None else c["se"][k] * (1.96 if mode == "ci95" else 1.0)
    lo, hi = [], []
    for k, m in enumerate(c["mean"]):
        ek = e(k)
        lo.append(None if m is None or ek is None else m - ek)
        hi.append(None if m is None or ek is None else m + ek)
    return lo, hi


def _band_mode(spec: dict, key: str = "band", default: str = "sd") -> str:
    m = spec.get(key, default)
    if m not in BAND_MODES:
        raise SpecError(f"{key} must be sd, se or ci95 (got {m!r})")
    return m


def _filter_x(series: list[dict], f: dict | None, units: dict, what: str = "time") -> None:
    """Drop points outside filter {t_min, t_max} (display units) from series drawn over time."""
    if not f:
        return
    bad = set(f) - {"t_min", "t_max"}
    if bad:
        raise SpecError(f"filter takes t_min / t_max (display time units), not {', '.join(sorted(bad))}")
    lo = -math.inf if f.get("t_min") is None else float(f["t_min"])
    hi = math.inf if f.get("t_max") is None else float(f["t_max"])
    if what != "time":
        raise SpecError("filter t_min / t_max applies to plots over time only")
    for s in series:
        keep = [i for i, x in enumerate(s["x"]) if x is not None and lo - 1e-9 * max(1, abs(lo)) <= x <= hi + 1e-9 * max(1, abs(hi))]
        for k in ("x", "y", "y2", "yerr"):
            if k in s and s[k] is not None:
                s[k] = [s[k][i] for i in keep]


# ------------------------------------------------------------------ builders
def build(spec: dict, src: Source, style: dict | None = None) -> dict:
    """The figure as data: {plot: {kind, ...renderer arguments}, info, warnings, style, units}."""
    warnings = validate(spec)
    st = style if style is not None else resolve_style(spec)
    units = resolve_units(spec)
    kind = spec["kind"]
    fn = {"compare": _b_compare, "timeseries": _b_timeseries, "overlay": _b_timeseries, "section": _b_section, "xy": _b_xy}[kind]
    plot, info, w = fn(spec, src, st, units)
    for key in ("title", "xlabel", "ylabel"):
        if key in spec:
            plot[key] = spec[key]
    if spec.get("xticks"):
        xt = spec["xticks"]
        if not isinstance(xt, dict) or not xt.get("values"):
            raise SpecError("xticks: {values: [...], labels: [...]} (labels optional)")
        plot["xticks"] = xt
    info["kind"] = kind
    if plot.get("kind") == "xy":
        info["data_range"] = _ranges(plot["series"])
        if not any(len(s.get("x") or []) for s in plot["series"]):
            w.append(_w("no_data", "nothing to draw: check the source, the filter and the window"))
    return {"plot": plot, "info": info, "warnings": warnings + w, "style": st, "units": units}


def _ranges(series: list[dict]) -> dict:
    xs = [v for s in series for v in s.get("x") or [] if isinstance(v, (int, float)) and math.isfinite(v)]
    ys = [v for s in series for k in ("y", "y2") for v in s.get(k) or [] if isinstance(v, (int, float)) and math.isfinite(v)]
    r = lambda a: [float(f"{min(a):.6g}"), float(f"{max(a):.6g}")] if a else None  # noqa: E731
    return {"x": r(xs), "y": r(ys)}


def _items_info(items: list[dict]) -> list[dict]:
    return [{"label": it["label"], "saved": it["saved"], "n_runs": len(it["cases"]),
             "cases": it["cases"] if len(it["cases"]) <= 6 else it["cases"][:3] + ["…"] + it["cases"][-2:]} for it in items]


def _b_compare(spec, src, st, units):
    """Time series per item: ensembles as mean ± band, single runs as markers (optionally a moving average)."""
    items = resolve_items(spec, src)
    q = spec.get("quantity") or "volume"
    bm = _band_mode(spec)
    ma = int(spec.get("moving_avg") or 0)
    ser, w = [], []
    for i, it in enumerate(items):
        lab = it["label"]
        if is_ensemble(it):
            c = ensemble_curve(it["members"], q, units)
            lo, hi = band_of(c, bm)
            ser.append({"x": c["x"], "y": lo, "y2": hi, "name": "_", "key": lab, "group": i, "mode": "band", "alpha": 0.2})
            ser.append({"x": c["x"], "y": c["mean"], "name": lab, "key": lab, "group": i, "mode": "lines", "lw": 1.4})
            continue
        rows = item_rows(it)
        x = [_conv("time", r["time"], units) for r in rows]
        y = [_conv(q, r.get(q), units) for r in rows]
        ser.append({"x": x, "y": y, "name": "_" if ma else lab, "key": lab, "group": i, "mode": "markers",
                    "alpha": 0.35 if ma else 0.9, "ms": 0.8})
        if ma:
            mx, my = moving_average(x, y, ma)
            ser.append({"x": mx, "y": my, "name": lab, "key": lab, "group": i, "mode": "lines", "lw": 1.6})
    _filter_x(ser, spec.get("filter"), units)
    info = {"items": _items_info(items), "quantity": q}
    if any(is_ensemble(it) for it in items):
        info["band"] = f"±1 {BAND_MODES[bm]}" if bm == "sd" else f"± {BAND_MODES[bm]}"
        info["band_mode"] = bm
    if ma:
        info["moving_average"] = f"{ma} points"
    if q not in export.QUANTITIES:
        w.append(_w("unknown_quantity", f"quantity {q!r} is not a known row quantity: the axis has no unit"))
    return {"kind": "xy", "quantity": q, "series": ser, "xq": "time", "yq": q, "title": ""}, info, w


def _b_timeseries(spec, src, st, units):
    """Raw time series of cases (one saved result) drawn by export.plot_timeseries, as the case / overlay tabs."""
    s = spec.get("source") or {}
    if isinstance(s, str):
        s = {"saved": s}
    names = _names(s.get("saved"))
    if len(names) != 1:
        raise SpecError(f"{spec['kind']}: source.saved is one saved result (several: use kind compare)")
    p = src.payload(names[0])
    res = {k: v for k, v in (p.get("results") or {}).items() if v.get("rows")}
    want, drop = _names(s.get("cases")), set(_names(s.get("exclude")))
    cases = [c for c in (want or sorted(res, key=_natural)) if c not in drop]
    _check_cases(cases, sorted(res, key=_natural), names[0])
    if spec["kind"] == "timeseries" and len(cases) != 1 and not want:
        raise SpecError(f"timeseries draws one case: set source.cases (cases: {', '.join(sorted(res, key=_natural)[:8])} …)"
                        " or use kind overlay")
    f = spec.get("filter") or {}
    if f:
        if set(f) - {"t_min", "t_max"}:
            raise SpecError("filter takes t_min / t_max (display time units)")
        tu = export.unit_of("time", units)[1]
        lo = -math.inf if f.get("t_min") is None else float(f["t_min"]) * tu
        hi = math.inf if f.get("t_max") is None else float(f["t_max"]) * tu
        res = {k: {**v, "rows": [r for r in v["rows"] if lo * (1 - 1e-9) <= r["time"] <= hi * (1 + 1e-9)]} for k, v in res.items()}
    q = spec.get("quantity") or "volume"
    band = bool(spec.get("band")) if spec["kind"] == "overlay" else False
    if band is not False and spec.get("band") not in (None, True, False):
        raise SpecError("overlay band is true / false (mean ± SD over the cases)")
    info = {"cases": cases, "quantity": q}
    if band:
        info["band"] = "±1 SD over the cases at common times (n-1)"
    return ({"kind": "timeseries", "results": res, "names": cases, "quantity": q, "band": band,
             "moving_avg": int(spec.get("moving_avg") or 0)}, info, [])














def _b_xy(spec, src, st, units):
    ser = spec.get("series")
    if not isinstance(ser, list) or not all(isinstance(s, dict) and "x" in s and "y" in s for s in ser):
        raise SpecError("xy: series is a list of {x: [...], y: [...], name, mode, ...}")
    return ({"kind": "xy", "title": "", "series": ser, "xq": spec.get("xq"), "yq": spec.get("yq"),
             "xform": spec.get("xform", ""), "yform": spec.get("yform", "")}, {"n_series": len(ser)}, [])


# ------------------------------------------------------------------ section (density cross-section of one frame)
def _b_section(spec, src, st, units):
    from .batch import _clean
    from .case import CaseData
    from .settings import Settings
    from . import viz
    s = spec.get("source") or {}
    names = _names(s.get("saved"))
    case = s.get("case") or (_names(s.get("cases")) or [None])[0]
    if len(names) != 1 or not case:
        raise SpecError("section: source: {saved: NAME, case: CASE}")
    p = src.payload(names[0])
    cd = next((c for c in p.get("cases") or [] if c["name"] == case), None)
    res = (p.get("results") or {}).get(case)
    if cd is None or not res:
        raise SpecError(f"{names[0]}: no case {case!r} with results")
    ddir = Path(cd.get("data_dir") or cd["root"])
    if not ddir.is_dir():
        raise SpecError(f"the data of {case} is not on this PC ({ddir}): a section needs the simulation output")
    c = CaseData.open(case, Path(cd["root"]), ddir, lazy=True)
    sset = Settings.from_dict((p.get("run_meta") or {}).get("settings") or {})
    root = src.root
    lu, lf = export.unit_of("r_eq", units)
    tu, tf = export.unit_of("time", units)
    plane = spec.get("projection") or "xz"
    if plane not in ("xz", "yz", "xy"):
        raise SpecError("projection is xz, yz or xy")
    k = 3 - "xyz".index(plane[0]) - "xyz".index(plane[1])
    info, w = {"case": case, "saved": names[0], "projection": plane}, []
    tn = spec.get("time", "auto")
    if tn in (None, "auto"):
        tn, pick = _pick_time(c, res, sset, root, spec.get("pick") or {}, units)
        info["picked"] = pick
        w.append(_w("time_auto", f"time picked automatically: {tn} ({fmt(float(tn) / tf, 4)} {tu}); pin it with time: "
                                 f"'{tn}' so the figure does not change"))
    else:
        tn = _time_name(res, str(tn), tf)
    fr = _clean(viz.frame(c, tn, sset, root, cells=False, grid=True))
    info["time"] = tn
    info["time_display"] = f"{fmt(fr['time'] / tf, 4)} {tu}"
    con = fr["contact"]
    info["fitted_R"] = f"{fmt(con['R'] / lf, 4)} {lu}" if con.get("R") else None
    pos = spec.get("slice_pos", "droplet")
    opts = {"view": "slice", "projection": plane, "slice_color": spec.get("color") or "rho",
            "show_iso": bool(spec.get("show_iso", False)), "slice_legend": bool(spec.get("legend", True)), "no_title": not st.get("title")}
    if pos == "droplet":
        if con.get("xc") is None:
            raise SpecError(f"no fitted sphere at {tn}: set slice_pos to a number ({lu}) or 'anchor'")
        lo, per = fr["bounds_min"], fr["periods"] or {}
        a = "xyz"[k]
        v = (con["xc"], con["yc"], con["zc"])[k]
        L = per.get(a)
        opts["slice_pos"] = lo[k] + ((v - lo[k]) % L) if L else v
    elif pos == "anchor":
        opts["slice_pos"] = viz.anchor(c, sset, root)["slice"]["xyz"[k]]
    else:
        opts["slice_pos"] = float(pos) * lf
    info["slice_pos"] = f"{'xyz'[k]} = {fmt(opts['slice_pos'] / lf, 4)} {lu}"
    if spec.get("rho_max") is not None:
        opts["rho_max"] = float(spec["rho_max"])
    else:
        opts["rho_max"] = viz.anchor(c, sset, root)["rho_max"]
        w.append(_w("rho_max_auto", f"colour scale top from the case (rho_max = {fmt(opts['rho_max'], 4)}): set rho_max to fix it "
                                    "across figures"))
    note = spec.get("note", "text" if spec.get("note_text") else "none")
    if note not in ("pos", "R", "none", "text"):
        raise SpecError("note is pos, R, text (with note_text) or none")
    opts["slice_note"], opts["slice_text"] = note, spec.get("note_text") or ""
    return {"kind": "section", "frame": fr, "opts": opts}, info, w


def _time_name(res: dict, tn: str, tf: float) -> str:
    names = [r.get("time_name") for r in res.get("rows") or []]
    if tn in names:
        return tn
    try:
        v = float(tn)
    except ValueError:
        raise SpecError(f"time {tn!r} is not a time of this case") from None
    best = min((r for r in res.get("rows") or [] if r.get("time_name")), key=lambda r: abs(r["time"] - v), default=None)
    if best is None or abs(best["time"] - v) > 1e-6 * max(abs(v), 1e-30):
        raise SpecError(f"time {tn} is not an output time of this case (write the time name, e.g. '{names[0] if names else '1e-09'}')")
    return best["time_name"]


def _pick_time(c, res: dict, sset, root, pick: dict, units: dict) -> tuple[str, dict]:
    """A representative frame: the droplet near the middle of the periodic box (so the section is not cut by a wall)
    and its fitted radius closest to the mean over the rows after t_min."""
    from .batch import _clean
    from . import viz
    tf = export.unit_of("time", units)[1]
    lf = export.unit_of("r_eq", units)[1]
    t_min = float(pick.get("t_min") or 0) * tf
    rows = [r for r in res.get("rows") or [] if r.get("status") == "ok" and r["time"] >= t_min * (1 - 1e-9) and r.get("time_name")]
    if not rows:
        raise SpecError("pick: no rows after t_min")
    Rs = [r["sphere_R"] for r in rows if isinstance(r.get("sphere_R"), (int, float)) and math.isfinite(r["sphere_R"])]
    R_t = float(pick["R_target"]) * lf if pick.get("R_target") else (float(np.mean(Rs)) if Rs else None)
    stride = int(pick.get("stride") or 4)
    max_off = float(pick.get("max_offset") or 0.2)
    cands = []
    for r in rows[::stride]:
        fr = _clean(viz.frame(c, r["time_name"], sset, root, cells=False))
        con, lo, per = fr["contact"], fr["bounds_min"], fr["periods"] or {}
        if con.get("xc") is None or not con.get("R"):
            continue
        off = 0.0
        for kk, a in enumerate("xy"):
            L = per.get(a)
            if L:
                v = lo[kk] + (((con["xc"], con["yc"])[kk] - lo[kk]) % L)
                off = max(off, abs(v - (lo[kk] + L / 2)) / L)
        dR = abs(con["R"] - R_t) / R_t if R_t else 0.0
        cands.append((off, dR, r["time_name"], con["R"]))
    if not cands:
        raise SpecError("pick: no frame with a fitted sphere")
    near = [x for x in cands if x[0] < max_off] or sorted(cands)[:5]
    off, dR, tn, R = min(near, key=lambda x: x[1])
    return tn, {"offset_from_box_centre": round(off, 3), "R": R / lf, "R_target": (R_t or 0) / lf, "n_candidates": len(cands)}


# ------------------------------------------------------------------ render
def render(spec: dict, src: Source, *, style_over: dict | None = None, lint_opts: dict | None = None) -> tuple[bytes, dict]:
    """(file bytes, report {warnings, info, size}) of a spec. The layout checks run on the drawn figure."""
    st = resolve_style(spec, style_over)
    b = build(spec, src, st)
    data = draw(b["plot"], st, b["units"], report := {"lint_opts": {**(spec.get("lint") or {}), **(lint_opts or {})}})
    sty = export._style(st)
    rep = {"warnings": b["warnings"] + report.get("lint", []), "info": b["info"],
           "size": {"width_cm": round(float(sty["width"]) * 2.54, 2), "height_cm": round(float(sty["height"]) * 2.54, 2),
                    "dpi": int(sty["dpi"]), "format": sty["format"] if b["plot"]["kind"] != "section" else "png",
                    "px": [round(float(sty["width"]) * int(sty["dpi"])), round(float(sty["height"]) * int(sty["dpi"]))]}}
    return data, rep


def draw(plot: dict, st: dict, units: dict, report: dict | None = None) -> bytes:
    """Bytes of a built plot (xy / timeseries / section)."""
    k = plot["kind"]
    if k == "xy":
        return export.plot_xy(plot.get("series") or [], st, plot.get("xlabel", ""), plot.get("ylabel", ""), plot.get("title", ""),
                              units=units, xq=plot.get("xq"), yq=plot.get("yq"), xform=plot.get("xform", ""),
                              yform=plot.get("yform", ""), xticks=plot.get("xticks"), report=report)
    if k == "timeseries":
        return export.plot_timeseries(plot["results"], plot["names"], plot["quantity"], units, st, plot.get("band", False),
                                      moving_avg=plot.get("moving_avg", 0), report=report)
    if k == "section":
        img = export.render_frame(plot["frame"], plot["opts"], units, st, report=report)
        import io
        buf = io.BytesIO()
        dpi = int(export._style(st)["dpi"])
        img.save(buf, format="PNG", dpi=(dpi, dpi))
        return buf.getvalue()
    raise SpecError(f"cannot draw a plot of kind {k}")


def series_json(b: dict) -> dict:
    """A built figure as JSON (the series and axis settings; for xy figures the /api/plot/png body)."""
    p = {k: v for k, v in b["plot"].items() if k not in ("results", "frame")}
    return {"plot": p, "info": b["info"], "warnings": b["warnings"]}
