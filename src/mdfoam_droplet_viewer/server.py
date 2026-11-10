"""Local web server (FastAPI). Binds to 127.0.0.1 only."""

from __future__ import annotations

import json
import math
import re
import threading
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import __version__, compare, export, figures, foam, saved, sim, viz
from .batch import Analyzer, _clean
from .case import CaseData, discover_cases
from .settings import PER_TIME_KEYS, Settings, density_kind, select_times, species_from_field

STATIC = Path(__file__).parent / "static"


def _code_stamp() -> float:
    """Newest modification time of the server's Python files (to notice an update while running)."""
    return max(p.stat().st_mtime for p in Path(__file__).parent.glob("*.py"))


STARTED_WITH = _code_stamp()
CACHE_ROOT = saved.default_root()


class State:
    def __init__(self):
        self.job_id = ""
        self.cases: list[CaseData] = []
        self.source: dict = {}
        self.analyzer = Analyzer(CACHE_ROOT)
        self.remote = None
        self.remote_cases: list[dict] = []
        self.models: dict = {}
        self.anchors: dict = {}   # (case, per-time settings, n times) -> viz anchor
        self.lock = threading.Lock()

    def case(self, name: str) -> CaseData:
        for c in self.cases:
            if c.name == name:
                return c
        raise HTTPException(404, f"unknown case {name}")


S = State()
app = FastAPI(title="mdFOAM Droplet Viewer", version=__version__)


def J(obj) -> JSONResponse:
    return JSONResponse(_clean(obj))


def _err(exc: Exception, code: int = 400):
    raise HTTPException(code, str(exc))


# ------------------------------------------------------------------ models
class ScanReq(BaseModel):
    path: str
    subpath: str = "auto"


class AnalyzeReq(BaseModel):
    settings: dict
    names: list[str] | None = None
    remote_tar: bool = True   # stream new files with `tar` on the server (falls back to SFTP)


class ExportReq(BaseModel):
    names: list[str]
    units: dict = {}


class PlotReq(BaseModel):
    kind: str = "timeseries"
    names: list[str] = []
    quantity: str = "volume"
    quantities: list[str] = []
    units: dict = {}
    style: dict = {}
    band: bool = False
    series: list[dict] = []
    xlabel: str = ""
    ylabel: str = ""
    title: str = ""
    window: dict | None = None    # {t0, t1 (s), crop}: averaging window of the case view
    moving_avg: int = 0
    xq: str | None = None         # xy plots: quantities for automatic axis labels
    yq: str | None = None
    xform: str = ""               # "" | "inv" (1/x) | "ln"
    yform: str = ""
    plots: list[dict] = []        # xy ZIP: [{filename, series, xq, yq, xform, yform, xlabel, ylabel, title}]
    groups: list[int] = []        # timeseries: colour slot per case (the screen's, so the figure matches it)
    xticks: dict | None = None    # xy: {values, labels} for a categorical x axis
    figure: dict | None = None    # a declarative figure spec (figures.py): the series are built on the server


class VizReq(BaseModel):
    case: str
    time: str = ""
    t_start: float | None = None
    t_end: float | None = None
    fps: float = 5.0
    settings: dict = {}
    opts: dict = {}
    units: dict = {}
    style: dict = {}




class RemoteConnectReq(BaseModel):
    host: str
    port: int = 22
    username: str = ""
    key_file: str = ""
    password: str = ""
    passphrase: str = ""


class RemoteScanReq(BaseModel):
    path: str
    subpath: str = "auto"


class SaveReq(BaseModel):
    name: str
    overwrite: bool = False
    ui: dict = {}


class OpenReq(BaseModel):
    name: str


# ------------------------------------------------------------------ basic
@app.get("/api/info")
def info():
    return J({"version": __version__, "cache_root": str(CACHE_ROOT), "workers": S.analyzer.workers,
              "defaults": Settings().to_dict(),
              "units": export.UNITS, "default_units": export.DEFAULT_UNITS,
              "quantities": {k: {"kind": v[0], "label": v[1]} for k, v in export.QUANTITIES.items()},
              "source": S.source, "stale": _code_stamp() > STARTED_WITH + 1})


