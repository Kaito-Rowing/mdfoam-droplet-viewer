# Model extension contract

The shared registry supports portable expression definitions and deliberately installed Python extensions. Neither interface contains a particular evaporation or Kelvin theory.

## Expression definition

```json
{
  "id": "decay-example",
  "label": "Exponential example",
  "kind": "expression",
  "expression": "a*exp(-k*x)",
  "x_quantity": "time",
  "y_quantity": "r_norm",
  "x_unit": "s",
  "y_unit": "1",
  "parameters": {
    "a": {"initial": 1.0, "lower": 0.0},
    "k": {"initial": 100000000.0, "lower": 0.0, "unit": "1/s"}
  }
}
```

Register with `register_model {definition: ...}`. IDs use letters/digits/hyphens/underscores and begin with a letter. Parameter names must be Python-style identifiers and cannot shadow `x`, constants, or math functions. Bounds satisfy `lower < upper` and contain the initial value. Null bounds mean unbounded.

Allowed operations: numeric constants, parentheses, `+ - * / **`, unary signs, and calls to `exp`, `log`, `log10`, `sqrt`, `sin`, `cos`, `tan`, `tanh`, `abs`, `minimum`, `maximum`. There are no imports, attributes, indexing, comprehensions, conditionals, file operations or arbitrary calls. Definitions are limited to 20 parameters, 200 syntax nodes and 2,000 characters; evaluations to 100,000 points.

## Python model

Install the working example in the same environment as the application:

```sh
python -m pip install ./examples/model-plugin
```

Restart the GUI/MCP process after installing or changing plugins. `list_models` should include `plugin-linear`. An entry point points to an object or module exposing:

```python
definition = {
    "id": "my-model", "kind": "python", "label": "My model",
    "x_quantity": "time", "y_quantity": "r_norm", "x_unit": "s", "y_unit": "1",
    "parameters": {"a": {"initial": 1.0}}
}

def predict(x, parameters, context):
    return parameters["a"] * x
```

`x` is a finite 1D NumPy array; return a same-shape finite array or a scalar to broadcast. Parameter values are floats. When fitting a case, `context` contains available analysis context such as molecular mass, periods and zones, plus explicit request context overrides. Do not mutate inputs or source cases. Numerical integration and other complex calculations can live inside `predict`; their outputs must meet the same contract.

Expose the plugin in its `pyproject.toml`:

```toml
[project.entry-points."mdfoam_droplet_viewer.models"]
my-model = "my_package.my_model"
```

The registry discovers installed entry points at initialization. Import/definition failures appear in `plugin_errors`. Python models cannot be registered by uploading a module path or source string through HTTP/MCP. Installed extensions have the same filesystem privileges as the application.

## Fitting and reproducibility

The common fitter uses bounded nonlinear least squares, with scaled residuals and parameter coordinates for very small physical quantities. Finite observations in the selected interval are used; at least `max(2, number_of_parameters + 1)` points are required. R² is null for constant observations. Optimizer non-convergence and invalid model domains return a failed status with a reason.

Snapshots include model definitions, parameters, observed/predicted curves and context. Export stored curves without a plugin using their `fit_id`; install the plugin to evaluate or refit it. Model definitions and settings are independent of the predecessor application's cache.
