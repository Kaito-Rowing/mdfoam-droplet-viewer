"""Layout checks of a drawn matplotlib figure (run at render time, reported as warnings).

  clipped_text    text (axis labels, tick labels, legend, notes) reaching outside the figure: cut off in the file
  legend_overlap  a legend inside the axes covering data: markers, line segments, bands, error bars, contours
  small_font      text below a minimum size at the size the figure is printed / shown
  tick_overlap    neighbouring tick labels running into each other

Every warning is {"code", "message", ...details}; messages say what to change in the figure spec.
The figure must have been drawn at its output dpi (call after savefig, before closing it).
"""

from __future__ import annotations

import numpy as np
from matplotlib.collections import Collection, PathCollection, QuadMesh
from matplotlib.text import Text
from matplotlib.transforms import Bbox

MIN_FONT_PT = 7.0     # common journal minimum at print size (most ask for 6-8 pt)
TOL_PX = 0.5          # clipping tolerance in pixels (anti-aliasing)
# legend positions tried when looking for a free place (matplotlib's codes)
LOCS = ["upper right", "upper left", "lower left", "lower right", "center left", "center right", "lower center", "upper center"]


def _txt(t: Text) -> str:
    return " ".join(t.get_text().split())


def _visible_texts(fig) -> list[tuple[str, Text]]:
    """(role, text) of every visible, non-empty text that is drawn."""
    out: list[tuple[str, Text]] = []
    seen: set[int] = set()

    def add(role, t):
        if t is None or id(t) in seen or not t.get_visible() or not _txt(t):
            return
        seen.add(id(t))
        out.append((role, t))

    for ax in fig.axes:
        if not ax.get_visible():
            continue
        for t in (ax.title, getattr(ax, "_left_title", None), getattr(ax, "_right_title", None)):
            add("title", t)
        add("x label", ax.xaxis.label)
        add("y label", ax.yaxis.label)
        for name, axis in (("x tick label", ax.xaxis), ("y tick label", ax.yaxis)):
            for t in _tick_labels(axis):
                add(name, t)
        for t in ax.texts:
            add("note", t)
        leg = ax.get_legend()
        if leg is not None and leg.get_visible():
            for t in leg.get_texts():
                add("legend", t)
    for leg in fig.legends:
        for t in leg.get_texts():
            add("legend", t)
    for t in fig.texts:
        add("note", t)
    return out


def _tick_labels(axis) -> list[Text]:
    """Tick labels that are actually drawn (inside the view limits)."""
    try:
        ticks = axis._update_ticks()  # the ticks matplotlib draws (private, but stable since 3.0)
    except Exception:
        return [t for t in axis.get_ticklabels() if t.get_visible()]
    out = []
    for tk in ticks:
        for lab in (tk.label1, tk.label2):
            if lab.get_visible() and _txt(lab):
                out.append(lab)
    return out


def _mm(px: float, dpi: float) -> float:
    return round(px / dpi * 25.4, 2)