def _forget_results():
    """A newly loaded folder starts without results: results of another folder must not mix in (or be saved with it)."""
    with S.analyzer._lock:
        S.analyzer.results = {}
        S.analyzer.run_meta = {}
    S.models = {}


def _refuse_if_running():
    if S.analyzer.job.state in ("running", "stopping"):
        raise HTTPException(409, "analysis is running: stop it (or wait) before loading another folder")


def case_info(c: CaseData) -> dict:
    """Case summary; remote cases not yet mirrored report what the remote scan saw."""
    info = c.info()
    try:  # thermostat targets from system/ (local data or the downloaded copy)
        sm = sim.read_sim(c.data_dir) if c.data_dir and Path(c.data_dir).is_dir() else None
    except Exception:
        sm = None
    if sm:
        info.update(target_T=sm["target_T"], target_text=sm["target_text"])
    rc = getattr(c, "remote", None)
    if rc is not None:
        info["remote"] = True
        has = c.times if c.scanned else (c.saved_info or {}).get("n_times")  # a lazy case is not scanned for this
        if not has:
            info.update(meta=rc.get("meta", {}), n_times=rc.get("n_times", 0), last_time=rc.get("last_time"),
                        n_processors=rc.get("n_processors", 0), remote_root=rc["root"], synced=False)
        else:
            info.update(remote_root=rc["root"], synced=True)
        if not rc.get("data_dir"):
            info["warnings"] = ["no mesh / results found"]
        # processor*/ is never downloaded: say what that leaves out
        if rc.get("data_dir") and not rc.get("reconstructed_mesh", True):
            info["warnings"] = info.get("warnings", []) + ["mesh only in processor*/ (not reconstructed): not downloaded"]
        if rc.get("n_proc_only"):
            info["warnings"] = info.get("warnings", []) + [
                f"{rc['n_proc_only']} time(s) only in processor*/ (not reconstructed): not downloaded"]
    return info


@app.post("/api/scan")
def scan(req: ScanReq):
    _refuse_if_running()
    p = Path(req.path.strip().strip('"'))
    try:
        found = discover_cases(p, req.subpath or "auto")
    except Exception as exc:
        _err(exc)
    if not found:
        raise HTTPException(400, "no cases found (no constant/polyMesh, main/ or processor*/ data)")
    cases = []
    for name, root, ddir in found:
        try:
            cases.append(CaseData.open(name, root, ddir))
        except Exception as exc:
            c = CaseData(name=name, root=root, data_dir=ddir or root, warnings=[f"open failed: {exc}"])
            cases.append(c)
    S.cases = cases
    S.source = {"type": "local", "path": str(p), "subpath": req.subpath}
    _forget_results()
    return J({"cases": [c.info() for c in cases]})


@app.get("/api/cases")
def cases():
    return J({"cases": [case_info(c) for c in S.cases], "source": S.source})


@app.get("/api/system")
def system(case: str = ""):
    c = S.case(case) if case else next((c for c in S.cases if c.times), None)  # stops at the first (lazy scans)
    if c is None:
        raise HTTPException(400, "no case with results")
    try:
        mesh = c.load_mesh(CACHE_ROOT)
    except Exception as exc:
        _err(exc)
    fields = []
    for f in c.available_fields():
        try:
            dims = c.field_dimensions(f)
        except Exception:
            dims = ""
        fields.append({"name": f, "dimensions": dims, "kind": density_kind(dims, f),
                       "n_times": sum(1 for t in c.times if f in t.fields)})
    masses = foam.read_molecule_masses(c.data_dir / "constant" / "moleculeProperties")
    ids = foam.read_id_list(c.data_dir / "constant" / "idList")
    try:
        sm = sim.read_sim(c.data_dir)
        sm = {k: v for k, v in sm.items() if k != "files"}
    except Exception:
        sm = None
    return J({"case": c.info(), "mesh": mesh.summary(), "fields": fields, "molecule_masses": masses,
              "species_ids": ids, "thermostat_T": (sm or {}).get("target_T") or _thermostat_T(c.data_dir), "sim": sm,
              "times": [{"name": t.name, "value": t.value, "source": t.source,
                         "rho": [f for f in t.fields if f.startswith("rho")]} for t in c.times]})


