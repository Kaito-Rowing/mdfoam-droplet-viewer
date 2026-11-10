"""Versioned application operations shared by GUI, CLI, and stdio MCP.

Each process owns one active dataset and one analysis job. Named results and
expression definitions are portable files in the configured application root.
"""
from __future__ import annotations

import json
import threading
import uuid
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, TypeAdapter

from . import export, saved
from .models import ModelRegistry, ModelDefinition
from .settings import Settings, WATER_PRESET

API_VERSION = "1.0"


class Request(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Empty(Request):
    pass


class Discover(Request):
    path: str
    subpath: str = "auto"


class Inspect(Request):
    case: str = ""


class Validate(Request):
    settings: dict


class Analyze(Validate):
    names: list[str] | None = None
    remote_tar: bool = True


class Job(Request):
    job_id: str = ""


class Register(Request):
    definition: ModelDefinition
    overwrite: bool = False


class Evaluate(Request):
    model_id: str
    x: list[float] = Field(max_length=100000)
    parameters: dict[str, float] = Field(default_factory=dict)
    context: dict = Field(default_factory=dict)


class Fit(Request):
    model_id: str
    x: list[float | None] | None = Field(default=None, max_length=100000)
    y: list[float | None] | None = Field(default=None, max_length=100000)
    case: str = ""
    window: dict[str, float | None] | None = None
    context: dict = Field(default_factory=dict)
    max_nfev: int = Field(default=2000, ge=1, le=10000)


class Save(Request):
    name: str
    overwrite: bool = False


class Open(Request):
    name: str


class Figure(Request):
    spec: dict
    output: str


class ModelFigure(Request):
    fit_id: str
    output: str


class Compare(Request):
    names: list[str] = Field(min_length=1, max_length=8)


class Connect(Request):
    host: str
    port: int = Field(default=22, ge=1, le=65535)
    username: str = ""
    key_file: str = ""
    password: str = ""
    passphrase: str = ""


class RemoteList(Request):
    path: str = "."


REQUESTS = {
    "schema": Empty, "discover": Discover, "inspect": Inspect, "validate_settings": Validate,
    "analyze": Analyze, "job_status": Job, "cancel": Job, "results": Empty,
    "list_models": Empty, "register_model": Register, "evaluate_model": Evaluate, "fit_model": Fit,
    "save": Save, "open": Open, "list_saved": Empty, "export_figure": Figure, "export_model_figure": ModelFigure,
    "compare": Compare,
    "remote_hosts": Empty, "remote_connect": Connect, "remote_disconnect": Empty,
    "remote_list": RemoteList, "remote_discover": Discover,
}
_registry = None
_registry_root = None
_lock = threading.RLock()


def registry():
    from . import server
    global _registry, _registry_root
    if _registry is None or _registry_root != server.CACHE_ROOT:
        _registry_root = server.CACHE_ROOT
        _registry = ModelRegistry(_registry_root)
    return _registry


def decoded(response):
    return json.loads(response.body) if hasattr(response, "body") else response


def schema():
    return {
        "operations": {name: model.model_json_schema() for name, model in REQUESTS.items()},
        "settings": TypeAdapter(Settings).json_schema(), "model": ModelDefinition.model_json_schema(),
        "quantities": {k: {"kind": v[0], "label": v[1]} for k, v in export.QUANTITIES.items()},
        "presets": {"water_example": {"settings": WATER_PRESET,
                    "note": "Illustrative water settings; verify thresholds for your model and temperature."}},
        "units": "Analysis and model input/output use SI. Model unit labels are metadata, not automatic conversion.",
        "job_scope": "One active dataset/job per server process; job ids are valid for that process only.",
    }


def operate(operation: str, arguments: dict | None = None) -> dict[str, Any]:
    from fastapi import HTTPException
    try:
        if operation not in REQUESTS:
            raise ValueError(f"unknown operation: {operation}")
        req = REQUESTS[operation].model_validate(arguments or {})
        with _lock:
            result = _dispatch(operation, req)
        return {"api_version": API_VERSION, "ok": True, "result": result}
    except Exception as exc:
        code = ("invalid_input" if isinstance(exc, (ValueError, TypeError, ValidationError)) else
                "not_found" if isinstance(exc, FileNotFoundError) else
                "already_exists" if isinstance(exc, FileExistsError) else
                "operation_failed")
        if isinstance(exc, HTTPException):
            code = {400: "invalid_input", 404: "not_found", 409: "conflict"}.get(exc.status_code, code)
        return {"api_version": API_VERSION, "ok": False, "error": {"code": code, "reason": str(getattr(exc, "detail", exc))}}


def _dispatch(op, req):
    from . import server as s, figures
    if op == "schema":
        return schema()
    if op == "discover":
        return decoded(s.scan(s.ScanReq(**req.model_dump())))
    if op == "inspect":
        result = decoded(s.system(req.case))
        result["candidates"] = {
            "density": [f["name"] for f in result["fields"] if f["kind"] in ("mass", "number")],
            "temperature": [f["name"] for f in result["fields"] if f["dimensions"].split() == ["0", "0", "0", "1", "0", "0", "0"]],
            "species": list(result["molecule_masses"]),
            "zones": result["mesh"].get("zones", {}),
        }
        return result
    if op == "validate_settings":
        return decoded(s.validate(req.model_dump()))
    if op == "analyze":
        settings = Settings.from_dict(req.settings)
        errors = settings.validate()
        if errors:
            raise ValueError("; ".join(errors))
        # Reject ambiguity/missing selections before starting work on local data.
        if s.S.source.get("type") != "remote":
            for case in s.S.cases:
                if req.names and case.name not in req.names:
                    continue
                if case.times and settings.density_field not in case.available_fields():
                    raise ValueError(f"{case.name}: density field {settings.density_field!r} is unavailable")
        result = decoded(s.analyze(s.AnalyzeReq(**req.model_dump())))
        return {**result, "job_id": s.S.job_id}
    if op in ("job_status", "cancel"):
        if req.job_id and req.job_id != s.S.job_id:
            raise FileNotFoundError("job id is not active in this process")
        if op == "cancel":
            s.S.analyzer.stop()
        return {"job_id": s.S.job_id, **s.S.analyzer.job.view()}
    if op == "results":
        return {"results": decoded(s.results())["results"], "models": s.S.models}
    if op == "list_models":
        return {"models": registry().list(), "plugin_errors": registry().plugin_errors}
    if op == "register_model":
        return registry().register(req.definition.model_dump(), req.overwrite)
    if op == "evaluate_model":
        return registry().evaluate(req.model_id, req.x, req.parameters, req.context)
    if op == "fit_model":
        context = req.context
        if req.case:
            rows = s.S.analyzer.results.get(req.case, {}).get("rows")
            if rows is None:
                raise FileNotFoundError(f"no results for case {req.case!r}")
            model = registry().get(req.model_id)
            rows = [row for row in rows if row.get("status") == "ok"]
            x = [row.get(model.x_quantity) for row in rows]
            y = [row.get(model.y_quantity) for row in rows]
            context = {**(s.S.analyzer.results[req.case].get("ctx") or {}), **context}
        else:
            if req.x is None or req.y is None:
                raise ValueError("provide case or both x and y")
            x, y = req.x, req.y
        result = registry().fit(req.model_id, x, y, req.window, context, req.max_nfev)
        fit_id = uuid.uuid4().hex
        result.update(fit_id=fit_id, case=req.case)
        s.S.models[fit_id] = result
        return result
    if op == "save":
        return decoded(s.saved_save(s.SaveReq(**req.model_dump())))
    if op == "open":
        result = decoded(s.saved_open(s.OpenReq(name=req.name)))
        result["models"] = s.S.models
        return result
    if op == "list_saved":
        return decoded(s.saved_list())
    if op == "compare":
        return decoded(s.compare_saved(s.CompareReq(names=req.names)))
    if op in ("export_figure", "export_model_figure"):
        if op == "export_figure":
            data, report = figures.render(req.spec, s._fig_source())
        else:
            fit = s.S.models.get(req.fit_id)
            if not fit or fit["status"] != "converged":
                raise ValueError("a converged saved fit is required")
            model = fit["model"]
            order = sorted(range(len(fit["x"])), key=fit["x"].__getitem__)
            spec = {"kind": "xy", "series": [
                {"x": fit["x"], "y": fit["observed"], "name": "observed"},
                {"x": [fit["x"][i] for i in order], "y": [fit["predicted"][i] for i in order], "name": model["label"] or model["id"], "mode": "lines"},
            ], "xlabel": f'{model["x_quantity"]} [{model["x_unit"]}]', "ylabel": f'{model["y_quantity"]} [{model["y_unit"]}]'}
            data, report = figures.render(spec, s._fig_source())
        dest = Path(req.output)
        expected = report["size"]["format"]
        if dest.suffix.lower().lstrip(".") not in (expected, "jpg" if expected == "jpeg" else expected):
            raise ValueError(f"output suffix must match figure format {expected}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        return {"output": str(dest.resolve()), **report}
    if op == "remote_hosts":
        return decoded(s.remote_hosts())
    if op == "remote_connect":
        return decoded(s.remote_connect(s.RemoteConnectReq(**req.model_dump())))
    if op == "remote_disconnect":
        return decoded(s.remote_disconnect())
    if op == "remote_list":
        return decoded(s.remote_list(req.path))
    if op == "remote_discover":
        return decoded(s.remote_scan(s.RemoteScanReq(**req.model_dump())))
    raise ValueError(f"unimplemented operation {op}")
