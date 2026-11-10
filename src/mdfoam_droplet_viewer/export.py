"""CSV tables, publication-style PNG plots, and visualization PNG/GIF export."""

from __future__ import annotations

import csv
import io
import json
import math
import re
import zipfile

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402

# ------------------------------------------------------------------ units
UNITS = {
    "length": {"m": 1.0, "nm": 1e-9, "Å": 1e-10},
    "volume": {"m³": 1.0, "nm³": 1e-27},
    "time": {"s": 1.0, "ns": 1e-9, "ps": 1e-12},
    "pressure": {"Pa": 1.0, "kPa": 1e3, "MPa": 1e6, "bar": 1e5, "atm": 101325.0},
    "temperature": {"K": 1.0},
    "angle": {"deg": 1.0},
    "mass": {"kg": 1.0, "zg": 1e-24},
    "count": {"": 1.0},
    "density": {"": 1.0},
    "mass_density": {"kg/m³": 1.0},
    "number_density": {"m⁻³": 1.0, "nm⁻³": 1e27},
}
DEFAULT_UNITS = {"length": "nm", "volume": "nm³", "time": "ns", "pressure": "kPa", "mass": "zg", "number_density": "nm⁻³"}

# quantity -> (kind, label)
QUANTITIES = {
    "time": ("time", "Time"),
    "volume": ("volume", "Droplet volume"),
    "r_eq": ("length", "Equivalent radius"),
    "r_norm": ("count", "Normalized equivalent radius r/r0"),
    "V_norm": ("count", "Normalized droplet volume V/V0"),
    "contact_angle": ("angle", "Contact angle"),
    "contact_radius": ("length", "Contact radius"),
    "n_liquid_cells": ("count", "Liquid cells"),
    "n_iso_points": ("count", "Iso-surface points"),
    "n_fit_points": ("count", "Fit points"),
    "z_base": ("length", "Substrate height z_base"),
    "droplet_height": ("length", "Droplet height"),
    "sphere_R": ("length", "Radius of curvature (fitted sphere)"),
    "box_L": ("length", "Droplet spacing L (periodic length)"),
    "sphere_zc": ("length", "Sphere centre z"),
    "fit_rms": ("length", "Fit RMS residual"),
    "temp_mean": ("temperature", "Mean droplet temperature"),
    "temp_n_cells": ("count", "Temperature cells"),
    "temp_volume": ("volume", "Temperature region volume"),
    "temp_N": ("count", "Temperature region molecules"),
    "gas_p": ("pressure", "Gas pressure estimate (ideal gas)"),
    "gas_T": ("temperature", "Gas temperature (N-weighted)"),
    "gas_N": ("count", "Gas molecules N"),
    "gas_rho": ("mass_density", "Gas density (mean over gas cells)"),
    "gas_ndens": ("number_density", "Gas number density"),
    "gas_volume": ("volume", "Gas region volume"),
    "gas_n_cells": ("count", "Gas cells"),
    "region_liquid_volume": ("volume", "Liquid region volume"),
    "region_margin_volume": ("volume", "Margin region volume"),
    "region_gas_volume": ("volume", "Gas region volume (phase map)"),
    "n_margin_cells": ("count", "Margin cells"),
    # summary
    "max_volume": ("volume", "Max volume"),
    "final_volume": ("volume", "Final volume"),
    "initial_volume": ("volume", "Initial volume"),
    "initial_r_eq": ("length", "Initial equivalent radius"),
    "evaporation_time": ("time", "Evaporation time"),
    "initial_time": ("time", "First time"),
    "final_time": ("time", "Last time"),
    "initial_angle": ("angle", "Initial contact angle"),
    "final_angle": ("angle", "Final valid contact angle"),
    "mean_angle": ("angle", "Mean contact angle"),
    "initial_contact_radius": ("length", "Initial contact radius"),
    "final_contact_radius": ("length", "Final valid contact radius"),
    "mean_temp": ("temperature", "Mean temperature"),
    "mean_gas_p": ("pressure", "Mean gas pressure estimate"),
    "mean_angle_sd": ("angle", "Contact angle SD over time"),
    "mean_temp_sd": ("temperature", "Temperature SD over time"),
    "mean_gas_p_sd": ("pressure", "Gas pressure SD over time"),
}


def unit_of(q: str, units: dict) -> tuple[str, float]:
    kind = QUANTITIES.get(q, ("count", q))[0]
    default = DEFAULT_UNITS.get(kind, next(iter(UNITS[kind])))
    u = (units or {}).get(kind, default)
    if u not in UNITS[kind]:  # unknown unit name: never label values with a unit they are not in
        u = default
    return u, UNITS[kind][u]


def convert(q: str, v, units: dict):
    if v is None or (isinstance(v, float) and not math.isfinite(v)):
        return None
    if not isinstance(v, (int, float)) or isinstance(v, bool):
        return v
    return v / unit_of(q, units)[1]


def header(q: str, units: dict) -> str:
    u = unit_of(q, units)[0]
    return f"{q} [{u}]" if u else q


def label(q: str, units: dict) -> str:
    u = unit_of(q, units)[0]
    name = QUANTITIES.get(q, ("", q))[1]
    return f"{name} [{u}]" if u else name


# short axis labels for figures (English, Japanese); others fall back to QUANTITIES
FIG_LABELS = {
    "time": ("Time", "時間"),
    "volume": ("Droplet volume", "液滴体積"),
    "r_eq": ("Equivalent radius", "等価半径"),
    "r_norm": ("Normalized radius $r/r_0$", "規格化等価半径 $r/r_0$"),
    "V_norm": ("Normalized volume $V/V_0$", "規格化体積 $V/V_0$"),
    "contact_angle": ("Contact angle", "接触角"),
    "contact_radius": ("Contact radius", "接触半径"),
    "temp_mean": ("Droplet temperature", "液滴温度"),
    "gas_p": ("Gas pressure", "気相圧力"),
    "gas_rho": ("Gas density", "気相密度"),
    "gas_ndens": ("Gas number density", "気相数密度"),
    "gas_N": ("Number of gas molecules", "気相分子数"),
    "gas_T": ("Gas temperature", "気相温度"),
    "gas_volume": ("Gas volume", "気相体積"),
    "n_liquid_cells": ("Liquid cells", "液相セル数"),
    "droplet_height": ("Droplet height", "液滴高さ"),
    "z_base": ("Substrate height", "基板高さ"),
    "sphere_R": ("Radius of curvature", "曲率半径"),
    "box_L": ("Droplet spacing L", "液滴間の距離 L"),
    "evaporation_time": ("Evaporation time", "蒸発時間"),
    "initial_angle": ("Initial contact angle", "初期接触角"),
    "mean_angle": ("Mean contact angle", "平均接触角"),
    "final_angle": ("Final contact angle", "最終接触角"),
    "initial_r_eq": ("Initial equivalent radius", "初期等価半径"),
    "initial_volume": ("Initial volume", "初期体積"),
    "max_volume": ("Maximum volume", "最大体積"),
    "mean_temp": ("Mean temperature", "平均温度"),
    "mean_gas_p": ("Mean gas pressure", "平均気相圧力"),
}
_WORDS = {"Case": "ケース", "Cases": "ケース数", "moving average": "移動平均", "points": "点", "mean": "平均"}


_SUP = {"⁻": "-", "¹": "1", "²": "2", "³": "3", "⁴": "4", "⁵": "5", "⁶": "6", "⁷": "7", "⁸": "8", "⁹": "9", "⁰": "0"}


