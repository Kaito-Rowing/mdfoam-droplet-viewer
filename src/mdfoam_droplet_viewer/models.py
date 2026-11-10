"""Portable expression models and explicitly installed Python model extensions.

Expressions are interpreted as a small AST. No Python eval/exec is used.
"""
from __future__ import annotations

import ast
import importlib.metadata
import json
import keyword
import math
import operator
from pathlib import Path
from typing import Literal

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, model_validator
from scipy.optimize import least_squares

FUNCTIONS = {k: getattr(np, k) for k in ("exp", "log", "log10", "sqrt", "sin", "cos", "tan", "abs", "tanh")}
FUNCTIONS.update(minimum=np.minimum, maximum=np.maximum)
CONSTANTS = {"pi": math.pi, "e": math.e}
OPERATORS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
             ast.Div: operator.truediv, ast.Pow: operator.pow}


class Parameter(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    initial: float
    lower: float | None = None
    upper: float | None = None
    unit: str = ""

    @model_validator(mode="after")
    def bounds(self):
        lo = self.lower if self.lower is not None else -math.inf
        hi = self.upper if self.upper is not None else math.inf
        if not lo < hi or not lo <= self.initial <= hi:
            raise ValueError("require lower < upper and lower <= initial <= upper")
        return self


class ModelDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
    label: str = ""
    kind: Literal["expression", "python"] = "expression"
    expression: str = ""
    x_quantity: str = "time"
    y_quantity: str = "volume"
    x_unit: str = "s"
    y_unit: str = "m^3"
    parameters: dict[str, Parameter] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_definition(self):
        if len(self.parameters) > 20:
            raise ValueError("at most 20 parameters")
        for name in self.parameters:
            if not name.isidentifier() or keyword.iskeyword(name) or name in {"x", *FUNCTIONS, *CONSTANTS}:
                raise ValueError(f"invalid/reserved parameter name: {name}")
        if self.kind == "expression":
            expression_tree(self.expression, self.parameters)
        return self


def expression_tree(expression: str, parameters: dict):
    if not expression or len(expression) > 2000:
        raise ValueError("expression length must be 1..2000")
    try:
        tree = ast.parse(expression, mode="eval")
    except (SyntaxError, RecursionError) as exc:
        raise ValueError("invalid expression") from exc
    nodes = list(ast.walk(tree))
    if len(nodes) > 200:
        raise ValueError("expression is too complex")
    allowed = (ast.Expression, ast.BinOp, ast.UnaryOp, ast.Constant, ast.Name, ast.Load,
               ast.Call, ast.UAdd, ast.USub, *OPERATORS)
    for node in nodes:
        if not isinstance(node, allowed):
            raise ValueError(f"expression construct not permitted: {type(node).__name__}")
        if isinstance(node, ast.Constant) and (type(node.value) not in (int, float) or not math.isfinite(node.value) or abs(node.value) > 1e100):
            raise ValueError("only finite numeric constants up to 1e100 are allowed")
        if isinstance(node, ast.Name) and node.id not in {"x", *parameters, *FUNCTIONS, *CONSTANTS}:
            raise ValueError(f"unknown expression name: {node.id}")
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in FUNCTIONS or node.keywords:
                raise ValueError("only calls to the documented math functions are permitted")
            count = 2 if node.func.id in ("minimum", "maximum") else 1
            if len(node.args) != count:
                raise ValueError(f"{node.func.id} requires {count} argument(s)")
    return tree.body


def interpret(node, values):
    if isinstance(node, ast.Constant):
        return float(node.value)
    if isinstance(node, ast.Name):
        if node.id not in values:
            raise ValueError(f"{node.id} must be called as a function")
        return values[node.id]
    if isinstance(node, ast.UnaryOp):
        value = interpret(node.operand, values)
        return -value if isinstance(node.op, ast.USub) else value
    if isinstance(node, ast.BinOp):
        return OPERATORS[type(node.op)](interpret(node.left, values), interpret(node.right, values))
    if isinstance(node, ast.Call):
        return FUNCTIONS[node.func.id](*(interpret(a, values) for a in node.args))
    raise ValueError("invalid expression node")


class ModelRegistry:
    def __init__(self, root: Path):
        self.directory = Path(root) / "models"
        self.plugins = {}
        self.plugin_errors = []
        for entry in importlib.metadata.entry_points(group="mdfoam_droplet_viewer.models"):
            try:
                extension = entry.load()
                definition = ModelDefinition.model_validate(extension.definition)
                if definition.kind != "python" or definition.id in self.plugins:
                    raise ValueError("extension must have a unique Python model id")
                self.plugins[definition.id] = (definition, extension)
            except Exception as exc:
                self.plugin_errors.append({"entry_point": entry.name, "reason": str(exc)})

    def register(self, definition: dict, overwrite: bool = False):
        model = ModelDefinition.model_validate(definition)
        if model.kind != "expression":
            raise ValueError("Python models must be installed as entry points, not submitted as code")
        if model.id in self.plugins:
            raise ValueError("id belongs to an installed Python extension")
        self.directory.mkdir(parents=True, exist_ok=True)
        destination = self.directory / (model.id + ".json")
        with destination.open("w" if overwrite else "x", encoding="utf-8") as stream:
            json.dump(model.model_dump(), stream, indent=2)
        return model.model_dump()

    def get(self, name: str) -> ModelDefinition:
        if name in self.plugins:
            return self.plugins[name][0]
        if not isinstance(name, str) or not name or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for c in name):
            raise ValueError("invalid model id")
        return ModelDefinition.model_validate_json((self.directory / (name + ".json")).read_text(encoding="utf-8"))

    def list(self):
        definitions = [v[0].model_dump() for v in self.plugins.values()]
        for path in sorted(self.directory.glob("*.json")):
            definitions.append(ModelDefinition.model_validate_json(path.read_text(encoding="utf-8")).model_dump())
        return definitions

    def predict(self, model, x, parameters=None, context=None):
        x = np.asarray(x, dtype=float)
        if x.ndim != 1 or x.size > 100000 or not np.isfinite(x).all():
            raise ValueError("x must contain at most 100000 finite scalar values")
        params = {k: p.initial for k, p in model.parameters.items()}
        unknown = set(parameters or {}) - set(params)
        if unknown:
            raise ValueError(f"unknown parameters: {sorted(unknown)}")
        params.update(parameters or {})
        if not all(isinstance(v, (int, float)) and math.isfinite(v) for v in params.values()):
            raise ValueError("parameters must be finite numbers")
        with np.errstate(all="ignore"):
            if model.kind == "python":
                output = self.plugins[model.id][1].predict(x.copy(), dict(params), dict(context or {}))
            else:
                output = interpret(expression_tree(model.expression, model.parameters), {"x": x, **CONSTANTS, **params})
        output = np.asarray(output, dtype=float)
        if output.ndim == 0:
            output = np.full(x.shape, output)
        if output.shape != x.shape or not np.isfinite(output).all():
            raise ValueError("model is undefined or non-finite on the requested domain")
        return output

    def evaluate(self, name, x, parameters=None, context=None):
        model = self.get(name)
        return {"model": model.model_dump(), "x": x,
                "y": self.predict(model, x, parameters, context).tolist(), "parameters": parameters or {k:v.initial for k,v in model.parameters.items()}}

    def fit(self, name, x, y, window=None, context=None, max_nfev=2000):
        model = self.get(name)
        x, y = np.asarray(x, dtype=float), np.asarray(y, dtype=float)
        if x.ndim != 1 or x.shape != y.shape or x.size > 100000:
            raise ValueError("x and y must have the same one-dimensional shape, at most 100000 points")
        mask = np.isfinite(x) & np.isfinite(y)
        if window:
            lo, hi = window.get("min"), window.get("max")
            if lo is not None:
                if not math.isfinite(lo):
                    raise ValueError("window.min must be finite")
                mask &= x >= lo
            if hi is not None:
                if not math.isfinite(hi) or (lo is not None and lo > hi):
                    raise ValueError("invalid window bounds")
                mask &= x <= hi
        n = int(mask.sum())
        result = {"model": model.model_dump(), "status": "failed", "reason": "", "n_points": n,
                  "n_excluded": int(x.size - n), "window": window, "context": context or {}}
        if n < max(2, len(model.parameters) + 1):
            return {**result, "reason": "insufficient finite points for the number of parameters"}
        x, y = x[mask], y[mask]
        keys = list(model.parameters)
        initial = [model.parameters[k].initial for k in keys]
        lower = [model.parameters[k].lower if model.parameters[k].lower is not None else -np.inf for k in keys]
        upper = [model.parameters[k].upper if model.parameters[k].upper is not None else np.inf for k in keys]
        scale = max(float(np.max(np.abs(y))), np.finfo(float).tiny)
        # Optimize dimensionless parameter coordinates. This also avoids SciPy moving
        # a 1e-26 volume parameter to 1e-10 when making a bound-feasible initial point.
        parameter_scale = np.array([abs(v) if v else
            (abs(upper[i]) if math.isfinite(upper[i]) and upper[i] else 1.0)
            for i, v in enumerate(initial)])
        try:
            def residual(values):
                return (self.predict(model, x, dict(zip(keys, values * parameter_scale)), context) - y) / scale
            if keys:
                optimized = least_squares(residual, np.array(initial) / parameter_scale,
                    bounds=(np.array(lower) / parameter_scale, np.array(upper) / parameter_scale),
                    x_scale="jac", max_nfev=max_nfev)
                params = dict(zip(keys, (optimized.x * parameter_scale).tolist()))
                success, reason = optimized.success, optimized.message
            else:
                params, success, reason = {}, True, "no free parameters"
            pred = self.predict(model, x, params, context)
            residual_scaled = (pred-y) / scale
            sse = float(np.sum(residual_scaled**2))
            sst = float(np.sum(((y-y.mean()) / scale)**2))
            return {**result, "status": "converged" if success else "failed", "reason": str(reason),
                    "parameters": params, "rmse": math.sqrt(sse/n)*scale, "r2": 1-sse/sst if sst > 0 else None,
                    "x": x.tolist(), "observed": y.tolist(), "predicted": pred.tolist()}
        except (ValueError, FloatingPointError, OverflowError) as exc:
            return {**result, "reason": str(exc)}