def check(fig, *, min_font_pt: float | None = None, print_width_cm: float | None = None) -> list[dict]:
    """All layout warnings of the drawn figure. print_width_cm: the width it will be shown at (fonts scale with
    it); default the figure's own width."""
    r = fig.canvas.get_renderer()
    dpi = float(fig.dpi)
    W, H = fig.bbox.width, fig.bbox.height
    fig_cm = fig.get_figwidth() * 2.54
    scale = (float(print_width_cm) / fig_cm) if print_width_cm else 1.0
    mfont = float(min_font_pt if min_font_pt is not None else MIN_FONT_PT)
    texts = _visible_texts(fig)
    warns: list[dict] = []
    # -- text outside the figure
    for role, t in texts:
        bb = t.get_window_extent(r)
        if bb.width <= 0 or bb.height <= 0:
            continue
        cut = {"left": -bb.x0, "right": bb.x1 - W, "bottom": -bb.y0, "top": bb.y1 - H}
        cut = {k: v for k, v in cut.items() if v > TOL_PX}
        if cut:
            side, px = max(cut.items(), key=lambda kv: kv[1])
            warns.append({"code": "clipped_text", "role": role, "text": _txt(t), "side": side, "by_mm": _mm(px, dpi),
                          "message": f"{role} '{_short(_txt(t))}' is cut off at the {side} edge by {_mm(px, dpi)} mm: "
                                     f"shorten it, use a smaller label_size / tick_size, or make the figure larger"})
    # -- font size at print size
    small: dict[str, list] = {}
    for role, t in texts:
        pt = float(t.get_fontsize()) * scale
        if pt < mfont - 1e-6:
            small.setdefault(role, []).append(round(pt, 1))
    for role, sizes in small.items():
        warns.append({"code": "small_font", "role": role, "size_pt": min(sizes), "min_pt": mfont, "count": len(sizes),
                      "message": f"{role} text is {min(sizes)} pt at {round(fig_cm * scale, 1)} cm wide "
                                 f"(minimum {mfont:g} pt): raise its size in the style"})
    # -- tick labels running into each other
    for ax in fig.axes:
        if not ax.get_visible():
            continue
        for name, axis in (("x", ax.xaxis), ("y", ax.yaxis)):
            labs = [(t, t.get_window_extent(r)) for t in _tick_labels(axis)]
            labs = [(t, b) for t, b in labs if b.width > 0]
            hits = []
            for i in range(len(labs)):
                for j in range(i + 1, len(labs)):
                    a, b = labs[i][1], labs[j][1]
                    if _overlap(a, b) > 1.0:  # px in both directions
                        hits.append(f"{_txt(labs[i][0])}/{_txt(labs[j][0])}")
            if hits:
                warns.append({"code": "tick_overlap", "axis": name, "pairs": hits[:6],
                              "message": f"{name} tick labels overlap ({', '.join(hits[:3])}): set fewer ticks "
                                         f"(xticks) or a smaller tick_size"})
    # -- legend over the data
    for ax in fig.axes:
        leg = ax.get_legend()
        if leg is None or not leg.get_visible():
            continue
        hit = _legend_hits(ax, leg, r)
        if hit["total"]:
            free = _free_locations(ax, leg, r)
            what = ", ".join(f"{n} {k}" for k, n in hit["kinds"].items())
            where = (f"free places: {', '.join(free)}" if free else "no free place inside the axes: use legend_loc 'outside right' "
                     "or 'outside top', or widen ylim / xlim to make room")
            warns.append({"code": "legend_overlap", "covers": hit["kinds"], "series": hit["series"][:6], "free_locs": free,
                          "message": f"legend covers data ({what}{_of(hit['series'])}); {where} (style legend_loc)"})
    return warns


def _of(series: list[str]) -> str:
    return f" of {', '.join(repr(s) for s in series[:3])}" if series else ""


def _short(s: str, n: int = 40) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


def _overlap(a: Bbox, b: Bbox) -> float:
    """Smaller side of the intersection of two boxes (0 = apart)."""
    w = min(a.x1, b.x1) - max(a.x0, b.x0)
    h = min(a.y1, b.y1) - max(a.y0, b.y0)
    return min(w, h) if w > 0 and h > 0 else 0.0


def _legend_box(leg, r) -> Bbox:
    """The legend's content (entries) without the frame padding: the part that hides what is under it."""
    box = getattr(leg, "_legend_box", None)
    try:
        return box.get_window_extent(r) if box is not None else leg.get_window_extent(r)
    except Exception:
        return leg.get_window_extent(r)


def _label(a) -> str:
    lab = a.get_label() if hasattr(a, "get_label") else ""
    return "" if not lab or lab.startswith("_") else lab