def _thermostat_T(d: Path) -> list[float]:
    """Target temperatures from system/tVEquilibrationDict or mdEquilibrationDict (informational)."""
    for name in ("tVEquilibrationDict", "mdEquilibrationDict"):
        f = d / "system" / name
        if f.exists():
            try:
                _, body = foam.strip_header(foam.read_text(f))
                return sorted({float(v) for v in re.findall(r"\bT\s+([0-9.eE+\-]+)\s*;", body)})
            except Exception:
                return []
    return []


@app.get("/api/sim")
def sim_settings(case: str):
    """Every value of the case's system/ dictionaries (flattened), plus the thermostat summary."""
    c = S.case(case)
    try:
        return J(sim.read_sim(c.data_dir))
    except Exception as exc:
        _err(exc)


@app.post("/api/settings/validate")
def validate(body: dict):
    s = Settings.from_dict(body.get("settings", {}))
    kind = "unknown"
    c0 = next((c for c in S.cases if c.times), None)
    if c0 is not None:
        try:
            kind = density_kind(c0.field_dimensions(s.density_field), s.density_field)
        except Exception:
            pass
    return J({"errors": s.validate(kind), "density_kind": kind, "species": s.species or species_from_field(s.density_field),
              "per_time_hash": s.hash(PER_TIME_KEYS), "hash": s.hash()})


# ------------------------------------------------------------------ analysis
@app.post("/api/analyze")
def analyze(req: AnalyzeReq):
    s = Settings.from_dict(req.settings)
    errs = s.validate()
    if errs:
        raise HTTPException(400, "; ".join(errs))
    connected = S.remote is not None and S.remote.cfg.host == S.source.get("host")
    if S.source.get("type") == "remote" and not connected and not any(c.times for c in S.cases):
        raise HTTPException(400, "not connected")
    if S.source.get("type") == "remote" and connected:
        sel = [rc for rc in S.remote_cases if (not req.names or rc["name"] in req.names) and rc.get("data_dir")]
        if not sel:
            raise HTTPException(400, "no remote case with results selected")
        try:
            S.analyzer.start([], s, pre_step=_remote_pre_step(sel, s, req.remote_tar))
        except RuntimeError as exc:
            _err(exc, 409)
        S.job_id = uuid.uuid4().hex
        return J({"started": True, "n_cases": len(sel)})
    cases = [c for c in S.cases if not req.names or c.name in req.names]
    if S.source.get("type") == "remote":  # not connected: analyse what was downloaded earlier
        cases = [c for c in cases if c.times]
    if not cases:
        raise HTTPException(400, "no cases selected")
    try:
        S.analyzer.start(cases, s)
    except RuntimeError as exc:
        _err(exc, 409)
    S.job_id = uuid.uuid4().hex
    return J({"started": True, "n_cases": len(cases)})


@app.get("/api/job")
def job(since: int = 0):
    return J(S.analyzer.job.view(since))


@app.post("/api/job/stop")
def job_stop():
    S.analyzer.stop()
    return J({"ok": True})


@app.get("/api/results")
def results():
    return J({"results": S.analyzer.results, "run_meta": S.analyzer.run_meta})


@app.post("/api/cache/clear")
def cache_clear():
    n = S.analyzer.cache.clear()
    from .case import _cached_addressing, _cached_mesh
    _cached_mesh.cache_clear()
    _cached_addressing.cache_clear()
    return J({"removed": n})




