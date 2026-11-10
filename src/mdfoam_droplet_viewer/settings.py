"""Analysis settings: defaults, validation, and hashing (display-only options are not part of the hash)."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass, field, fields

K_B = 1.380649e-23  # J/K


@dataclass
class Settings:
    # --- input
    data_subpath: str = "auto"
    density_field: str = ""
    number_density_field: str = ""   # used for molecule counts (N = n V); "" -> derive from mass density
    temperature_field: str = ""
    molecule_mass: float = 0.0                  # kg; 0 -> read from moleculeProperties for the field's species
    species: str = ""                           # "" -> derived from the field suffix (rhoM_water -> water)

    # --- time steps used (also limits what is downloaded from a remote host)
    time_min: float | None = None               # s; None = from the first time
    time_max: float | None = None               # s; None = up to the newest time
    time_stride: int = 1                        # every n-th time in the range; the newest one is always included

    # --- phases (units of the density field)
    liquid_min: float | None = None                   # droplet threshold rho >= liquid_min
    gas_min: float = 0.0
    gas_max: float | None = None
    region_zones: list[str] | None = None       # zones for temperature/gas; None or [] = all, never infer by name
    droplet_zones: list[str] = field(default_factory=list)               # zones for volume/iso points ([] = all)

    # --- contact angle
    fit_lower: float = 0.5
    fit_upper: float = 1.0
    base_fraction: float = 0.05
    periodic_unwrap: bool = True
    periodic_override: dict = field(default_factory=dict)   # {"x": L, "y": L}; empty -> detected from cyclic patches

    # --- evaporation / summary
    zero_tolerance: float = 0.0
    zero_consecutive: int = 3
    mean_angle_percent: float = 100.0

    # --- temperature
    temperature_enabled: bool = True
    temp_rho_min: float | None = None           # None -> liquid_min
    temp_rho_max: float | None = None
    temp_average: str = "number"                # number | volume | simple

    # --- gas pressure
    pressure_enabled: bool = True
    gas_mode: str = "density"                   # density: gas_min <= rho <= gas_max | distance: away from droplet and wall
    gas_min_distance: float = 1.5e-9            # m, distance mode: from the nearest droplet (liquid) cell centre
    gas_min_height: float = 1.0e-9              # m, distance mode: above the bottom of the gas/temperature zones
    gas_temp: str = "cell"                      # cell: sum N_i T_i | liquid: droplet temperature | fixed: gas_temp_fixed
    gas_temp_fixed: float = 300.0               # K

    # Axis points away from the substrate; position is in the original world axis (m).
    substrate_normal: str = "+z"
    substrate_position: float | None = None

    def validate(self, density_kind: str = "mass") -> list[str]:
        errs = []
        if not self.density_field:
            errs.append("density_field is required; inspect the available fields")
        if self.substrate_normal not in ("+x", "-x", "+y", "-y", "+z", "-z"):
            errs.append("substrate_normal must be +x|-x|+y|-y|+z|-z")
        if self.substrate_position is not None and not math.isfinite(self.substrate_position):
            errs.append("substrate_position must be finite")
        if not math.isfinite(self.molecule_mass) or self.molecule_mass < 0:
            errs.append("molecule_mass must be >= 0 (0 means read from data)")
        for key in ("time_min", "time_max", "temp_rho_min", "temp_rho_max"):
            value = getattr(self, key)
            if value is not None and not math.isfinite(value):
                errs.append(f"{key} must be finite")
        if not isinstance(self.time_stride, int) or isinstance(self.time_stride, bool):
            errs.append("time_stride must be an integer")
        if self.temperature_enabled and not self.temperature_field:
            errs.append("temperature_field is required when temperature is enabled")
        if self.pressure_enabled and self.gas_temp == "cell" and not self.temperature_field:
            errs.append("temperature_field is required for cell-temperature pressure")
        nums = {"liquid_min": self.liquid_min, "gas_min": self.gas_min, "gas_max": self.gas_max,
                "zero_tolerance": self.zero_tolerance, "fit_lower": self.fit_lower, "fit_upper": self.fit_upper}
        for k, v in nums.items():
            if v is None or not math.isfinite(v):
                errs.append(f"{k} must be a finite number")
        if not errs:
            if not (self.gas_min <= self.gas_max < self.liquid_min):
                errs.append("require gas_min <= gas_max < liquid_min")
            if not (0 <= self.fit_lower < self.fit_upper <= 1):
                errs.append("require 0 <= fit_lower < fit_upper <= 1")
            if self.gas_min < 0:
                errs.append("gas_min must be >= 0")
        for key in ("gas_min_distance", "gas_min_height", "gas_temp_fixed", "base_fraction", "mean_angle_percent"):
            value = getattr(self, key)
            if value is None or not math.isfinite(value):
                errs.append(f"{key} must be finite")
        for key, value in self.periodic_override.items():
            if key not in ("x", "y", "z") or (value is not None and (not isinstance(value, (int, float)) or not math.isfinite(value))):
                errs.append("periodic_override requires x/y/z keys and finite numeric values or null")
        if self.time_stride < 1:
            errs.append("time_stride must be >= 1")
        if self.time_min is not None and self.time_max is not None and self.time_min > self.time_max:
            errs.append("time_min must be <= time_max")
        if self.zero_consecutive < 1:
            errs.append("zero_consecutive must be >= 1")
        if not (0 < self.mean_angle_percent <= 100):
            errs.append("mean_angle_percent must be in (0, 100]")
        if not (0 < self.base_fraction < 1):
            errs.append("base_fraction must be in (0, 1)")
        if self.temp_average not in ("number", "volume", "simple"):
            errs.append("temp_average must be number|volume|simple")
        if self.temp_rho_min is not None and self.temp_rho_max is not None and self.temp_rho_min > self.temp_rho_max:
            errs.append("temp_rho_min must be <= temp_rho_max")
        if self.gas_mode not in ("density", "distance"):
            errs.append("gas_mode must be density|distance")
        if self.gas_min_distance is None or self.gas_min_distance < 0 or self.gas_min_height is None or self.gas_min_height < 0:
            errs.append("gas distance / height must be >= 0")
        if self.gas_temp not in ("cell", "liquid", "fixed"):
            errs.append("gas_temp must be cell|liquid|fixed")
        if self.gas_temp == "fixed" and not (self.gas_temp_fixed and self.gas_temp_fixed > 0):
            errs.append("gas_temp_fixed must be > 0 K")
        return errs

    @property
    def temp_min(self) -> float:
        return self.liquid_min if self.temp_rho_min is None else self.temp_rho_min

    def pick_times(self, items: list, value=lambda x: x.value) -> list:
        """The items (time entries, sorted by time) inside the time range, thinned to every time_stride-th."""
        vals = [value(x) for x in items]
        keep = select_times(vals, self.time_stride, self.time_min, self.time_max)
        return [x for i, x in enumerate(items) if i in keep]

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Settings":
        known = {f.name for f in fields(cls)}
        d = d or {}
        unknown = set(d) - known
        if unknown:
            raise ValueError(f"unknown settings: {sorted(unknown)}")
        # Pydantic validates JSON types before arithmetic and NumPy operations.
        from pydantic import TypeAdapter
        return TypeAdapter(cls).validate_python(d)

    def hash(self, keys: tuple[str, ...] | None = None) -> str:
        d = Settings.from_dict(self.to_dict()).to_dict()
        if keys is not None:
            d = {k: d[k] for k in keys}
        return hashlib.sha1(json.dumps(d, sort_keys=True, default=str).encode()).hexdigest()[:12]


# Settings that affect per-time computations (cache key). Summary-level settings are cheap and recomputed.
PER_TIME_KEYS = (
    "density_field", "number_density_field", "temperature_field", "molecule_mass", "species", "substrate_normal", "substrate_position",
    "liquid_min", "gas_min", "gas_max", "region_zones", "droplet_zones",
    "fit_lower", "fit_upper", "base_fraction", "periodic_unwrap", "periodic_override",
    "temperature_enabled", "temp_rho_min", "temp_rho_max", "temp_average", "pressure_enabled",
    "gas_mode", "gas_min_distance", "gas_min_height", "gas_temp", "gas_temp_fixed",
)



def select_times(values: list[float], stride: int = 1, t_min: float | None = None,
                 t_max: float | None = None) -> set[int]:
    """Indices of the time values kept: inside [t_min, t_max], every stride-th counted from the first one,
    plus the newest one (so a running case always shows its latest state). Bounds allow float round-off."""
    def tol(x):
        return 1e-9 * abs(x)
    order = sorted(range(len(values)), key=values.__getitem__)
    inside = [i for i in order if (t_min is None or values[i] >= t_min - tol(t_min))
              and (t_max is None or values[i] <= t_max + tol(t_max))]
    keep = set(inside[::max(1, int(stride or 1))])
    if inside:
        keep.add(inside[-1])
    return keep


def resolve_region_zones(setting: list[str] | None, zone_names: list[str]) -> list[str]:
    """None or [] selects all cells; zone names have no implicit physical meaning."""
    return [] if setting is None else list(setting)



def species_from_field(name: str) -> str:
    """rhoM_water -> water, rhoN_Average_water -> water."""
    parts = name.split("_")
    return parts[-1] if len(parts) > 1 else ""


def density_kind(dimensions: str, field_name: str = "") -> str:
    """'mass' for [1 -3 ...], 'number' for [0 -3 ...], else 'unknown'."""
    d = dimensions.split()
    if len(d) >= 2:
        if d[0] == "1" and d[1] == "-3":
            return "mass"
        if d[0] == "0" and d[1] == "-3":
            return "number"
    if field_name.startswith("rhoM"):
        return "mass"
    if field_name.startswith("rhoN"):
        return "number"
    return "unknown"


WATER_PRESET = {
    "density_field": "rhoM_water", "number_density_field": "rhoN_water",
    "temperature_field": "temperature_water", "liquid_min": 500.0, "gas_min": 0.0, "gas_max": 50.0,
    "species": "water", "molecule_mass": 2.9915e-26,
}

def water_settings(**overrides) -> Settings:
    """Explicit illustrative water preset; thresholds must be checked for the model/temperature."""
    return Settings(**{**WATER_PRESET, **overrides})
