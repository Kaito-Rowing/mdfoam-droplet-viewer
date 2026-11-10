"""Generate small, deterministic synthetic cases; no research data is included."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

from ._synthetic import grid_mesh, cell_centres, write_mesh, write_field
from .settings import Settings


def generate(destination: Path, n=14, steps=12):
    destination = Path(destination)
    if destination.exists() and any(destination.iterdir()):
        raise FileExistsError("demo destination must be empty")
    destination.mkdir(parents=True, exist_ok=True)
    x = np.linspace(-5e-9, 5e-9, n+1)
    z = np.linspace(0, 7e-9, n+1)
    geometry = grid_mesh(x, x, z)
    cc = cell_centres(x, x, z)
    for index, radius in enumerate((3.0e-9, 3.7e-9)):
        case = destination / f"sample-{index+1}" / "main"
        write_mesh(case, *geometry, zones={"sample-fluid": np.arange(len(cc))})
        (case/'constant/moleculeProperties').write_text("sample\n{\nsiteMasses (6.6e-26);\n}\n", encoding="utf-8")
        for t in np.linspace(0.1e-9, 2.5e-9, steps):
            r = radius*np.exp(-1e8*t)
            distance = np.linalg.norm(cc - [0, 0, -0.3*r], axis=1)
            density = 5 + 795/(1+np.exp(np.clip((distance-r)/0.15e-9, -60, 60)))
            time = case/f"{t:.9g}"
            write_field(time/'density_sample', density)
            write_field(time/'count_sample', density/6.6e-26, "[0 -3 0 0 0 0 0]")
            write_field(time/'T_sample', np.full(len(cc), 300.0), "[0 0 0 1 0 0 0]")
    settings = Settings(density_field="density_sample", number_density_field="count_sample", temperature_field="T_sample",
                        species="sample", liquid_min=400.0, gas_max=30.0, region_zones=["sample-fluid"], substrate_position=0.0)
    (destination/'settings.json').write_text(json.dumps(settings.to_dict(), indent=2), encoding="utf-8")
    definition = {"id": "exponential-demo", "label": "Exponential example", "expression": "a*exp(-k*x)",
                  "x_quantity": "time", "y_quantity": "r_norm", "x_unit": "s", "y_unit": "1",
                  "parameters": {"a": {"initial": 1.0, "lower": 0.0}, "k": {"initial": 1e8, "lower": 0.0}}}
    (destination/'register.json').write_text(json.dumps({"definition": definition}, indent=2), encoding="utf-8")
    (destination/'fit.json').write_text(json.dumps({"model_id": definition["id"], "case": "sample-1"}, indent=2), encoding="utf-8")
    return {"path": str(destination.resolve()), "cases": 2, "steps": steps}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    print(json.dumps(generate(args.destination)))


if __name__ == "__main__":
    main()