# ------------------------------------------------------------------ visualization
@app.post("/api/viz")
def viz_frame(req: VizReq):
    c = S.case(req.case)
    s = Settings.from_dict(req.settings)
    try:
        fr = viz.frame(c, req.time, s, CACHE_ROOT, molecules=bool(req.opts.get("molecules")),
                       max_points=int(req.opts.get("max_points", 20000)), grid=bool(req.opts.get("grid")))
    except Exception as exc:
        _err(exc)
    return J(fr)


def _anchor(c: CaseData, s: Settings, times: list[str] | None = None) -> dict:
    key = (c.name, str(c.data_dir), s.hash(PER_TIME_KEYS), tuple(times) if times else len(c.times))
    if key not in S.anchors:
        S.anchors[key] = viz.anchor(c, s, CACHE_ROOT, times=times)
    return S.anchors[key]


@app.post("/api/viz/anchor")
def viz_anchor(req: VizReq):
    """Fixed references for a case: the most droplet-like cross-section per axis and the density scale."""
    c = S.case(req.case)
    try:
        return J(_anchor(c, Settings.from_dict(req.settings)))
    except Exception as exc:
        _err(exc)


def _fixed_opts(c: CaseData, s: Settings, opts: dict, times: list[str] | None = None) -> dict:
    """Section position and density range that stay the same over time (unless the user chose them)."""
    opts = dict(opts)
    if opts.get("view") == "slice" and ((opts.get("slice_pos") is None and not opts.get("slice_follow"))
                                        or opts.get("rho_max") is None):
        a = _anchor(c, s, times)
        plane = opts.get("projection", "xz")
        k = "xyz"[3 - "xyz".index(plane[0]) - "xyz".index(plane[1])]
        if opts.get("slice_pos") is None and not opts.get("slice_follow"):
            opts["slice_pos"] = a["slice"][k]
        if opts.get("rho_max") is None:
            opts["rho_max"] = a["rho_max"]
    return opts


@app.post("/api/viz/png")
def viz_png(req: VizReq):
    c = S.case(req.case)
    s = Settings.from_dict(req.settings)
    fr = viz.frame(c, req.time, s, CACHE_ROOT, molecules=bool(req.opts.get("show_molecules")),
                   max_points=int(req.opts.get("max_points", 20000)), grid=req.opts.get("view") == "slice")
    report: dict = {}
    img = export.render_frame(_clean(fr), _fixed_opts(c, s, req.opts), req.units, req.style, report=report)
    return Response(export.image_png(img), media_type="image/png", headers=_lint_header(report.get("lint", [])))


def _lint_header(warnings, name: str = "X-Figure-Lint") -> dict:
    """Layout warnings as a response header (ASCII JSON): the image stays the body."""
    return {name: json.dumps(_clean(warnings), ensure_ascii=True), "Access-Control-Expose-Headers": name}


def _current_payload() -> dict:
    loaded = {c.name for c in S.cases}
    cases = [{"name": c.name, "root": str(c.root), "data_dir": str(c.data_dir)} for c in S.cases]
    return {"name": S.source.get("saved") or "loaded", "results": {k: v for k, v in S.analyzer.results.items() if k in loaded},
            "run_meta": S.analyzer.run_meta, "cases": cases, "source": S.source}


_FIG_SOURCE: list = []


def _fig_source() -> figures.Source:
    """One source for the server's lifetime: saved results stay loaded while their files are unchanged."""
    if not _FIG_SOURCE or _FIG_SOURCE[0].root != CACHE_ROOT:
        _FIG_SOURCE[:] = [figures.Source(CACHE_ROOT, current=_current_payload)]
    return _FIG_SOURCE[0]


