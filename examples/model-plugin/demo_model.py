"""Minimal extension contract; replace predict with your numerical model."""
import numpy as np

definition = {
    "id": "plugin-linear", "label": "Installed linear example", "kind": "python",
    "x_quantity": "time", "y_quantity": "r_norm", "x_unit": "s", "y_unit": "1",
    "parameters": {"a": {"initial": 1.0}, "b": {"initial": -1e8, "unit": "1/s"}},
}


def predict(x, parameters, context):
    """Return one finite y per x; x is a 1D NumPy array in SI units.

    context contains data-derived mass, periods, zones etc. when fitting a case.
    Models must not mutate their input or the source case data.
    """
    return parameters["a"] + parameters["b"] * np.asarray(x)
