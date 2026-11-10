"""Background batch analysis: process pool, progress, logs, stop, per-case errors, and result cache."""

from __future__ import annotations

import hashlib
import json
import math
import os
import threading
import time as _time
import traceback
from concurrent.futures import FIRST_COMPLETED, Future, ProcessPoolExecutor, wait
from dataclasses import dataclass, field
from pathlib import Path
from multiprocessing import get_context

from . import __version__
from .analysis import case_ctx, derive_gas_density, derive_normalized, process_case_times, summarize
from .case import CaseData
from .settings import PER_TIME_KEYS, Settings

CHUNK = 25


def _clean(v):
    """JSON-safe: NaN/inf -> None."""
    if isinstance(v, float) and not math.isfinite(v):
        return None
    if isinstance(v, dict):
        return {k: _clean(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_clean(x) for x in v]
    return v


def _restore(row: dict) -> dict:
    return {k: (math.nan if v is None and k not in ("reason", "warnings") else v) for k, v in row.items()}


class ResultCache:
    def __init__(self, root: Path):
        self.root = Path(root)

    def _dir(self, case: CaseData) -> Path:
        return self.root / "results" / hashlib.sha1(str(case.data_dir.resolve()).encode()).hexdigest()[:16]

    def load(self, case: CaseData, key: str) -> dict:
        f = self._dir(case) / f"{key}.json"
        if not f.exists():
            return {}
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            f.unlink(missing_ok=True)  # corrupt cache is discarded, never reused
            return {}

    def save(self, case: CaseData, key: str, data: dict) -> None:
        d = self._dir(case)
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / f"{key}.json.tmp"
        tmp.write_text(json.dumps(_clean(data)), encoding="utf-8")
        tmp.replace(d / f"{key}.json")

    def clear(self) -> int:
        n = 0
        for sub in ("results", "mesh"):
            p = self.root / sub
            if p.exists():
                for f in p.rglob("*"):
                    if f.is_file():
                        f.unlink(missing_ok=True)
                        n += 1
        return n


def time_signature(case: CaseData, t: str, s: Settings) -> str:
    parts = [case.field_path_signature(t, s.density_field)]
    if s.temperature_enabled or s.pressure_enabled:
        te = case.time_entry(t)
        for f in (s.temperature_field, s.number_density_field):
            parts.append(case.field_path_signature(t, f) if f and f in te.fields else "none")
    return "|".join(parts)


@dataclass
class Job:
    state: str = "idle"          # idle | running | stopping | done | stopped | error
    total: int = 0
    done: int = 0
    started: float = 0.0
    finished: float = 0.0
    phase: str = ""
    detail: dict = field(default_factory=dict)  # phase-specific counters for the UI (sync files/bytes, cases)
    logs: list = field(default_factory=list)
    stop_flag: threading.Event = field(default_factory=threading.Event)

    def log(self, msg: str, level: str = "info") -> None:
        self.logs.append({"t": _time.strftime("%H:%M:%S"), "level": level, "msg": msg})
        del self.logs[:-2000]

    def view(self, since: int = 0) -> dict:
        return {"state": self.state, "total": self.total, "done": self.done, "phase": self.phase, "detail": self.detail,
                "elapsed": (self.finished or _time.time()) - self.started if self.started else 0,
                "logs": self.logs[since:], "n_logs": len(self.logs)}


class Analyzer:
    def __init__(self, cache_root: Path, workers: int | None = None):
        self.cache = ResultCache(cache_root)
        self.cache_root = Path(cache_root)
        self.workers = workers or max(1, (os.cpu_count() or 2) - 1)
        self.job = Job()
        self.results: dict[str, dict] = {}
        self.run_meta: dict = {}
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------ control
    def start(self, cases: list[CaseData], settings: Settings, pre_step=None) -> None:
        errors = settings.validate()
        if errors:
            raise ValueError("; ".join(errors))
        if self._thread and self._thread.is_alive():
            raise RuntimeError("an analysis is already running")
        self.job = Job(state="running", started=_time.time())
        self._thread = threading.Thread(target=self._run, args=(cases, settings, pre_step), daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self.job.state == "running":
            self.job.state = "stopping"
            self.job.stop_flag.set()
            self.job.log("stop requested: finishing running tasks, cancelling pending ones", "warn")

    # ------------------------------------------------------------------ worker
    def _run(self, cases: list[CaseData], s: Settings, pre_step) -> None:
        job = self.job
        try:
            if pre_step is not None:
                job.phase = "sync"
                cases = pre_step(job) or cases
            self._analyze(cases, s)
        except Exception as exc:
            job.state = "error"
            job.log(f"fatal: {exc}\n{traceback.format_exc()}", "error")
        finally:
            job.finished = _time.time()
            if job.state == "running":
                job.state = "done"
            elif job.state == "stopping":
                job.state = "stopped"
            job.log(f"finished: {job.state} ({job.done}/{job.total} time steps)")

    def _analyze(self, cases: list[CaseData], s: Settings) -> None:
        job = self.job
        job.phase = "analysis"
        key = s.hash(PER_TIME_KEYS)
        sdict = s.to_dict()
        cache_dir = str(self.cache_root)
        new_results: dict[str, dict] = {}
        tasks = []  # (case, times)
        for c in cases:
            try:
                c.scan_times()
            except OSError as exc:
                new_results[c.name] = self._empty(c, "error", f"cannot scan: {exc}")
                continue
            with_field = [t for t in c.times if s.density_field in t.fields]
            times = s.pick_times(with_field)
            res = self._empty(c, "running", "")
            res["n_times_without_field"] = len(c.times) - len(with_field)
            res["n_times_not_selected"] = len(with_field) - len(times)
            new_results[c.name] = res
            if not times:
                res["status"] = "nodata"
                res["reason"] = ("no time in the selected range" if with_field else
                                 "no time with field " + s.density_field if c.times else "no results yet")
                continue
            cached = self.cache.load(c, key)
            todo = []
            for t in times:
                ent = cached.get(t.name)
                if ent and ent.get("sig") == time_signature(c, t.name, s):
                    res["rows"].append(_restore(ent["row"]))
                else:
                    todo.append(t.name)
            res["_cache"] = cached
            res["n_cached"] = len(times) - len(todo)
            for i in range(0, len(todo), CHUNK):
                tasks.append((c, todo[i:i + CHUNK]))
            if not todo:
                res["status"] = "done"
        n_cached = sum(r.get("n_cached", 0) for r in new_results.values())
        job.total = sum(len(t) for _, t in tasks) + n_cached
        job.done = n_cached
        job.log(f"{len(cases)} cases, {job.total} time steps ({n_cached} from cache), {self.workers} workers")
        job.detail = {"cases_total": sum(1 for r in new_results.values() if r["status"] in ("running", "done")),
                      "cases_done": 0, "current": ""}
        with self._lock:
            self.results.update(new_results)
            self.run_meta = self._meta(cases, s)

        if tasks:
            # Forking a threaded GUI/MCP process can inherit locked runtime state.
            # Spawn gives identical, independent worker initialization on every OS.
            with ProcessPoolExecutor(max_workers=self.workers, mp_context=get_context("spawn")) as pool:
                futs: dict[Future, tuple] = {}
                for c, ts in tasks:
                    f = pool.submit(process_case_times, c.name, str(c.root), str(c.data_dir), ts, sdict, cache_dir)
                    futs[f] = (c, ts)
                pending = set(futs)
                while pending:
                    if job.stop_flag.is_set():
                        for f in pending:
                            f.cancel()
                    done, pending = wait(pending, timeout=0.5, return_when=FIRST_COMPLETED)
                    for f in done:
                        c, ts = futs[f]
                        res = new_results[c.name]
                        if f.cancelled():
                            res["incomplete"] = True
                            continue
                        try:
                            out = f.result()
                        except Exception as exc:
                            res["errors"].append(f"{ts[0]}..{ts[-1]}: {exc}")
                            job.log(f"[{c.name}] error: {exc}", "error")
                            res["incomplete"] = True
                            continue
                        res["ctx"] = out["ctx"]
                        for r in out["rows"]:
                            res["rows"].append(r)
                            res["_cache"][r["time_name"]] = {"sig": time_signature(c, r["time_name"], s), "row": r}
                            if r.get("status") != "ok":
                                job.log(f"[{c.name}] t={r['time_name']} skipped: {r.get('reason')}", "warn")
                        job.done += len(ts)
                    for c in cases:
                        res = new_results.get(c.name)
                        if res and res["status"] == "running" and not any(fc is c for f, (fc, _) in futs.items() if f in pending):
                            self._finalize(c, res, s, key)
        for c in cases:
            res = new_results[c.name]
            if "_cache" in res:  # not finalized yet (fully cached cases, or cases left after stop)
                self._finalize(c, res, s, key)


    def _finalize(self, c: CaseData, res: dict, s: Settings, key: str) -> None:
        if not res.get("ctx") and res["rows"]:
            # every time came from the cache, so no worker reported the context: build it here (mesh is cached)
            try:
                res["ctx"] = case_ctx(c, s, self.cache_root)
            except Exception as exc:
                self.job.log(f"[{c.name}] context (molecule mass, box) unavailable: {exc}", "warn")
        res["rows"].sort(key=lambda r: r["time"])
        for r in res["rows"]:
            derive_gas_density(r, (res.get("ctx") or {}).get("mass"))
        derive_normalized(res["rows"])
        if "_cache" in res:
            try:
                self.cache.save(c, key, res["_cache"])
            except OSError as exc:
                self.job.log(f"[{c.name}] cache write failed: {exc}", "warn")
            del res["_cache"]
        res["summary"] = summarize(res["rows"], s)
        if res.get("incomplete"):
            res["status"] = "stopped" if self.job.stop_flag.is_set() else "error"
        else:
            res["status"] = "error" if res["errors"] else "done"
        res["skipped"] = [{"time": r["time_name"], "reason": r.get("reason", "")} for r in res["rows"] if r.get("status") != "ok"]
        self.job.detail["cases_done"] = self.job.detail.get("cases_done", 0) + 1
        self.job.detail["current"] = c.name
        self.job.log(f"[{c.name}] {res['status']}: {len(res['rows'])} times")


    def _empty(self, c: CaseData, status: str, reason: str) -> dict:
        return {"name": c.name, "status": status, "reason": reason, "rows": [], "errors": [], "summary": {},
                "ctx": {}, "meta": c.meta, "info": c.info()}

    def _meta(self, cases: list[CaseData], s: Settings) -> dict:
        return {"software": "mdfoam-droplet-viewer", "version": __version__,
                "created": _time.strftime("%Y-%m-%d %H:%M:%S"), "settings": s.to_dict(),
                "settings_hash": s.hash(), "per_time_hash": s.hash(PER_TIME_KEYS),
                "cases": [{"name": c.name, "data_dir": str(c.data_dir), "n_times": len(c.times)} for c in cases]}