@app.post("/api/viz/gif")
def viz_gif(req: VizReq):
    c = S.case(req.case)
    s = Settings.from_dict(req.settings)
    t0 = -math.inf if req.t_start is None else req.t_start
    t1 = math.inf if req.t_end is None else req.t_end
    times = [t for t in c.times if t0 <= t.value <= t1 and s.density_field in t.fields]
    if not times:
        raise HTTPException(400, "no times in range")
    if len(times) > 400:
        raise HTTPException(400, f"{len(times)} frames requested; narrow the range (max 400)")
    # one section, one colour range, fixed axes and margins for every frame: only the data moves
    opts = {**_fixed_opts(c, s, req.opts, [t.name for t in times]), "fixed_layout": True}
    imgs = []
    for t in times:
        fr = viz.frame(c, t.name, s, CACHE_ROOT, molecules=bool(req.opts.get("show_molecules")),
                       max_points=int(req.opts.get("max_points", 20000)), grid=req.opts.get("view") == "slice")
        imgs.append(export.render_frame(_clean(fr), opts, req.units, req.style))
    return Response(export.frames_to_gif(imgs, req.fps), media_type="image/gif")


# ------------------------------------------------------------------ export
@app.post("/api/export/csv")
def export_csv(req: ExportReq):
    data = export.build_csv_zip(S.analyzer.results, req.names, req.units, S.analyzer.run_meta,
                                {k: v for k, v in S.models.items() if k in req.names})
    return Response(data, media_type="application/zip")


@app.post("/api/plot/png")
def plot_png(req: PlotReq):
    R = S.analyzer.results
    names = [n for n in req.names if n in R]
    report: dict = {}
    built: list[dict] = []
    if req.figure:  # the server's builder; the browser's series (if sent) are the fallback
        try:
            st = figures.resolve_style({**req.figure, "style": {**(req.figure.get("style") or {}), **req.style}})
            b = figures.build({**req.figure, "units": req.units or req.figure.get("units") or {}}, _fig_source(), st)
            data = figures.draw(b["plot"], st, b["units"], report)
            return Response(data, media_type=export.mime(st), headers=_lint_header(b["warnings"] + report.get("lint", [])))
        except Exception as exc:
            if not req.series:
                _err(exc)
            report = {}
            built = [{"code": "figure_fallback", "message": f"drawn from the screen's series: {exc}"}]
    try:
        if req.kind == "timeseries":
            groups = [g for n, g in zip(req.names, req.groups) if n in R] if len(req.groups) == len(req.names) else None
            data = export.plot_timeseries(R, names, req.quantity, req.units, req.style, req.band,
                                          window=req.window, moving_avg=req.moving_avg, groups=groups, report=report)
        elif req.kind == "bar":
            data = export.plot_summary_bar(R, names, req.quantity, req.units, req.style)
        elif req.kind == "hist":
            data = export.plot_summary_hist(R, names, req.quantity, req.units, req.style)
        elif req.kind == "xy":
            data = export.plot_xy(req.series, req.style, req.xlabel, req.ylabel, req.title, units=req.units,
                                  xq=req.xq, yq=req.yq, xform=req.xform, yform=req.yform, xticks=req.xticks, report=report)
        else:
            raise HTTPException(400, f"unknown plot kind {req.kind}")
    except HTTPException:
        raise
    except Exception as exc:
        _err(exc)
    return Response(data, media_type=export.mime(req.style), headers=_lint_header(built + report.get("lint", [])))


class FigureReq(BaseModel):
    spec: dict                    # a figure spec (figures.py; the same as a spec file)
    style: dict = {}              # overrides of the spec's style (e.g. dpi for a preview)
    lint: dict = {}               # min_font_pt, print_width_cm


def _figure(req: FigureReq):
    try:
        return figures.render(req.spec, _fig_source(), style_over=req.style, lint_opts=req.lint)
    except figures.SpecError as exc:
        _err(exc)


@app.post("/api/figure/png")
def figure_png(req: FigureReq):
    """A spec drawn on the server; warnings and info in the X-Figure-Report header (JSON)."""
    data, rep = _figure(req)
    return Response(data, media_type=export.FORMATS.get(rep["size"]["format"], "image/png"),
                    headers=_lint_header(rep, "X-Figure-Report"))


@app.post("/api/figure/check")
def figure_check(req: FigureReq):
    """Spec errors (400) or the warnings / info of the drawn figure, without the image."""
    _, rep = _figure(req)
    return J({"ok": True, **rep})


