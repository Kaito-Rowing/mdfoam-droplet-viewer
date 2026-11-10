"""Command line for analysis and figures without the browser; every command prints JSON on stdout.

    python -m mdfoam_droplet_viewer.cli analyze PATH --settings s.json --save NAME
    python -m mdfoam_droplet_viewer.cli saved list
    python -m mdfoam_droplet_viewer.cli summary NAME
    python -m mdfoam_droplet_viewer.cli fig render spec.yaml -o out.png
    python -m mdfoam_droplet_viewer.cli fig check spec.yaml
    python -m mdfoam_droplet_viewer.cli fig series spec.yaml

(also installed as the `mdfoam` command). Exit codes: 0 ok, 1 lint warnings with --strict, 2 bad input / spec.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from pathlib import Path

from . import __version__, export, saved


def _out(obj, code: int = 0) -> int:
    if "api_version" not in obj:
        ok = obj.get("ok", code == 0)
        obj = ({"api_version": "1.0", "ok": True, "result": obj} if ok else
               {"api_version": "1.0", "ok": False, "error": {"code": "invalid_input", "reason": obj.get("error", "operation failed")}, "details": obj})
    sys.stdout.write(json.dumps(_clean(obj), ensure_ascii=False, indent=1, default=str) + "\n")
    return code


def _clean(v):
    if isinstance(v, float) and not math.isfinite(v):
        return None
    if isinstance(v, dict):
        return {k: _clean(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_clean(x) for x in v]
    if hasattr(v, "item") and not isinstance(v, (str, bytes)):  # numpy scalars
        try:
            return v.item()
        except Exception:
            return v
    return v


def _fail(msg: str, code: int = 2, **kw) -> int:
    return _out({"ok": False, "error": msg, **kw}, code)


def _root(args) -> Path:
    return Path(args.root) if args.root else saved.default_root()


def _units(args) -> dict:
    return {**export.DEFAULT_UNITS, **(json.loads(args.units) if getattr(args, "units", None) else {})}


# ------------------------------------------------------------------ analyze
def cmd_analyze(args) -> int:
    from . import server
    from .service import operate
    server.CACHE_ROOT = _root(args)
    from .batch import Analyzer
    server.S = server.State()
    if args.workers:
        server.S.analyzer = Analyzer(server.CACHE_ROOT, workers=args.workers)
    settings = {}
    if args.settings_from:
        settings = saved.load(_root(args), args.settings_from).get("run_meta", {}).get("settings", {})
    if args.settings:
        settings.update(json.loads(Path(args.settings).read_text(encoding="utf-8")))
    if args.save and saved.exists(_root(args), args.save) and not args.overwrite:
        return _fail("saved result exists; use --overwrite")
    scanned = operate("discover", {"path": args.path, "subpath": args.subpath or settings.get("data_subpath", "auto")})
    if not scanned["ok"]:
        return _out(scanned, 2)
    result = operate("analyze", {"settings": settings, "names": args.cases.split(",") if args.cases else None})
    if not result["ok"]:
        return _out(result, 2)
    since = 0
    try:
        while server.S.analyzer._thread.is_alive():
            time.sleep(0.1)
            since = _progress(server.S.analyzer, since, args.quiet)
    except KeyboardInterrupt:
        server.S.analyzer.stop()
        server.S.analyzer._thread.join()
        return _fail("analysis cancelled", 130)
    final = operate("job_status", {})
    if args.save:
        final["saved"] = operate("save", {"name": args.save, "overwrite": args.overwrite})
        if not final["saved"]["ok"]:
            return _out(final, 2)
    if final["result"]["state"] != "done":
        return _fail("analysis did not complete", job=final)
    results = server.S.analyzer.results
    if not any(r.get("rows") for r in results.values()) or any(r.get("errors") or r.get("status") == "error" for r in results.values()):
        return _fail("no usable results or case errors", job=final, cases=results)
    return _out(final)



def _progress(an, since: int, quiet: bool) -> int:
    logs = an.job.logs
    if not quiet:
        for x in logs[since:]:
            sys.stderr.write(f"[{x['t']}] {x['level']}: {x['msg']}\n")
    return len(logs)


# ------------------------------------------------------------------ saved results
def cmd_saved_list(args) -> int:
    items = saved.list_saved(_root(args))
    return _out({"ok": True, "root": str(_root(args) / "saved"),
                 "saved": [{k: m.get(k) for k in ("name", "saved_at", "n_cases", "n_rows", "case_names", "source", "version")}
                           for m in items]})


def cmd_summary(args) -> int:
    from .compare import from_payload
    root = _root(args)
    try:
        p = saved.load(root, args.name)
    except FileNotFoundError:
        return _fail(f"no saved result {args.name!r}", saved=[m["name"] for m in saved.list_saved(root)])
    units = _units(args)
    res = p.get("results") or {}
    names = [n for n, r in res.items() if r.get("rows")]
    conv = lambda q, v: export.convert(q, v, units) if q in export.QUANTITIES else v  # noqa: E731
    cases = []
    for n, r in res.items():
        sm = r.get("summary") or {}
        cases.append({"name": n, "status": r.get("status"), "n_rows": len(r.get("rows") or []),
                      "n_skipped": len(r.get("skipped") or []), "reason": r.get("reason") or None,
                      "summary": {k: conv(k, v) for k, v in sm.items()}, "meta": r.get("meta") or {}})
    if args.cases:
        want = set(args.cases.split(","))
        cases = [c for c in cases if c["name"] in want]
    stats = export.statistics_rows(res, names, units)
    cmp = from_payload(p, args.name)  # what the comparison sees: box, molecule mass, set temperature
    first = next(iter(cmp["cases"].values()), {}) if cmp["cases"] else {}
    ctx = first.get("ctx") or {}
    lf = export.unit_of("r_eq", units)[1]
    return _out({"ok": True, "name": p.get("name", args.name), "saved_at": p.get("saved_at"), "source": p.get("source"),
                 "version": p.get("version"), "units": {q: export.unit_of(q, units)[0] for q in sorted({*export.STAT_QUANTITIES})},
                 "n_cases": len(res), "n_with_rows": len(names),
                 "box": {a: v / lf for a, v in (ctx.get("periods") or {}).items()} or None,
                 "box_unit": export.unit_of("r_eq", units)[0], "molecule_mass_kg": ctx.get("mass"),
                 "sim": first.get("sim"), "settings": (p.get("run_meta") or {}).get("settings"),
                 "statistics": [s for s in stats if s["n_cases"]],
                 "cases": [] if args.stats_only else (cases[: args.limit] if args.limit else cases),
                 "row_quantities": sorted({k for r in res.values() for row in (r.get("rows") or [])[:1] for k in row})})


STATS_DEFAULT = ("gas_p", "temp_mean", "contact_angle", "r_eq")


def cmd_stats(args) -> int:
    """Time-window statistics of row quantities: per case the time mean with its SD and an SE corrected for the
    autocorrelation of consecutive outputs, and over the cases the spread of those means (runs are independent)."""
    from .analysis import derive_gas_density, derive_normalized
    from .stats import mean_se
    root = _root(args)
    try:
        p = saved.load(root, args.name)
    except FileNotFoundError:
        return _fail(f"no saved result {args.name!r}", saved=[m["name"] for m in saved.list_saved(root)])
    units = _units(args)
    res = {n: r for n, r in (p.get("results") or {}).items() if r.get("rows")}
    if args.cases:
        want = args.cases.split(",")
        miss = [c for c in want if c not in res]
        if miss:
            return _fail(f"no results for case(s) {', '.join(miss)}", cases=sorted(res)[:20])
        res = {n: res[n] for n in want}
    qs = [q.strip() for q in (args.quantity.split(",") if args.quantity else STATS_DEFAULT) if q.strip()]
    have = {k for r in res.values() for row in r["rows"][:1] for k in row} | {"r_norm", "V_norm", "gas_ndens", "gas_rho"}
    bad = [q for q in qs if q not in have]
    if bad:
        return _fail(f"unknown row quantity {', '.join(bad)}", row_quantities=sorted(have))
    tf = export.unit_of("time", units)[1]
    lo = -math.inf if args.t_min is None else args.t_min * tf
    hi = math.inf if args.t_max is None else args.t_max * tf
    cases, means = [], {q: [] for q in qs}
    for n, r in res.items():
        rows = sorted(r["rows"], key=lambda x: x.get("time", 0))
        if any(q in ("r_norm", "V_norm") for q in qs) and "r_norm" not in rows[0]:
            derive_normalized(rows)  # each run divided by its first value (before the window cut)
        rows = [x for x in rows if x.get("status") == "ok" and lo * (1 - 1e-9) <= x["time"] <= hi * (1 + 1e-9)]
        for x in rows:
            if "gas_ndens" not in x:
                derive_gas_density(x, (r.get("ctx") or {}).get("mass"))
        c = {"name": n, "n_rows": len(rows),
             "t_range": [x["time"] / tf for x in (rows[0], rows[-1])] if rows else None}
        for q in qs:
            vals = [export.convert(q, x.get(q), units) for x in rows]
            d = mean_se([v for v in vals if isinstance(v, (int, float))])
            c[q] = {"n": d["n"], "mean": d["mean"], "sd": d["sd"], "se": d["se"], "n_eff": round(d["n_eff"], 1)}
            if d["n"]:
                means[q].append(d["mean"])
        cases.append(c)
    across = {q: export.describe(v) for q, v in means.items()}
    return _out({"ok": True, "name": p.get("name", args.name),
                 "units": {q: _unit_name(q, units) for q in qs}, "time_unit": export.unit_of("time", units)[0],
                 "window": {"t_min": args.t_min, "t_max": args.t_max},
                 "notes": {"per_case": "mean over the rows in the window; sd = SD over time; se = SE of the time mean "
                                       "corrected for autocorrelation (AR(1)), n_eff = effectively independent samples",
                           "across_cases": "statistics of the per-case means: sd = spread between cases (n-1), "
                                           "se = sd / sqrt(n) (cases taken as independent runs)"},
                 "across_cases": across,
                 "cases": [] if args.stats_only else (cases[: args.limit] if args.limit else cases)})


def _unit_name(q: str, units: dict) -> str:
    kind = export.QUANTITIES.get(q, ("count",))[0]
    return "°" if kind == "angle" else "K" if kind == "temperature" else export.unit_of(q, units)[0]


# ------------------------------------------------------------------ figures
def _spec(args):
    from . import figures
    try:
        spec = figures.load_spec(args.spec)
    except (OSError, ValueError) as exc:
        return None, _fail(f"cannot read the spec: {exc}", spec=str(args.spec))
    return spec, None


def _style_over(args) -> dict:
    over = {}
    if getattr(args, "dpi", None):
        over["dpi"] = args.dpi
    if getattr(args, "format", None):
        over["format"] = args.format
    if getattr(args, "lang", None):
        over["lang"] = args.lang
    return over


def cmd_fig(args) -> int:
    from . import figures
    spec, err = _spec(args)
    if spec is None:
        return err
    src = figures.Source(_root(args))
    lint = {}
    if args.min_font is not None:
        lint["min_font_pt"] = args.min_font
    if args.print_width is not None:
        lint["print_width_cm"] = args.print_width
    try:
        if args.fig_cmd == "series":
            b = figures.build(spec, src, figures.resolve_style(spec, _style_over(args)))
            return _out({"ok": True, **figures.series_json(b)})
        data, rep = figures.render(spec, src, style_over=_style_over(args), lint_opts=lint)
    except figures.SpecError as exc:
        return _fail(str(exc), spec=str(args.spec))
    out = {"ok": True, "spec": str(args.spec)}
    if args.fig_cmd == "render":
        dest = Path(args.output or spec.get("output") or Path(args.spec).with_suffix("." + rep["size"]["format"]).name)
        if not dest.is_absolute() and not args.output and spec.get("output"):
            dest = Path(args.spec).parent / dest  # output: in the spec is relative to the spec file
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        out["output"] = str(dest.resolve())
    lint_codes = {"clipped_text", "legend_overlap", "small_font", "tick_overlap"}
    out.update(rep)
    out["n_layout_warnings"] = sum(1 for w in rep["warnings"] if w.get("code") in lint_codes)
    return _out(out, 1 if args.strict and out["n_layout_warnings"] else 0)


def cmd_presets(args) -> int:
    from . import figures
    return _out({"ok": True, "default": figures.DEFAULT_PRESET, "presets": figures.PRESETS,
                 "kinds": {k: sorted(v | figures.COMMON_KEYS) for k, v in figures.KIND_KEYS.items()}})


def cmd_call(args, operation=None):
    from . import server
    from .service import operate
    server.CACHE_ROOT = _root(args)
    server.S = server.State()
    if getattr(args, "saved", None):
        opened = operate("open", {"name": args.saved})
        if not opened["ok"]:
            return _out(opened, 2)
    if getattr(args, "path", None):
        discovered = operate("discover", {"path": args.path})
        if not discovered["ok"]:
            return _out(discovered, 2)
    body = json.loads(Path(args.input).read_text(encoding="utf-8")) if getattr(args, "input", None) else {}
    if (operation or args.operation) in ("analyze", "job_status", "cancel"):
        return _fail("Use the blocking analyze command for CLI analysis and job control in a persistent MCP/GUI process")
    result = operate(operation or args.operation, body)
    if result["ok"] and getattr(args, "saved", None) and args.operation == "fit_model":
        persisted = operate("save", {"name": args.saved, "overwrite": True})
        if not persisted["ok"]:
            return _out(persisted, 2)
    return _out(result, 0 if result["ok"] else 2)


# ------------------------------------------------------------------ main
def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="mdfoam-droplet", description="mdFOAM Droplet Viewer without the browser (JSON on stdout)")
    ap.add_argument("--root", help="cache / saved-results folder (default ~/.mdfoam_droplet_viewer or MDFOAM_DROPLET_CACHE)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sc = sub.add_parser("schema", help="discover operation, settings, quantity and model schemas")
    sc.set_defaults(fn=lambda args: cmd_call(args, "schema"))
    call = sub.add_parser("call", help="call a versioned operation using a JSON input file")
    from .service import REQUESTS
    call.add_argument("operation", choices=sorted(REQUESTS))
    call.add_argument("--input", help="JSON request file (default: empty object)")
    call.add_argument("--path", help="discover this local dataset before the operation (e.g. inspect)")
    call.add_argument("--saved", help="open this result before the operation; persist successful fits back to it")
    call.set_defaults(fn=cmd_call)

    a = sub.add_parser("analyze", help="analyse the cases under PATH (and save the results)")
    a.add_argument("path")
    a.add_argument("--settings", help="JSON file with analysis settings (SI units; missing keys: defaults)")
    a.add_argument("--settings-from", help="start from the settings of this saved result")
    a.add_argument("--save", help="save the results under this name")
    a.add_argument("--overwrite", action="store_true")
    a.add_argument("--subpath", default="", help="data folder inside each case (default from the settings: auto)")
    a.add_argument("--cases", help="comma-separated case names to analyse (default all)")
    a.add_argument("--workers", type=int, help="worker processes (default CPUs - 1)")
    a.add_argument("--quiet", action="store_true", help="no progress log on stderr")
    a.set_defaults(fn=cmd_analyze)

    s = sub.add_parser("saved", help="saved results")
    ss = s.add_subparsers(dest="saved_cmd", required=True)
    sl = ss.add_parser("list")
    sl.set_defaults(fn=cmd_saved_list)

    m = sub.add_parser("summary", help="per-case summary and across-case statistics of a saved result")
    m.add_argument("name")
    m.add_argument("--units", help='display units as JSON, e.g. {"time": "ns", "pressure": "kPa"}')
    m.add_argument("--cases", help="comma-separated cases to list (statistics use all)")
    m.add_argument("--limit", type=int, default=0, help="list at most this many cases")
    m.add_argument("--stats-only", action="store_true", help="only the across-case statistics, no case list")
    m.set_defaults(fn=cmd_summary)

    t = sub.add_parser("stats", help="time-window statistics of row quantities (per case and over the cases)")
    t.add_argument("name")
    t.add_argument("-q", "--quantity", help=f"comma-separated row quantities (default {','.join(STATS_DEFAULT)})")
    t.add_argument("--t-min", type=float, help="window start in display time units (default: first output)")
    t.add_argument("--t-max", type=float, help="window end in display time units (default: last output)")
    t.add_argument("--units", help='display units as JSON, e.g. {"time": "ns", "pressure": "kPa"}')
    t.add_argument("--cases", help="comma-separated cases (default all with results)")
    t.add_argument("--limit", type=int, default=0, help="list at most this many cases")
    t.add_argument("--stats-only", action="store_true", help="only the statistics over the cases, no case list")
    t.set_defaults(fn=cmd_stats)

    f = sub.add_parser("fig", help="figures from spec files (JSON / YAML)")
    fs = f.add_subparsers(dest="fig_cmd", required=True)
    for name, hlp in (("render", "draw the spec into a file"), ("check", "draw in memory: spec errors and layout warnings"),
                      ("series", "the built series as JSON (no drawing)")):
        p = fs.add_parser(name, help=hlp)
        p.add_argument("spec")
        if name == "render":
            p.add_argument("-o", "--output", help="output file (default: the spec's output:, else <spec>.png here)")
        p.add_argument("--dpi", type=int)
        p.add_argument("--format", choices=sorted(export.FORMATS))
        p.add_argument("--lang", choices=["en", "ja"])
        p.add_argument("--min-font", type=float, help="smallest allowed text at print size in pt (default 7)")
        p.add_argument("--print-width", type=float, help="width the figure is printed / shown at, cm (default its own)")
        p.add_argument("--strict", action="store_true", help="exit 1 when there are layout warnings")
        p.set_defaults(fn=cmd_fig)
    pr = fs.add_parser("presets", help="size presets and the keys of each figure kind")
    pr.set_defaults(fn=cmd_presets)
    return ap


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):  # Windows consoles: JSON with Japanese / Greek text
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass
    args = parser().parse_args(argv)
    try:
        return args.fn(args)
    except KeyboardInterrupt:
        return _fail("interrupted", 130)
    except Exception as exc:
        return _fail(str(exc))


if __name__ == "__main__":
    sys.exit(main())