def _legend_hits(ax, leg, r) -> dict:
    """Data under the legend: counts per kind ('points', 'line segments', 'band', 'error bars', 'contour')."""
    lb = _legend_box(leg, r)
    clip = ax.bbox
    x0, y0 = max(lb.x0, clip.x0), max(lb.y0, clip.y0)
    x1, y1 = min(lb.x1, clip.x1), min(lb.y1, clip.y1)
    kinds: dict[str, int] = {}
    series: list[str] = []
    if x1 <= x0 or y1 <= y0:
        return {"total": 0, "kinds": kinds, "series": series}
    box = Bbox([[x0, y0], [x1, y1]])

    def hit(kind, n, a):
        if n:
            kinds[kind] = kinds.get(kind, 0) + int(n)
            lab = _label(a)
            if lab and lab not in series:
                series.append(lab)

    for ln in ax.get_lines():
        if not ln.get_visible() or ln.get_alpha() == 0:
            continue
        xy = ln.get_transform().transform(np.asarray(ln.get_xydata(), float))
        if not len(xy):
            continue
        if ln.get_marker() not in (None, "None", "none", "", " "):
            ok = np.isfinite(xy).all(1)
            p = xy[ok]
            hit("points", ((p[:, 0] >= x0) & (p[:, 0] <= x1) & (p[:, 1] >= y0) & (p[:, 1] <= y1)).sum(), ln)
        if ln.get_linestyle() not in ("None", "none", "", " ") and ln.get_linewidth() > 0:
            hit("line segments", _segments_in(xy, box), ln)
    for c in ax.collections:
        if not c.get_visible() or isinstance(c, QuadMesh):  # a colour map is the background, not covered data
            continue
        hit(*_collection_hits(c, box, x0, y0, x1, y1), c)
    return {"total": sum(kinds.values()), "kinds": kinds, "series": series}


def _segments_in(xy: np.ndarray, box: Bbox) -> int:
    """Segments of a polyline (NaN = gap) that cross the box (Liang-Barsky clipping, vectorised)."""
    xy = np.asarray(xy, float)
    if len(xy) < 2:
        return 0
    a, b = xy[:-1], xy[1:]
    ok = np.isfinite(a).all(1) & np.isfinite(b).all(1)
    a, b = a[ok], b[ok]
    d = b - a
    t0, t1 = np.zeros(len(a)), np.ones(len(a))
    keep = np.ones(len(a), bool)
    with np.errstate(divide="ignore", invalid="ignore"):
        for p, q in ((-d[:, 0], a[:, 0] - box.x0), (d[:, 0], box.x1 - a[:, 0]),
                     (-d[:, 1], a[:, 1] - box.y0), (d[:, 1], box.y1 - a[:, 1])):
            par = p == 0
            keep &= ~(par & (q < 0))
            rr = q / p
            t0 = np.where(~par & (p < 0), np.maximum(t0, rr), t0)
            t1 = np.where(~par & (p > 0), np.minimum(t1, rr), t1)
    return int((keep & (t0 <= t1)).sum())


def _collection_hits(c: Collection, box: Bbox, x0, y0, x1, y1) -> tuple[str, int]:
    if isinstance(c, PathCollection) and len(c.get_offsets()):  # scatter: the marker positions
        p = c.get_offset_transform().transform(np.asarray(c.get_offsets(), float))
        p = p[np.isfinite(p).all(1)]
        return "points", int(((p[:, 0] >= x0) & (p[:, 0] <= x1) & (p[:, 1] >= y0) & (p[:, 1] <= y1)).sum())
    tr = c.get_transform()
    filled = len(c.get_facecolor()) and float(np.max(np.asarray(c.get_facecolor())[:, 3])) > 0
    kind = "band" if filled else ("contour" if type(c).__name__.endswith("ContourSet") else "error bars")
    n = 0
    for p in c.get_paths():
        if not len(p.vertices):
            continue
        q = tr.transform_path(p)
        if filled:
            if q.intersects_bbox(box, filled=True):
                n += 1
        else:
            n += _segments_in(q.vertices, box)
    return kind, n


def _free_locations(ax, leg, r) -> list[str]:
    """Legend positions inside the axes where it covers nothing (the legend is moved back afterwards)."""
    old = leg._loc
    free = []
    try:
        for name in LOCS:
            leg._loc = leg.codes[name]
            ax.figure.canvas.draw()
            if not _legend_hits(ax, leg, ax.figure.canvas.get_renderer())["total"]:
                free.append(name)
    finally:
        leg._loc = old
        ax.figure.canvas.draw()
    return free