@app.post("/api/figure/build")
def figure_build(req: FigureReq):
    """The series and axis settings a spec draws (the body of /api/plot/png for xy figures)."""
    try:
        return J(figures.series_json(figures.build(req.spec, _fig_source(), figures.resolve_style(req.spec, req.style))))
    except figures.SpecError as exc:
        _err(exc)


@app.post("/api/plot/xyzip")
def plot_xyzip(req: PlotReq):
    try:
        data = export.xy_zip(req.plots, req.style, req.units)
    except Exception as exc:
        _err(exc)
    return Response(data, media_type="application/zip")


class CompareReq(BaseModel):
    names: list[str]              # saved results; compare.CURRENT = the results loaded on screen


compare._CACHE_ROOT[:] = [CACHE_ROOT]


@app.post("/api/compare")
def compare_saved(req: CompareReq):
    """Saved results side by side (rows trimmed to the plotted quantities, simulation settings when available).
    One saved result with several cases (e.g. a radius sweep) is compared case by case."""
    if not 1 <= len(req.names) <= 8:
        raise HTTPException(400, "choose 1 to 8 saved results")
    try:
        items = []
        for n in req.names:
            if n == compare.CURRENT:
                payload = _current_payload()
                items.append({**compare.from_payload(payload, n), "name": n, "title": payload["name"]})
            else:
                items.append(compare.from_payload(saved.load(CACHE_ROOT, n), n))
        return J({"items": items})
    except FileNotFoundError:
        raise HTTPException(404, "saved result not found")
    except Exception as exc:
        _err(exc)






# ------------------------------------------------------------------ figure presets (named export settings)
def _presets_path() -> Path:
    return CACHE_ROOT / "figure_presets.json"


def _read_presets() -> list[dict]:
    import json
    try:
        data = json.loads(_presets_path().read_text(encoding="utf-8"))
        return [p for p in data if isinstance(p, dict) and p.get("name")]
    except (OSError, ValueError):
        return []


class PresetReq(BaseModel):
    name: str
    data: dict = {}


@app.get("/api/figpresets")
def figpresets_list():
    return J({"presets": _read_presets()})