def _sup(u: str) -> str:
    """Superscripts as mathtext (drawn in the text font; Arial has no superscript minus): nm⁻¹ -> nm$^{-1}$."""
    # only negative powers: ², ³ (and ¹) are in Latin-1 and present in every font
    return re.sub(r"⁻[¹²³⁴⁵⁶⁷⁸⁹⁰]+", lambda m: "$^{" + "".join(_SUP[c] for c in m.group(0)) + "}$", u)


def fig_label(q: str, units: dict, st: dict) -> str:
    """Axis label in the figure language with the unit as [u] or (u)."""
    ja = st.get("lang") == "ja"
    en_ja = FIG_LABELS.get(q)
    name = (en_ja[1] if ja else en_ja[0]) if en_ja else QUANTITIES.get(q, ("", q))[1]
    u = unit_of(q, units)[0] if q in QUANTITIES else ""
    if QUANTITIES.get(q, ("",))[0] == "angle":
        u = "°"
    elif QUANTITIES.get(q, ("",))[0] == "temperature":
        u = "K"
    if not u:
        return name
    u = _sup(u)
    return f"{name} ({u})" if st.get("unit_style") == "paren" else f"{name} [{u}]"


def word(st: dict, en: str) -> str:
    return _WORDS.get(en, en) if st.get("lang") == "ja" else en


# ------------------------------------------------------------------ CSV
def _csv(rows: list[dict], cols: list[str], units: dict) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow([header(c, units) if c in QUANTITIES else c for c in cols])
    for r in rows:
        out = []
        for c in cols:
            v = convert(c, r.get(c), units) if c in QUANTITIES else r.get(c)
            out.append("" if v is None or (isinstance(v, float) and not math.isfinite(v)) else
                       (f"{v:.9g}" if isinstance(v, float) else v))
        w.writerow(out)
    return buf.getvalue()


TS_COLS = ["case", "time", "time_name", "source", "status", "reason", "volume", "r_eq", "n_liquid_cells",
           "contact_angle", "contact_radius", "contact_reason", "n_iso_points", "n_fit_points", "z_base",
           "droplet_height", "sphere_R", "sphere_zc", "fit_rms", "region_liquid_volume", "region_margin_volume",
           "region_gas_volume", "n_margin_cells", "warnings"]
TEMP_COLS = ["case", "time", "temp_mean", "temp_n_cells", "temp_volume", "temp_N", "temp_excluded_empty", "temp_reason"]
GAS_COLS = ["case", "time", "gas_p", "gas_N", "gas_volume", "gas_ndens", "gas_rho", "gas_T", "gas_n_cells", "gas_N_source", "gas_reason", "note"]
SUM_COLS = ["case", "status", "n_times", "n_skipped", "initial_time", "final_time", "initial_volume", "max_volume",
            "final_volume", "initial_r_eq", "evaporation_time", "evaporated", "initial_angle", "final_angle",
            "mean_angle", "mean_angle_sd", "mean_angle_rows", "initial_contact_radius", "final_contact_radius", "mean_temp",
            "mean_temp_sd", "mean_gas_p", "mean_gas_p_sd"]
# across-case statistics (statistics.csv / the overview table)
STAT_QUANTITIES = ["evaporation_time", "initial_angle", "mean_angle", "final_angle", "initial_r_eq", "initial_volume",
                   "max_volume", "final_volume", "initial_contact_radius", "final_contact_radius", "mean_temp", "mean_gas_p",
                   "mean_angle_sd", "mean_temp_sd", "mean_gas_p_sd"]


def describe(values) -> dict:
    """n, mean, sample SD (n-1), standard error, min, median, max of the finite values."""
    v = np.array([x for x in values if x is not None and isinstance(x, (int, float)) and math.isfinite(x)], float)
    n = int(v.size)
    if not n:
        return {"n": 0, "mean": None, "sd": None, "se": None, "min": None, "median": None, "max": None}
    sd = float(v.std(ddof=1)) if n >= 2 else None
    return {"n": n, "mean": float(v.mean()), "sd": sd, "se": sd / math.sqrt(n) if sd is not None else None,
            "min": float(v.min()), "median": float(np.median(v)), "max": float(v.max())}


def statistics_rows(results: dict, names: list[str], units: dict) -> list[dict]:
    rows = []
    for q in STAT_QUANTITIES:
        vals = [convert(q, (results[n].get("summary") or {}).get(q), units) for n in names if n in results]
        d = describe(vals)
        rows.append({"quantity": q, "unit": unit_of(q, units)[0], "n_cases": d["n"], "mean": d["mean"], "sd": d["sd"],
                     "se": d["se"], "min": d["min"], "median": d["median"], "max": d["max"]})
    return rows