@app.post("/api/figpresets")
def figpresets_save(req: PresetReq):
    import json
    import time
    name = req.name.strip()[:60]
    if not name:
        raise HTTPException(400, "preset name is empty")
    ps = [p for p in _read_presets() if p["name"] != name]
    ps.append({"name": name, "data": req.data, "saved_at": time.strftime("%Y-%m-%d %H:%M:%S")})
    _presets_path().parent.mkdir(parents=True, exist_ok=True)
    tmp = _presets_path().with_suffix(".tmp")
    tmp.write_text(json.dumps(ps, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(_presets_path())
    return J({"presets": ps})


@app.post("/api/figpresets/delete")
def figpresets_delete(req: PresetReq):
    import json
    ps = [p for p in _read_presets() if p["name"] != req.name]
    _presets_path().parent.mkdir(parents=True, exist_ok=True)
    _presets_path().write_text(json.dumps(ps, ensure_ascii=False, indent=1), encoding="utf-8")
    return J({"presets": ps})






@app.post("/api/plot/batch")
def plot_batch(req: PlotReq):
    R = S.analyzer.results
    names = [n for n in req.names if n in R and R[n].get("rows")]
    data = export.batch_zip(R, names, req.quantities or [req.quantity], req.units, req.style,
                            window=req.window, moving_avg=req.moving_avg)
    return Response(data, media_type="application/zip")


# ------------------------------------------------------------------ remote
@app.get("/api/remote/hosts")
def remote_hosts():
    from .remote import ssh_config_hosts
    try:
        return J(ssh_config_hosts())
    except Exception as exc:
        return J({"hosts": [], "keys": [], "ppk": [], "error": str(exc)})


@app.post("/api/remote/connect")
def remote_connect(req: RemoteConnectReq):
    from .remote import RemoteConfig, RemoteSession
    if S.remote is not None:
        S.remote.close()
        S.remote = None
    try:
        S.remote = RemoteSession(RemoteConfig(req.host, req.port, req.username, req.key_file),
                                 password=req.password or None, passphrase=req.passphrase or None)
    except Exception as exc:
        msg = str(exc)
        if "not found in known_hosts" in msg or "Server" in msg and "not found" in msg:
            msg += " — connect once with `ssh` in a terminal to register the host key."
        _err(Exception(msg))
    return J({"ok": True, "home": S.remote.home()})


@app.post("/api/remote/disconnect")
def remote_disconnect():
    if S.remote is not None:
        S.remote.close()
    S.remote = None
    return J({"ok": True})


@app.get("/api/remote/list")
def remote_list(path: str = "."):
    if S.remote is None:
        raise HTTPException(400, "not connected")
    try:
        p = S.remote.normalize(path)
        return J({"path": p, "items": S.remote.listdir(p)})
    except Exception as exc:
        _err(exc)


@app.post("/api/remote/scan")
def remote_scan(req: RemoteScanReq):
    if S.remote is None:
        raise HTTPException(400, "not connected")
    _refuse_if_running()
    try:
        found = S.remote.find_cases(req.path, req.subpath)
    except Exception as exc:
        _err(exc)
    if not found:
        raise HTTPException(400, "no cases found here or one level below (no main/, constant/polyMesh or case_info.txt)")
    S.remote_cases = found
    S.source = {"type": "remote", "host": S.remote.cfg.host, "path": S.remote.normalize(req.path), "subpath": req.subpath}
    _forget_results()
    mirror = _mirror_root()
    cases = []
    for rc in found:
        # reuse an earlier mirror of this case if there is one (times show up before the next sync)
        lroot = mirror / rc["root"].strip("/").replace(":", "_")
        local = discover_cases(lroot, req.subpath or "auto") if lroot.is_dir() and rc.get("data_dir") else []
        c = CaseData.open(rc["name"], local[0][1], local[0][2]) if local else \
            CaseData(name=rc["name"], root=lroot, data_dir=lroot, meta=rc.get("meta", {}))
        c.remote = rc
        cases.append(c)
    S.cases = cases
    return J({"cases": [case_info(c) for c in cases], "source": S.source})


def _mirror_root() -> Path:
    return CACHE_ROOT / "remote" / re.sub(r"[^\w.\-]", "_", S.remote.cfg.host)


def _remote_pre_step(sel: list[dict], s: Settings, bulk: bool = True):
    def step(job):
        fields = sorted({f for f in (s.density_field, s.number_density_field, s.temperature_field) if f})
        job.detail = {"cases_total": len(sel), "cases_listed": 0, "files_total": 0, "files_done": 0}

        def progress(d):
            job.detail = d

        def time_filter(names):
            return [names[i] for i in sorted(select_times([float(n) for n in names], s.time_stride, s.time_min, s.time_max))]

        roots = S.remote.mirror_cases(sel, _mirror_root(), fields, False,
                                      stop=job.stop_flag.is_set, progress=progress, log=job.log, time_filter=time_filter,
                                      bulk=bulk)
        local = []
        for rc in sel:
            lroot = roots.get(rc["name"])
            found = discover_cases(lroot, s.data_subpath) if lroot and lroot.is_dir() else []
            if found:
                c = CaseData.open(rc["name"], found[0][1], found[0][2])
                c.remote = rc
                local.append(c)
        # keep the scanned list (and its order); swap in the mirrored data for the synced cases
        by_root = {c.remote["root"]: c for c in local}
        S.cases = [by_root.get(getattr(c, "remote", {}).get("root"), c) for c in S.cases]
        return local
    return step


# ------------------------------------------------------------------ saved results
@app.get("/api/saved")
def saved_list():
    return J({"saved": saved.list_saved(CACHE_ROOT)})


@app.post("/api/saved")
def saved_save(req: SaveReq):
    loaded = {c.name for c in S.cases}
    R = {k: v for k, v in S.analyzer.results.items() if k in loaded}  # only the cases on screen
    if not R:
        raise HTTPException(400, "no results to save: run the analysis first")
    payload = saved.make_payload(__version__, S.source, S.analyzer.run_meta, R, S.cases, S.models, req.ui)
    try:
        meta = saved.save(CACHE_ROOT, req.name, payload, overwrite=req.overwrite)
    except FileExistsError:
        raise HTTPException(409, "a saved result with this name already exists")
    except (ValueError, OSError) as exc:
        _err(exc)
    return J({"saved": meta})


@app.post("/api/saved/open")
def saved_open(req: OpenReq):
    _refuse_if_running()
    try:
        payload = saved.load(CACHE_ROOT, req.name)
    except FileNotFoundError:
        raise HTTPException(404, "saved result not found")
    except Exception as exc:
        _err(exc)
    cases = []
    for cd in payload.get("cases", []):
        root, ddir = Path(cd["root"]), Path(cd.get("data_dir") or cd["root"])
        try:
            if not ddir.is_dir():
                raise FileNotFoundError(ddir)
            c = CaseData.open(cd["name"], root, ddir, lazy=True)
            c.saved_info = ((payload.get("results") or {}).get(cd["name"]) or {}).get("info")
        except Exception:
            # data no longer on this PC (or never downloaded): results are still shown, viz is not
            c = CaseData(name=cd["name"], root=root, data_dir=ddir, meta=cd.get("meta") or {},
                         warnings=["data not available on this PC: saved results only"])
        if cd.get("remote"):
            c.remote = cd["remote"]
        cases.append(c)
    with S.analyzer._lock:
        S.analyzer.results = payload.get("results") or {}
        S.analyzer.run_meta = payload.get("run_meta") or {}
    S.models = payload.get("models") or {}
    S.cases = cases
    S.remote_cases = [c.remote for c in cases if getattr(c, "remote", None)]
    S.source = {**(payload.get("source") or {}), "saved": payload.get("name", req.name)}
    return J({"cases": [case_info(c) for c in cases], "source": S.source, "ui": payload.get("ui") or {},
              "name": payload.get("name", req.name), "saved_at": payload.get("saved_at")})


@app.post("/api/saved/delete")
def saved_delete(req: OpenReq):
    try:
        saved.delete(CACHE_ROOT, req.name)
    except ValueError as exc:
        _err(exc)
    return J({"ok": True})


# ------------------------------------------------------------------ static
@app.get("/")
def index():
    """index.html with ?v=<mtime> on the static assets, so a browser never keeps stale CSS/JS after an update."""
    from fastapi.responses import HTMLResponse
    html = (STATIC / "public.html").read_text(encoding="utf-8")
    for name in ("public.css", "public.js", "favicon.svg"):
        v = int((STATIC / name).stat().st_mtime)
        html = html.replace(f'/static/{name}"', f'/static/{name}?v={v}"')
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return FileResponse(STATIC / "favicon.svg", media_type="image/svg+xml")


@app.get("/vendor/plotly.min.js")
def plotly_js():
    import plotly
    return FileResponse(Path(plotly.__file__).parent / "package_data" / "plotly.min.js",
                        media_type="application/javascript")


app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.post("/api/v1/{operation}")
def application_operation(operation: str, body: dict):
    from .service import operate
    return J(operate(operation, body))


@app.middleware("http")
async def local_origin(request, call_next):
    from urllib.parse import urlsplit
    host = request.headers.get("host", "").split(":")[0]
    if host not in ("127.0.0.1", "localhost", "testserver"):
        return JSONResponse({"error": "local host required"}, status_code=403)
    origin = request.headers.get("origin")
    if origin and urlsplit(origin).netloc != request.headers.get("host"):
        return JSONResponse({"error": "same origin required"}, status_code=403)
    return await call_next(request)