def build_csv_zip(results: dict[str, dict], names: list[str], units: dict, run_meta: dict,
                  models: dict | None = None) -> bytes:
    sel = [results[n] for n in names if n in results]
    meta_keys = sorted({k for r in sel for k in (r.get("meta") or {})})
    summary_rows, ts_rows, status_rows = [], [], []
    for r in sel:
        row = {"case": r["name"], "status": r["status"], **(r.get("summary") or {})}
        row.update({f"meta:{k}": v for k, v in (r.get("meta") or {}).items()})
        summary_rows.append(row)
        for t in r.get("rows", []):
            ts_rows.append({"case": r["name"], **t, "note": "ideal-gas estimate for the selected density range"})
        status_rows.append({"case": r["name"], "status": r["status"], "reason": r.get("reason", ""),
                            "errors": " | ".join(r.get("errors", [])),
                            "skipped_times": " ".join(s["time"] for s in r.get("skipped", [])),
                            "skip_reasons": " | ".join(sorted({s["reason"] for s in r.get("skipped", [])})),
                            "warnings": " | ".join((r.get("ctx") or {}).get("warnings", []) + (r.get("info") or {}).get("warnings", []))})
    files = {
        "summary.csv": _csv(summary_rows, SUM_COLS + [f"meta:{k}" for k in meta_keys], units),
        "timeseries.csv": _csv(ts_rows, TS_COLS, units),
        "temperature.csv": _csv(ts_rows, TEMP_COLS, units),
        "gas_pressure.csv": _csv(ts_rows, GAS_COLS, units),
        "statistics.csv": _csv(statistics_rows(results, [r["name"] for r in sel if r.get("rows")], units),
                               ["quantity", "unit", "n_cases", "mean", "sd", "se", "min", "median", "max"], {}),
        "status.csv": _csv(status_rows, ["case", "status", "reason", "errors", "skipped_times", "skip_reasons", "warnings"], units),
        "units.json": json.dumps({k: unit_of(k, units)[0] for k in QUANTITIES}, ensure_ascii=False, indent=1),
        "run_meta.json": json.dumps(run_meta, ensure_ascii=False, indent=1, default=str),
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for k, v in files.items():
            z.writestr(k, "﻿" + v if k.endswith(".csv") else v)  # BOM so Excel reads UTF-8
    return buf.getvalue()


# ------------------------------------------------------------------ plotting
THEMES = {
    "paper": {"bg": "white", "fg": "black", "grid": "#d0d0d0", "font": "DejaVu Sans"},
    "presentation": {"bg": "white", "fg": "#222222", "grid": "#e0e0e0", "font": "DejaVu Sans"},
    "dark": {"bg": "#1e1f22", "fg": "#e8e8e8", "grid": "#444444", "font": "DejaVu Sans"},
}


FORMATS = {"png": "image/png", "pdf": "application/pdf", "svg": "image/svg+xml", "tiff": "image/tiff",
           "eps": "application/postscript"}
FONTS = {
    "sans": ["Arial", "Helvetica", "Liberation Sans", "Nimbus Sans", "DejaVu Sans"],
    "serif": ["Times New Roman", "Times", "Liberation Serif", "Nimbus Roman", "DejaVu Serif"],
    "ja": ["Yu Gothic", "Meiryo", "BIZ UDPGothic", "Hiragino Sans", "Noto Sans JP", "Noto Sans CJK JP", "IPAexGothic",
           "MS Gothic", "Arial", "DejaVu Sans"],
}


# Series colours. Categorical palettes are used in their fixed order (colour follows the series, never cycled
# hues); ordered palettes are sampled evenly from light to dark for series with a natural order (e.g. radii).
PALETTES = {
    # the app's screen colours (validated for colour-vision deficiency on adjacent pairs)
    "standard": {"kind": "categorical", "colors": ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"]},
    # Okabe & Ito (2008): the classic colour-blind-safe set used in many journals
    "okabe_ito": {"kind": "categorical", "colors": ["#0072b2", "#d55e00", "#009e73", "#e69f00", "#cc79a7", "#56b4e9", "#000000", "#f0e442"]},
    "blues": {"kind": "ordered", "colors": ["#86b6ef", "#5598e7", "#2a78d6", "#1c5cab", "#104281", "#0d366b"]},
    "viridis": {"kind": "ordered", "colors": ["#440154", "#414487", "#2a788e", "#22a884", "#7ad151", "#bddf26"]},
    "warm": {"kind": "ordered", "colors": ["#f6b26b", "#eb6834", "#c7431b", "#9a2d10", "#6b1d0a"]},
    "mono": {"kind": "categorical", "colors": ["#000000", "#555555", "#8a8a8a", "#b5b5b5"]},
}
MARKERS = ["o", "s", "^", "D", "v", "P", "X", "*"]
DASHES = ["-", "--", "-.", ":"]
LEGEND_LOCS = {"best", "upper right", "upper left", "lower left", "lower right", "center right", "center left",
               "lower center", "upper center", "center"}


def palette(st: dict, n: int) -> list[str]:
    """n series colours from the style's palette ('custom': st['palette_colors'])."""
    name = st.get("palette") or "standard"
    if name == "custom":
        cols = [c for c in (st.get("palette_colors") or []) if isinstance(c, str) and c.startswith("#")]
        pal = {"kind": "categorical", "colors": cols or PALETTES["standard"]["colors"]}
    else:
        pal = PALETTES.get(name, PALETTES["standard"])
    cols = pal["colors"]
    if pal["kind"] == "ordered" and n > 0:
        if n == 1:
            return [cols[len(cols) // 2]]
        from matplotlib.colors import LinearSegmentedColormap
        cm = LinearSegmentedColormap.from_list("pal", cols)
        return [_hex(cm(i / (n - 1))) for i in range(n)]
    return [cols[i % len(cols)] for i in range(n)]


def _hex(c) -> str:
    from matplotlib.colors import to_hex
    return to_hex(c)


def _style(st: dict) -> dict:
    d = {"width": 6.0, "height": 4.0, "dpi": 200, "marker_size": 4.0, "line_width": 1.2, "font_size": 11,
         "label_size": None, "tick_size": None, "legend_size": None, "axes_width": 1.0, "format": "png",
         "lang": "en", "font": "sans", "unit_style": "bracket", "auto_title": False, "legend_frame": False,
         "theme": "paper", "title": "", "xlabel": "", "ylabel": "", "xlim": [None, None], "ylim": [None, None],
         "legend": True, "legend_loc": "best", "legend_ncol": 0, "grid": True, "palette": "standard",
         "palette_colors": [], "vary_markers": False, "legend_labels": {}, "series_colors": {}, "transparent": False,
         "mode": "markers", "single_color": "", "log_y": False, "log_x": False}
    d.update({k: v for k, v in (st or {}).items() if v is not None and v != ""})
    fs = float(d["font_size"])
    d["label_size"] = float(d["label_size"] or fs)
    d["tick_size"] = float(d["tick_size"] or fs * 0.9)
    d["legend_size"] = float(d["legend_size"] or fs * 0.8)
    if d["format"] not in FORMATS:
        d["format"] = "png"
    return d


_INSTALLED: set[str] | None = None


def _families(fam: str) -> list[str]:
    """Installed fonts for the figure language, followed by Japanese fonts as per-glyph fallback
    (e.g. a Japanese legend name in an English figure) and DejaVu Sans as the last resort."""
    global _INSTALLED
    if _INSTALLED is None:
        from matplotlib import font_manager
        # only families with a regular weight (some installs ship e.g. Noto Sans JP Thin alone)
        _INSTALLED = {f.name for f in font_manager.fontManager.ttflist if f.weight in (400, "normal", "regular")}
    names = FONTS[fam] + [f for f in FONTS["ja"] if fam != "ja"] + ["DejaVu Sans"]
    out = [n for n in dict.fromkeys(names) if n in _INSTALLED]
    return out or ["DejaVu Sans"]


def _apply_rc(st: dict) -> None:
    """Fonts and line weights for one figure. Fonts are embedded as TrueType (editable, accepted by journals)."""
    fam = "ja" if st.get("lang") == "ja" else ("serif" if st.get("font") == "serif" else "sans")
    aw = float(st.get("axes_width") or 1.0)
    plt.rcParams.update({
        "font.size": st["font_size"], "font.family": _families(fam),
        "font.sans-serif": FONTS["ja"] if fam == "ja" else FONTS["sans"], "font.serif": FONTS["serif"],
        "pdf.fonttype": 42, "ps.fonttype": 42, "svg.fonttype": "none",
        "axes.linewidth": aw, "xtick.major.width": aw, "ytick.major.width": aw,
        "xtick.minor.width": aw * 0.7, "ytick.minor.width": aw * 0.7,
        "xtick.major.size": 3.5 * max(1.0, aw), "ytick.major.size": 3.5 * max(1.0, aw),
        "axes.unicode_minus": True, "mathtext.default": "regular",
    })


def mime(st_or_fmt) -> str:
    fmt = st_or_fmt if isinstance(st_or_fmt, str) else _style(st_or_fmt)["format"]
    return FORMATS.get(fmt, "image/png")


def _new_fig(st: dict):
    th = THEMES.get(st["theme"], THEMES["paper"])
    _apply_rc(st)
    fig, ax = plt.subplots(figsize=(float(st["width"]), float(st["height"])), dpi=int(st["dpi"]))
    fig.patch.set_facecolor(th["bg"])
    ax.set_facecolor(th["bg"])
    for sp in ax.spines.values():
        sp.set_color(th["fg"])
    ax.tick_params(colors=th["fg"], which="both", direction="in", top=True, right=True, labelsize=st["tick_size"])
    ax.xaxis.label.set_color(th["fg"])
    ax.yaxis.label.set_color(th["fg"])
    ax.title.set_color(th["fg"])
    if st["grid"]:
        ax.grid(True, color=th["grid"], linewidth=0.6)
        ax.set_axisbelow(True)
    return fig, ax, th


def _finish(fig, ax, st, th, default_x, default_y, default_title, n_series, handles=None, report=None) -> bytes:
    ax.set_xlabel(st["xlabel"] or default_x, fontsize=st["label_size"])
    ax.set_ylabel(st["ylabel"] or default_y, fontsize=st["label_size"])
    title = st["title"] or (default_title if st.get("auto_title", True) else "")
    if title:
        ax.set_title(title, fontsize=st["label_size"] * 1.05)
    xl, yl = st["xlim"], st["ylim"]
    if xl and any(v not in (None, "") for v in xl):
        ax.set_xlim(*[None if v in (None, "") else float(v) for v in xl])
    if yl and any(v not in (None, "") for v in yl):
        ax.set_ylim(*[None if v in (None, "") else float(v) for v in yl])
    if st.get("log_y"):
        ax.set_yscale("log")
    if st.get("log_x"):
        ax.set_xscale("log")
    if st["legend"] and st["legend_loc"] != "none" and 0 < n_series <= 30 and ax.get_legend_handles_labels()[0]:
        ncol = int(st.get("legend_ncol") or 0) or (1 if n_series <= 10 else 2)
        loc = st["legend_loc"]
        kw = {"fontsize": st["legend_size"], "frameon": bool(st.get("legend_frame")), "ncol": ncol}
        if loc == "outside right":
            kw.update(loc="upper left", bbox_to_anchor=(1.01, 1.0), borderaxespad=0.0)
        elif loc == "outside top":
            kw.update(loc="lower left", bbox_to_anchor=(0.0, 1.01), borderaxespad=0.0, ncol=int(st.get("legend_ncol") or 0) or min(n_series, 4))
        else:
            kw["loc"] = loc if loc in LEGEND_LOCS else "best"
        leg = ax.legend(*handles, **kw) if handles and handles[0] else ax.legend(**kw)
        for t in leg.get_texts():
            t.set_color(th["fg"])
    fig.tight_layout(pad=0.4)
    keep_inside(fig)
    return _save(fig, st, report)


def keep_inside(fig, pad_pt: float = 2.0) -> None:
    """Shrink the axes area until every drawn text is inside the figure. tight_layout leaves e.g. the half of the
    last tick label that hangs over the axes end, or a two-line axis label, partly outside the file."""
    if len(fig.axes) != 1:
        return
    pad = pad_pt / 72.0
    W, H = fig.get_figwidth(), fig.get_figheight()
    for _ in range(3):
        r = fig.canvas.get_renderer()
        bb = fig.get_tightbbox(r)  # inches, everything drawn (ticks, labels, legend, title)
        over = {"left": pad - bb.x0, "right": bb.x1 - (W - pad), "bottom": pad - bb.y0, "top": bb.y1 - (H - pad)}
        if max(over.values()) <= 0.005:
            return
        sp = fig.subplotpars
        fig.subplots_adjust(left=sp.left + max(0.0, over["left"]) / W, right=sp.right - max(0.0, over["right"]) / W,
                            bottom=sp.bottom + max(0.0, over["bottom"]) / H, top=sp.top - max(0.0, over["top"]) / H)


def _save(fig, st, report: dict | None = None) -> bytes:
    """The figure at exactly width x height (no bbox cropping), in the requested format.
    report: a dict that receives the layout warnings of the figure ('lint'); its 'lint_opts' are passed on."""
    buf = io.BytesIO()
    fmt = st["format"]
    kw = {"pil_kwargs": {"compression": "tiff_lzw"}} if fmt == "tiff" else {}
    if fmt in ("png", "tiff"):
        kw["dpi"] = int(st["dpi"])
    if st.get("transparent") and fmt in ("png", "svg", "pdf", "tiff"):
        fig.patch.set_alpha(0.0)
        for ax in fig.axes:
            ax.patch.set_alpha(0.0)
        fig.savefig(buf, format=fmt, transparent=True, **kw)
    else:
        fig.savefig(buf, format=fmt, facecolor=fig.get_facecolor(), **kw)
    lint(fig, report)
    plt.close(fig)
    return buf.getvalue()


def lint(fig, report: dict | None) -> None:
    """Layout warnings of the drawn figure into report['lint'] (nothing without a report)."""
    if report is None:
        return
    from . import figlint
    fig.canvas.draw()  # the layout at the figure's dpi, whatever the file format
    try:
        report["lint"] = figlint.check(fig, **(report.get("lint_opts") or {}))
    except Exception as exc:  # a check must never cost the figure
        report["lint"] = [{"code": "lint_error", "message": f"layout check failed: {exc}"}]


def _colors(st, n):
    if n <= 1 and st.get("single_color"):
        return [st["single_color"]]
    return palette(st, max(n, 1))[:max(n, 1)]


def legend_name(st: dict, name: str) -> str:
    """The legend text of a series: the user's rename if any."""
    return (st.get("legend_labels") or {}).get(name) or name


def series_color(st: dict, key: str, default):
    return (st.get("series_colors") or {}).get(key) or default


def _marker(st: dict, i: int, default: str = "o") -> str:
    return MARKERS[i % len(MARKERS)] if st.get("vary_markers") else default


def _series_xy(rows, q, units):
    if q in ("r_norm", "V_norm") and rows and q not in rows[0]:  # results from before these existed
        from .analysis import derive_normalized
        derive_normalized(sorted(rows, key=lambda x: x.get("time", 0)))
    ok = [r for r in rows if r.get("status") == "ok"]
    x = np.array([convert("time", r["time"], units) for r in ok], float)
    y = np.array([np.nan if convert(q, r.get(q), units) is None else convert(q, r.get(q), units) for r in ok], float)
    return x, y


def in_window(rows: list[dict], window: dict | None) -> list[dict]:
    """Rows whose time lies in window {t0, t1} (seconds, bounds allow round-off); all rows without one."""
    if not window or window.get("t0") is None or window.get("t1") is None:
        return rows
    t0, t1 = float(window["t0"]), float(window["t1"])
    return [r for r in rows if t0 - 1e-9 * abs(t0) <= r["time"] <= t1 + 1e-9 * abs(t1)]


def moving_average(x: np.ndarray, y: np.ndarray, w: int) -> tuple[np.ndarray, np.ndarray]:
    """Centred moving average over the finite points (w points, shrinking at the ends)."""
    ok = np.isfinite(y)
    x, y = x[ok], y[ok]
    if not y.size:
        return x, y
    half = int(w) // 2
    c = np.concatenate([[0.0], np.cumsum(y)])
    i = np.arange(y.size)
    a, b = np.maximum(0, i - half), np.minimum(y.size, i + half + 1)
    return x, (c[b] - c[a]) / (b - a)


def plot_timeseries(results: dict, names: list[str], q: str, units: dict, style: dict, band: bool = False,
                    window: dict | None = None, moving_avg: int = 0, groups: list[int] | None = None,
                    report: dict | None = None) -> bytes:
    """Time series of q. window {t0, t1, crop}: crop=True draws only that range, otherwise the window is
    shaded; either way the window mean (± SD) is drawn as a dashed line. moving_avg: points (0 = none)."""
    st = _style(style)
    fig, ax, th = _new_fig(st)
    # colour slot per case: the screen's slot when given (colour follows the case), else the order
    gi = [int(g) for g in groups] if groups and len(groups) == len(names) else list(range(len(names)))
    pal = _colors(st, (max(gi) + 1) if len(names) > 1 else 1)
    crop = bool(window and window.get("crop", True))
    n_extra = 0
    for k, n in enumerate(names):
        c = series_color(st, n, pal[gi[k]] if len(names) > 1 else pal[0])
        rows = results[n].get("rows", [])
        x, y = _series_xy(in_window(rows, window) if crop else rows, q, units)
        kw = {"color": c, "label": legend_name(st, n) if len(names) > 1 else "_nolegend_"}  # one case: its name is not a legend
        faint = {"alpha": 0.4} if moving_avg else {}
        mk = _marker(st, gi[k])
        if st["mode"] in ("markers", "both"):
            ax.plot(x, y, mk if st["mode"] == "markers" else mk + "-", ms=st["marker_size"], lw=st["line_width"] if st["mode"] == "both" else 0, **kw, **faint)
        else:
            ax.plot(x, y, "-", lw=st["line_width"], **kw, **faint)
        if moving_avg and len(names) == 1:
            mx, my = moving_average(x, y, moving_avg)
            lab = f"{word(st, 'moving average')} ({moving_avg} {word(st, 'points')})"
            ax.plot(mx, my, "-", lw=max(1.5, st["line_width"] * 1.6), color=c, label=legend_name(st, lab))
            n_extra += 1
    if window and len(names) == 1:
        tu = unit_of("time", units)[1]
        x0, x1 = float(window["t0"]) / tu, float(window["t1"]) / tu
        _, wy = _series_xy(in_window(results[names[0]].get("rows", []), window), q, units)
        d = describe(wy)
        if not crop:
            ax.axvspan(x0, x1, color=th["fg"], alpha=0.08, lw=0)
        if d["n"]:
            sd = f" ± {d['sd']:.3g} (SD)" if d["sd"] is not None else ""
            ax.hlines(d["mean"], x0, x1, colors=th["fg"], linestyles="--", lw=max(1.0, st["line_width"]),
                      label=f"{word(st, 'mean')} {d['mean']:.4g}{sd}, n={d['n']}")  # numbers: not renamable
            n_extra += 1
    if band and len(names) > 1:
        grid_t, mean, sd = mean_band(results, names, q, units)
        if grid_t.size:
            ax.plot(grid_t, mean, color=th["fg"], lw=2, label=legend_name(st, word(st, "mean")))
            ax.fill_between(grid_t, mean - sd, mean + sd, color=th["fg"], alpha=0.15, label=legend_name(st, "±1 SD (n-1)"))
            n_extra += 2
    title = names[0] if len(names) == 1 else ""
    if window and title and not st["title"]:
        tu, un = unit_of("time", units)[1], unit_of("time", units)[0]
        title = f"{title}  (t = {float(window['t0']) / tu:.4g}–{float(window['t1']) / tu:.4g} {un})"
    return _finish(fig, ax, st, th, fig_label("time", units, st), fig_label(q, units, st), title, len(names) + n_extra,
                   report=report)


def mean_band(results, names, q, units):
    """Mean and std across cases at common time names."""
    table: dict[float, list] = {}
    for n in names:
        x, y = _series_xy(results[n].get("rows", []), q, units)
        for xi, yi in zip(x, y):
            if math.isfinite(yi):
                table.setdefault(round(xi, 9), []).append(yi)
    ts = sorted(t for t, v in table.items() if len(v) >= 2)
    if not ts:
        return np.array([]), np.array([]), np.array([])
    return (np.array(ts), np.array([np.mean(table[t]) for t in ts]), np.array([np.std(table[t], ddof=1) for t in ts]))


def plot_summary_bar(results: dict, names: list[str], q: str, units: dict, style: dict) -> bytes:
    st = _style(style)
    fig, ax, th = _new_fig(st)
    vals = [convert(q, (results[n].get("summary") or {}).get(q), units) for n in names]
    y = [np.nan if v is None else v for v in vals]
    ax.bar(range(len(names)), y, color=_colors(st, 1)[0])
    ax.set_xticks(range(len(names)))
    ax.set_xticklabels(names, rotation=90, fontsize=max(5, st["font_size"] * (0.8 if len(names) < 30 else 0.45)))
    for i, v in enumerate(vals):
        if v is None:
            ax.text(i, 0, "n/a", rotation=90, ha="center", va="bottom", fontsize=6, color=th["fg"])
    st["legend"] = False
    return _finish(fig, ax, st, th, word(st, "Case"), fig_label(q, units, st), "", 0)


def plot_summary_hist(results: dict, names: list[str], q: str, units: dict, style: dict, bins: int = 20) -> bytes:
    st = _style(style)
    fig, ax, th = _new_fig(st)
    vals = [convert(q, (results[n].get("summary") or {}).get(q), units) for n in names]
    v = np.array([x for x in vals if x is not None], float)
    if v.size:
        ax.hist(v, bins=bins, color=_colors(st, 1)[0], edgecolor=th["bg"])
        d = describe(v)
        label_sd = f" ± {d['sd']:.3g} (SD)" if d["sd"] is not None else ""
        ax.axvline(d["mean"], color=th["fg"], ls="--", lw=1, label=f"mean {d['mean']:.4g}{label_sd}, n={d['n']}")
        if d["sd"] is not None:
            ax.axvspan(d["mean"] - d["sd"], d["mean"] + d["sd"], color=th["fg"], alpha=0.08)
    return _finish(fig, ax, st, th, fig_label(q, units, st), word(st, "Cases"), "", 1)


def axis_label(q: str | None, units: dict, st: dict, form: str = "") -> str:
    """Figure label of a quantity, optionally as 1/q ('inv') or ln q ('ln')."""
    if not q:
        return ""
    base = fig_label(q, units, st)
    if form not in ("inv", "ln"):
        return base
    ja = st.get("lang") == "ja"
    en_ja = FIG_LABELS.get(q)
    name = (en_ja[1] if ja else en_ja[0]) if en_ja else QUANTITIES.get(q, ("", q))[1]
    u = base[len(name):].strip().strip("[]()")
    if form == "ln":
        return f"ln({name} / {u})" if u else f"ln({name})"
    inv = f"{u}$^{{-1}}$" if u and " " not in u and "$" not in u else (f"1/{u}" if u else "")
    lab = f"1 / {name}"
    return (f"{lab} ({inv})" if st.get("unit_style") == "paren" else f"{lab} [{inv}]") if inv else lab


def plot_xy(series: list[dict], style: dict, xlabel: str, ylabel: str, title: str = "", *, units: dict | None = None,
            xq: str | None = None, yq: str | None = None, xform: str = "", yform: str = "", xticks: dict | None = None,
            report: dict | None = None) -> bytes:
    """Generic: series = [{x, y, name, mode('markers'|'lines'|'bar'|'errorbar'|'band'), group?, key?, color?, yerr?,
    y2? (band: lower/upper = y/y2), dash?, alpha?, lw?, ms?, order?}].
    group: the series' identity (one colour of the palette per group, the same in every plot of a comparison);
    key: the name for per-series colour overrides (default: name). An explicit color is kept as it is (fit lines,
    references). Empty axis labels are built from xq / yq (figure language and unit style of the style)."""
    st = _style(style)
    fig, ax, th = _new_fig(st)
    groups = [s["group"] for s in series if isinstance(s.get("group"), int)]
    gpal = _colors(st, max(groups) + 1) if groups else []
    if groups and max(groups) == 0:  # one identity: the palette's first colour, not the single-series colour
        gpal = palette(st, 1)
    cols = _colors(st, len(series))
    n_leg = 0
    hs, ls = [], []  # legend in the order the series are given (data first, then fits), not matplotlib's by type
    for k, (c, s) in enumerate(zip(cols, series)):
        g = s.get("group") if isinstance(s.get("group"), int) else None
        col = s.get("color") or (gpal[g] if g is not None else c)
        if s.get("color") is None:
            col = series_color(st, s.get("key") or s.get("name") or "", col)
        name = s.get("name") or ""
        lab = legend_name(st, name) if name and not name.startswith("_") else "_nolegend_"
        n_leg += not lab.startswith("_")
        alpha = s.get("alpha", 1.0)
        mk = s.get("marker") or _marker(st, g if g is not None else k)
        z = s.get("order")
        x = np.array([np.nan if v is None else v for v in s["x"]], float)
        y = np.array([np.nan if v is None else v for v in s["y"]], float)
        if s.get("mode") == "lines":
            dash = s.get("dash", "-")
            if st.get("vary_markers") and g is not None and dash == "-":
                dash = DASHES[g % len(DASHES)]
            hd = ax.plot(x, y, lw=st["line_width"] * s.get("lw", 1.0), color=col, label=lab, ls=dash, alpha=alpha, zorder=z)[0]
        elif s.get("mode") == "band":
            y2 = np.array([np.nan if v is None else v for v in (s.get("y2") or [None] * len(y))], float)
            hd = ax.fill_between(x, y, y2, color=col, alpha=s.get("alpha", 0.18), lw=0, label=lab, zorder=z)
        elif s.get("mode") == "bar":
            hd = ax.bar(x, y, width=s.get("width", 0.08), color=col, label=lab, alpha=0.7)
        elif s.get("mode") == "errorbar":
            yerr = np.array([np.nan if v is None else v for v in (s.get("yerr") or [None] * len(y))], float)
            hd = ax.errorbar(x, y, yerr=yerr, fmt=mk, ms=st["marker_size"] * s.get("ms", 1.0), color=col, ecolor=col,
                             elinewidth=max(0.6, st["line_width"] * 0.8), capsize=st["marker_size"] * 0.8, label=lab, alpha=alpha,
                             zorder=z if z is not None else 3)
        else:
            hd = ax.plot(x, y, mk, ms=st["marker_size"] * s.get("ms", 1.0), color=col, label=lab, alpha=alpha,
                         mec="none" if alpha < 1 else None, ls="none", zorder=z)[0]
        if not lab.startswith("_"):
            hs.append(hd)
            ls.append(lab)
    units = units or {}
    if xticks and xticks.get("values"):  # categorical x (e.g. one column of runs per condition)
        ax.set_xticks(xticks["values"], [legend_name(st, str(v)) for v in xticks.get("labels") or xticks["values"]])
        ax.tick_params(axis="x", which="both", top=False)
        ax.minorticks_off()
        ax.set_xlim(min(xticks["values"]) - 0.6, max(xticks["values"]) + 0.6)
        ax.grid(False, axis="x")
    return _finish(fig, ax, st, th, xlabel or axis_label(xq, units, st, xform), ylabel or axis_label(yq, units, st, yform),
                   title, n_leg, handles=(hs, ls), report=report)


def xy_zip(plots: list[dict], style: dict, units: dict) -> bytes:
    """Several xy plots in one ZIP; file names from each plot's 'filename' (extension from the format)."""
    buf = io.BytesIO()
    ext = _style(style)["format"]
    used: set[str] = set()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for n, p in enumerate(plots):
            base = safe_name(p.get("filename") or f"plot{n + 1}")
            fname, k = f"{base}.{ext}", 2
            while fname in used:
                fname, k = f"{base}_{k}.{ext}", k + 1
            used.add(fname)
            z.writestr(fname, plot_xy(p.get("series") or [], style, p.get("xlabel", ""), p.get("ylabel", ""), p.get("title", ""),
                                      units=units, xq=p.get("xq"), yq=p.get("yq"), xform=p.get("xform", ""), yform=p.get("yform", ""),
                                      xticks=p.get("xticks")))
    return buf.getvalue()


def safe_name(s: str) -> str:
    return re.sub(r"[^\w.\-]+", "_", s).strip("_") or "x"


def batch_zip(results: dict, names: list[str], quantities: list[str], units: dict, style: dict,
              window: dict | None = None, moving_avg: int = 0) -> bytes:
    """One PNG per case x quantity: '<case>__<quantity>.png'. Duplicate names get a numeric suffix."""
    buf = io.BytesIO()
    used: set[str] = set()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for n in names:
            for q in quantities:
                ext = _style(style)["format"]
                fname = f"{safe_name(n)}__{safe_name(q)}.{ext}"
                k = 2
                while fname in used:
                    fname = f"{safe_name(n)}__{safe_name(q)}_{k}.{ext}"
                    k += 1
                used.add(fname)
                z.writestr(fname, plot_timeseries(results, [n], q, units, style, window=window, moving_avg=moving_avg))
    return buf.getvalue()


# ------------------------------------------------------------------ visualization rendering
def render_frame(fr: dict, opts: dict, units: dict, style: dict, report: dict | None = None) -> Image.Image:
    st = _style({"width": 6, "height": 5, "dpi": 120, **(style or {})})
    lu, lf = unit_of("r_eq", units)
    view = opts.get("view", "2d")
    th = THEMES.get(st["theme"], THEMES["paper"])
    _apply_rc(st)
    fig = plt.figure(figsize=(float(st["width"]), float(st["height"])), dpi=int(st["dpi"]))
    fig.patch.set_facecolor(th["bg"])
    iso = np.array(fr["iso"]).reshape(-1, 3) / lf
    fm = np.array(fr.get("fit_mask") or [False] * len(iso), bool)
    c = fr["contact"]
    ms = st["marker_size"]
    if view == "slice" and "grid" in fr:
        _render_slice(fig, fr, opts, st, th, lf, lu, ms)
        ax = fig.axes[0]
    elif view == "3d":
        ax = fig.add_subplot(111, projection="3d")
        ax.set_facecolor(th["bg"])
        if opts.get("show_cells", True) and "cells" in fr:
            for key, col in (("liquid", "#1f77b4"), ("margin", "#ff7f0e"), ("gas_occupied", "#2ca02c")):
                if opts.get(f"show_{key}", key != "gas_occupied"):
                    p = np.array(fr["cells"][key]).reshape(-1, 3) / lf
                    ax.scatter(p[:, 0], p[:, 1], p[:, 2], s=ms, c=col, alpha=0.35, label=key, depthshade=False)
        if opts.get("show_iso", True) and len(iso):
            # into the box like the cells (the iso points are unwrapped around the droplet for the fit)
            per = np.array([(fr.get("periods") or {}).get(a) or 0.0 for a in "xyz"], float) / lf
            iso = _wrap_box(iso, np.array(fr["bounds_min"], float) / lf, per)
            ax.scatter(iso[:, 0], iso[:, 1], iso[:, 2], s=ms, c="#d62728", label="iso", depthshade=False)
        if opts.get("show_molecules") and "pos" in (fr.get("molecules") or {}):
            p = np.array(fr["molecules"]["pos"]).reshape(-1, 3) / lf
            t = np.array(fr["molecules"]["type"])
            palette = ("#17becf", "#7f7f7f", "#e377c2", "#bcbd22", "#9467bd")
            for index, tid in enumerate(np.unique(t)):
                col = palette[index % len(palette)]
                m = t == tid
                ax.scatter(p[m, 0], p[m, 1], p[m, 2], s=ms * 0.5, c=col, alpha=0.5, label=f"type {tid}", depthshade=False)
        bmin = np.array(fr["bounds_min"]) / lf
        bmax = np.array(fr["bounds_max"]) / lf
        ax.set_xlim(bmin[0], bmax[0]); ax.set_ylim(bmin[1], bmax[1]); ax.set_zlim(bmin[2], bmax[2])
        ax.set_box_aspect(bmax - bmin)
        ax.view_init(elev=float(opts.get("elev", 20)), azim=float(opts.get("azim", -60)))
        ax.set_xlabel(f"x [{lu}]"); ax.set_ylabel(f"y [{lu}]"); ax.set_zlabel(f"z [{lu}]")
    else:
        ax = fig.add_subplot(111)
        ax.set_facecolor(th["bg"])
        proj = opts.get("projection", "xz")
        i, j = "xyz".index(proj[0]), "xyz".index(proj[1])
        # periodic directions: draw everything inside the box, so the axes can stay fixed over time
        # (a droplet crossing the boundary shows up on both sides, as it is in the simulation)
        blo = np.array(fr["bounds_min"], float) / lf
        bhi = np.array(fr["bounds_max"], float) / lf
        per = np.array([(fr.get("periods") or {}).get(a) or 0.0 for a in "xyz"], float) / lf
        iso = _wrap_box(iso, blo, per)
        if "cells" in fr:
            for key, col in (("liquid", "#1f77b4"), ("margin", "#ff7f0e"), ("gas_occupied", "#2ca02c")):
                if opts.get(f"show_{key}"):
                    p = np.array(fr["cells"][key]).reshape(-1, 3) / lf
                    ax.plot(p[:, i], p[:, j], "s", ms=ms * 1.5, color=col, alpha=0.25, label=key, mec="none")
        if len(iso):
            ax.plot(iso[~fm, i], iso[~fm, j], "o", ms=ms, color="#9e9e9e", label="iso points")
            ax.plot(iso[fm, i], iso[fm, j], "o", ms=ms, color="#d62728", label="fit points")
        if c["R"] is not None:
            cen = _wrap_box(np.array([[c["xc"], c["yc"], c["zc"]]], float) / lf, blo, per)[0]
            R = c["R"] / lf
            ang = np.linspace(0, 2 * np.pi, 361)
            for n, (si, sj) in enumerate(_images(per[i], per[j])):  # periodic copies near the box edges
                lab = (lambda s: s if n == 0 else "_nolegend_")
                if proj == "xy":
                    zb = c["z_base"] / lf
                    rr = math.sqrt(max(R * R - (zb - cen[2]) ** 2, 0))
                    ax.plot(cen[0] + si + R * np.cos(ang), cen[1] + sj + R * np.sin(ang), "-", color=th["fg"], lw=1, label=lab("sphere (equator)"))
                    ax.plot(cen[0] + si + rr * np.cos(ang), cen[1] + sj + rr * np.sin(ang), "--", color="#2ca02c", lw=1.2, label=lab("contact line"))
                else:
                    ax.plot(cen[i] + si + R * np.cos(ang), cen[j] + sj + R * np.sin(ang), "-", color=th["fg"], lw=1, label=lab("fitted sphere"))
        if c["z_base"] is not None and proj != "xy":
            zb = c["z_base"] / lf
            ax.axhline(zb, color="#8c564b", lw=1.2, label="z_base")
            for k, ls in (("fit_lower_z", ":"), ("fit_upper_z", ":")):
                if c.get(k) is not None:
                    ax.axhline(c[k] / lf, color="#9467bd", lw=0.8, ls=ls)
            if c["contact_radius"] is not None and c["xc"] is not None:
                a = c["contact_radius"] / lf
                ctr = _wrap_box(np.array([[c["xc"], c["yc"], c["zc"]]], float) / lf, blo, per)[0][i]
                for si, _ in _images(per[i], 0.0):
                    ax.plot([ctr + si - a, ctr + si + a], [zb, zb], "s", color="#2ca02c", ms=ms + 2, label="contact" if si == 0 else "_nolegend_")
        ax.set_xlim(blo[i], bhi[i])
        ax.set_ylim(blo[j], bhi[j])
        ax.set_aspect("equal", adjustable="box")
        ax.set_xlabel(f"{proj[0]} [{lu}]"); ax.set_ylabel(f"{proj[1]} [{lu}]")
        if st["grid"]:
            ax.grid(True, color=th["grid"], lw=0.5)
        if st["legend"]:
            ax.legend(fontsize=st["font_size"] * 0.7, frameon=False, loc="upper right")
    ang_txt = f"θ={c['angle_deg']:.1f}°" if c["angle_deg"] is not None else f"θ: n/a ({c['reason']})"
    tu, tf = unit_of("time", units)
    if not opts.get("no_title"):
        ax.set_title(st["title"] or f"{fr['case']}  t={fr['time'] / tf:.4g} {tu}  {ang_txt}", color=th["fg"],
                     fontsize=st["font_size"])
    if opts.get("fixed_layout"):
        # animations: identical margins in every frame (tight_layout would shift the axes a little)
        if view != "3d":
            fig.subplots_adjust(left=0.13, right=0.97 if len(fig.axes) == 1 else 0.9, bottom=0.11 + _legend_room(fig), top=0.91)
    else:
        fig.tight_layout(rect=(0, _legend_room(fig), 1, 1))
    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=fig.get_facecolor())
    lint(fig, report)
    plt.close(fig)
    buf.seek(0)
    return Image.open(buf).convert("RGB")


def _wrap_box(p: np.ndarray, lo: np.ndarray, per: np.ndarray) -> np.ndarray:
    """Points wrapped into [lo, lo + period) along the periodic axes (period 0 = not periodic)."""
    p = np.array(p, float).reshape(-1, 3)
    return np.where(per > 0, lo + np.mod(p - lo, np.where(per > 0, per, 1.0)), p)


def _images(pi: float, pj: float) -> list[tuple[float, float]]:
    """Shifts of the periodic copies that can reach into the box (the original first)."""
    si = [0.0, -pi, pi] if pi else [0.0]
    sj = [0.0, -pj, pj] if pj else [0.0]
    return [(a, b) for a in si for b in sj]


PHASE_COLORS = ["#c9c8c1", "#f0efec", "#1baf7a", "#eda100", "#2a78d6"]  # outside, below gas, gas, margin, liquid
PHASE_NAMES = ["outside zones", "below gas min", "gas", "margin (excluded)", "liquid"]


def _render_slice(fig, fr, opts, st, th, lf, lu, ms):
    from matplotlib.colors import BoundaryNorm, ListedColormap, LogNorm
    from matplotlib.patches import Patch

    from .viz import slice_grid
    from .viz import best_layer
    plane = opts.get("projection", "xz")
    pos = opts.get("slice_pos")
    if pos is None and opts.get("slice_follow"):  # follow the droplet: its widest cut in this frame
        pos = best_layer(fr["grid"], 3 - "xyz".index(plane[0]) - "xyz".index(plane[1]))
    val, xs, ys, ph, rh = slice_grid(fr["grid"], plane, pos)
    i, j = "xyz".index(plane[0]), "xyz".index(plane[1])
    k = 3 - i - j

    def edges(c):
        if c.size == 1:
            return np.array([c[0] - 0.5, c[0] + 0.5])
        m = 0.5 * (c[1:] + c[:-1])
        return np.concatenate([[2 * c[0] - m[0]], m, [2 * c[-1] - m[-1]]])

    ex, ey = edges(xs) / lf, edges(ys) / lf
    ax = fig.add_subplot(111)
    ax.set_facecolor(th["bg"])
    thr = fr["grid"]["thresholds"]
    if opts.get("slice_color", "phase") == "rho":
        floor = 1e-3 * max(thr["liquid_min"], 1e-30)
        top = float(opts.get("rho_max") or 0) or 2.4 * thr["liquid_min"]  # fixed over time (case-wide scale)
        z = np.where(np.isfinite(rh), np.clip(rh, floor, top), np.nan)
        m = ax.pcolormesh(ex, ey, np.ma.masked_invalid(z), cmap="Blues", norm=LogNorm(vmin=floor, vmax=top), shading="flat")
        cb = fig.colorbar(m, ax=ax, fraction=0.04, pad=0.02)
        kind = fr["grid"].get("density_kind", "mass")
        name = "密度 ρ" if st.get("lang") == "ja" else "Density ρ"
        unit = "kg/m³" if kind == "mass" else ("m⁻³" if kind == "number" else "")
        cb.set_label(f"{name} ({unit})" if unit and st.get("unit_style") == "paren" else f"{name} [{unit}]" if unit else name)
        cxs, cys = xs / lf, ys / lf
        ja = st.get("lang") == "ja"
        du = f" {unit}" if unit else ""
        lines = [(rh, thr["liquid_min"], "#d03b3b",
                  f"液相（ρ ≥ {thr['liquid_min']:g}{du}）" if ja else f"liquid (ρ ≥ {thr['liquid_min']:g}{du})")]
        if thr.get("gas_mode") == "distance":  # the gas region the pressure is taken from: its boundary
            d, hgt = thr.get("gas_min_distance"), thr.get("gas_min_height")
            parts = []
            if d:
                parts.append(f"液相から {d / lf:.3g} {lu} 以上" if ja else f"≥ {d / lf:.3g} {lu} from the liquid")
            if hgt:
                parts.append(f"基板から {hgt / lf:.3g} {lu} 以上" if ja else f"≥ {hgt / lf:.3g} {lu} above the substrate")
            name = "気相領域の境界" if ja else "gas region boundary"
            lines.append((np.where(np.isfinite(ph), (ph == 2).astype(float), 0.0), 0.5, "#1baf7a",
                          f"{name}（{'，'.join(parts)}）" if ja and parts else f"{name}\n({', '.join(parts)})" if parts else name))
        else:
            lines.append((rh, thr["gas_max"], "#1baf7a", f"気相の上限（ρ = {thr['gas_max']:g}{du}）" if ja
                          else f"gas limit (ρ = {thr['gas_max']:g}{du})"))
        for field, level, col, name in lines:
            if np.nanmin(field) < level < np.nanmax(field):
                ax.contour(cxs, cys, np.nan_to_num(field, nan=0.0), levels=[level], colors=[col], linewidths=1.5)
                ax.plot([], [], color=col, lw=1.5, label=name)
    else:
        cmap = ListedColormap(PHASE_COLORS)
        m = ax.pcolormesh(ex, ey, np.ma.masked_invalid(ph), cmap=cmap, norm=BoundaryNorm(np.arange(-0.5, 5.5), 5), shading="flat")
        present = sorted({int(v) for v in ph[np.isfinite(ph)]})
        names = list(PHASE_NAMES)
        if fr["grid"].get("zones_outside"):
            names[0] = "outside zones (" + ", ".join(fr["grid"]["zones_outside"]) + ")"
        handles = [Patch(color=PHASE_COLORS[c], label=names[c]) for c in present]
        leg = ax.legend(handles=handles, fontsize=st["font_size"] * 0.7, frameon=True, loc="upper right", framealpha=0.9)
        leg.get_frame().set_facecolor(th["bg"])
        leg.get_frame().set_edgecolor(th["grid"])
    c = fr["contact"]
    lo = np.array(fr["bounds_min"], float)
    per = np.array([(fr.get("periods") or {}).get(a) or 0.0 for a in "xyz"], float)

    def wrap(p):  # fitted geometry is in unwrapped coordinates: bring it back into the periodic box
        p = np.array(p, float)
        return np.where(per > 0, lo + np.mod(p - lo, np.where(per > 0, per, 1.0)), p)

    if opts.get("show_iso", True) and fr.get("iso"):
        iso = wrap(np.array(fr["iso"]).reshape(-1, 3))
        h = (xs[1] - xs[0]) if xs.size > 1 else 1e-9
        near = np.abs(iso[:, k] - val) <= 0.75 * h
        ax.plot(iso[near, i] / lf, iso[near, j] / lf, ".", color=th["fg"], ms=ms * 0.8, label="iso points (near slice)")
    ja = st.get("lang") == "ja"
    circ = slice_circle(c, val, k, lo, per)
    if circ is not None:
        cen, r = circ
        ang = np.linspace(0, 2 * np.pi, 361)
        name = "フィットした球" if ja else "fitted sphere"
        for n, (si, sj) in enumerate(_images(per[i], per[j])):  # periodic copies near the edges
            ax.plot((cen[i] + si + r * np.cos(ang)) / lf, (cen[j] + sj + r * np.sin(ang)) / lf, "-", color=th["fg"], lw=1,
                    label=name if n == 0 else "_nolegend_")
    if c.get("z_base") is not None and plane != "xy":
        ax.axhline(c["z_base"] / lf, color="#8c564b", lw=1.2, label=r"液滴の底面 $z_\mathrm{base}$" if ja else r"droplet base $z_\mathrm{base}$")
    if opts.get("slice_color", "phase") == "rho" and opts.get("slice_legend", True):
        # under the plot, for the whole figure width: the section itself stays uncovered
        h, lab = ax.get_legend_handles_labels()
        leg = fig.legend(h, lab, loc="lower center", ncol=1, fontsize=st["font_size"] * 0.7, frameon=False, handlelength=1.6)
        for tx in leg.get_texts():
            tx.set_color(th["fg"])
        fig._bottom_legend = leg  # render_frame keeps room for it
    ax.set_xlim(ex[0], ex[-1])  # the mesh extent: fixed over time (the fitted circle may reach outside it)
    ax.set_ylim(ey[0], ey[-1])
    ax.set_aspect("equal", adjustable="box")
    ax.set_xlabel(f"{plane[0]} [{lu}]")
    ax.set_ylabel(f"{plane[1]} [{lu}]")
    # pos: where the section is | R: the fitted radius | none | text: slice_text as written (e.g. the set radius)
    note = opts.get("slice_note", "pos")
    text = (f"{'xyz'[k]} = {val / lf:.3g} {lu}" if note == "pos" else
            f"R = {c['R'] / lf:.3g} {lu}" if note == "R" and c.get("R") is not None else
            str(opts.get("slice_text") or "") if note == "text" else "")
    if text:
        ax.text(0.01, 0.99, text, transform=ax.transAxes, va="top", fontsize=st["font_size"] * 0.75, color=th["fg"])


def _legend_room(fig) -> float:
    """Figure fraction taken by a legend placed under the plot (0 without one)."""
    leg = getattr(fig, "_bottom_legend", None)
    if leg is None:
        return 0.0
    fig.canvas.draw()
    return min(0.5, leg.get_window_extent().height / fig.bbox.height + 0.01)


def slice_circle(c: dict, val: float, k: int, lo: np.ndarray, per: np.ndarray) -> tuple[np.ndarray, float] | None:
    """Where the fitted sphere cuts the section at coordinate `val` along axis k: (centre wrapped into the box,
    circle radius), or None. Across a periodic wall the nearest image of the centre counts."""
    if c.get("R") is None or c.get("xc") is None:
        return None
    p = np.array([c["xc"], c["yc"], c["zc"]], float)
    cen = np.where(per > 0, lo + np.mod(p - lo, np.where(per > 0, per, 1.0)), p)
    d = val - cen[k]
    if per[k] > 0:
        d = (d + 0.5 * per[k]) % per[k] - 0.5 * per[k]
    r2 = c["R"] ** 2 - d ** 2
    return (cen, float(np.sqrt(r2))) if r2 > 0 else None


def frames_to_gif(images: list[Image.Image], fps: float) -> bytes:
    buf = io.BytesIO()
    if not images:
        return b""
    w, h = images[0].size
    imgs = [im if im.size == (w, h) else im.resize((w, h)) for im in images]
    imgs[0].save(buf, format="GIF", save_all=True, append_images=imgs[1:], duration=int(1000 / max(fps, 0.1)), loop=0)
    return buf.getvalue()


def image_png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
