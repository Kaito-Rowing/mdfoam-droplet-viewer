"""Core calculations on synthetic data with known answers. Units: SI (m, m^3, s, K, Pa)."""

import math
import zipfile
import io

import numpy as np
import pytest
from scipy.integrate import solve_ivp

from mdfoam_droplet_viewer import foam
from mdfoam_droplet_viewer.analysis import analyze_time, build_context, evaporation_time, summarize
from mdfoam_droplet_viewer.case import CaseData, discover_cases
from mdfoam_droplet_viewer.droplet import contact_angle, droplet_points, equivalent_radius, fit_sphere
from mdfoam_droplet_viewer.export import build_csv_zip
from mdfoam_droplet_viewer.mesh import compute_mesh
from mdfoam_droplet_viewer.settings import K_B, water_settings as Settings

from synth import MOLPROPS, cell_centres, cell_volumes, grid_mesh, write_field, write_mesh

NM = 1e-9


def make_grid(n=24, L=12.0, zbot=-1.0, ztop=8.0, nz=27, nonuniform=False):
    xs = np.linspace(-L / 2, L / 2, n + 1) * NM
    ys = xs.copy()
    if nonuniform:
        zs = (zbot + (ztop - zbot) * np.linspace(0, 1, nz + 1) ** 1.3) * NM
    else:
        zs = np.linspace(zbot, ztop, nz + 1) * NM
    return xs, ys, zs


# ---------------------------------------------------------------- mesh / volume
def test_nonuniform_volumes_and_centres():
    xs, ys, zs = make_grid(n=6, nz=7, nonuniform=True)
    m = compute_mesh(*grid_mesh(xs, ys, zs)[:4], grid_mesh(xs, ys, zs)[4], {})
    np.testing.assert_allclose(m.cell_volumes, cell_volumes(xs, ys, zs), rtol=1e-10)
    np.testing.assert_allclose(m.cell_centres, cell_centres(xs, ys, zs), atol=1e-20)
    assert not np.isclose(m.cell_volumes.min(), m.cell_volumes.max(), atol=0)
    assert m.periodic["x"] == pytest.approx(12 * NM)
    assert m.periodic["y"] == pytest.approx(12 * NM)
    assert "z" not in m.periodic


def test_volume_sum_and_equivalent_radius(tmp_path):
    xs, ys, zs = make_grid(n=6, nz=7, nonuniform=True)
    pts, faces, own, nei, patches = grid_mesh(xs, ys, zs)
    write_mesh(tmp_path, pts, faces, own, nei, patches)
    vol = cell_volumes(xs, ys, zs)
    rho = np.zeros(vol.size)
    sel = np.arange(0, vol.size, 3)
    rho[sel] = 800.0
    rho[1] = 500.0          # exactly at threshold -> included
    rho[2] = 499.999        # just below -> excluded
    write_field(tmp_path / "1e-10" / "rhoM_water", rho)
    c = CaseData.open("c", tmp_path, tmp_path)
    m = c.load_mesh()
    s = Settings(temperature_enabled=False, pressure_enabled=False)
    row = analyze_time(c, "1e-10", s, build_context(c, m, s))
    expect = vol[sel].sum() + vol[1]
    assert row["volume"] == pytest.approx(expect, rel=1e-10)
    assert row["r_eq"] == pytest.approx((3 * expect / (4 * math.pi)) ** (1 / 3), rel=1e-10)
    assert equivalent_radius(0.0) == 0.0


# ---------------------------------------------------------------- sphere fit / contact angle
def test_sphere_fit_exact_nanoscale():
    rng = np.random.default_rng(1)
    v = rng.normal(size=(200, 3)); v /= np.linalg.norm(v, axis=1)[:, None]
    v = v[v[:, 2] > 0.2]
    c = np.array([1.0, -2.0, -0.7]) * NM
    p = c + 2.5 * NM * v
    f = fit_sphere(p)
    assert f.ok and f.R == pytest.approx(2.5 * NM, rel=1e-9)
    assert f.zc == pytest.approx(c[2], abs=1e-18)


def test_sphere_fit_degenerate_and_small():
    ang = np.linspace(0, 2 * np.pi, 20, endpoint=False)
    circle = np.column_stack([np.cos(ang), np.sin(ang), np.zeros_like(ang)]) * NM
    assert not fit_sphere(circle).ok          # coplanar circle: sphere not determined
    assert not fit_sphere(circle[:3]).ok      # < 4 points
    v = np.random.default_rng(2).normal(size=(50, 3)); v /= np.linalg.norm(v, axis=1)[:, None]
    assert not fit_sphere(1e-14 * v).ok       # radius < 1e-12 m


def cap_density(cc, centre, R, slope=200.0):
    """Density linear in distance from the sphere surface, 0 below the substrate (z<0)."""
    d = np.linalg.norm(cc - centre, axis=1)
    rho = np.clip(500.0 + slope * (R - d) / NM, 0, 1000.0)
    rho[cc[:, 2] < 0] = 0.0
    return rho


@pytest.mark.parametrize("theta_deg", [60.0, 90.0, 120.0])
# centred / on the x boundary / on the corner (both walls) / just off the corner
@pytest.mark.parametrize("shift", [(0.0, 0.0), (6.0, 0.0), (6.0, 6.0), (5.3, -5.6)])
def test_contact_angle_cap(theta_deg, shift):
    xs, ys, zs = make_grid(n=60, L=12.0, zbot=-1.0, ztop=7.0, nz=80)
    pts, faces, own, nei, patches = grid_mesh(xs, ys, zs)
    m = compute_mesh(pts, faces, own, nei, patches, {})
    R = 3.0 * NM
    zc = -R * math.cos(math.radians(theta_deg))
    centre = np.array([shift[0] * NM, shift[1] * NM, zc])
    cc = m.cell_centres
    # periodic distance in x and y so a droplet on the boundary is one droplet
    L = 12 * NM
    dxy = cc[:, :2] - centre[:2]; dxy -= L * np.round(dxy / L)
    rho = cap_density(np.column_stack([centre[:2] + dxy, cc[:, 2]]), centre, R)
    p = droplet_points(m, rho, 500.0, m.periodic, True)
    cr = contact_angle(p, 0.5, 1.0)
    assert cr.ok, cr.reason
    # The iso-surface meets the substrate between cell centres; allow a few degrees for grid effects.
    assert cr.angle_deg == pytest.approx(theta_deg, abs=4.0)
    assert cr.fit.R == pytest.approx(R, rel=0.03)
    assert cr.contact_radius == pytest.approx(R * math.sin(math.radians(theta_deg)), rel=0.08)
    # the fitted centre is the droplet's, up to a whole period
    off = np.array([cr.fit.xc, cr.fit.yc]) - centre[:2]
    assert np.abs(off - L * np.round(off / L)).max() < 0.05 * R


def test_slice_circle_across_the_periodic_wall():
    from mdfoam_droplet_viewer.export import slice_circle
    L = 10 * NM
    per = np.array([L, L, 0.0])
    lo = np.array([-5 * NM, -5 * NM, -1 * NM])
    c = {"xc": 4.8 * NM, "yc": 0.0, "zc": 0.0, "R": 2 * NM}
    # an x section at -4.8 nm is 0.4 nm from the centre through the wall (not 9.6 nm)
    cen, r = slice_circle(c, -4.8 * NM, 0, lo, per)
    assert r == pytest.approx(math.sqrt(4 - 0.16) * NM)
    assert cen[0] == pytest.approx(4.8 * NM)
    # an unwrapped centre (outside the box) is brought back into it
    cen, r = slice_circle({**c, "xc": 5.2 * NM}, 4.9 * NM, 0, lo, per)
    assert cen[0] == pytest.approx(-4.8 * NM) and r == pytest.approx(math.sqrt(4 - 0.09) * NM)
    assert slice_circle(c, 0.0, 0, lo, per) is None              # 4.8 nm away: misses the sphere
    assert slice_circle(c, 3 * NM, 2, lo, per) is None           # z is not periodic: above the sphere


def test_contact_angle_missing_reasons():
    assert contact_angle(np.zeros((0, 3)), 0.5, 1).reason == "no iso-density points"
    flat = np.column_stack([np.arange(10.0), np.zeros(10), np.zeros(10)]) * NM
    assert contact_angle(flat, 0.5, 1).reason == "droplet height is zero"


# ---------------------------------------------------------------- evaporation
def rows_from(vols, status=None):
    return [{"time": (i + 1) * 1e-10, "volume": v, "status": (status or {}).get(i, "ok")} for i, v in enumerate(vols)]


def test_evaporation_first_time_of_zero_run():
    rows = rows_from([5, 0, 0, 3, 0, 0, 0, 0])
    assert evaporation_time(rows, 0.0, 3) == pytest.approx(5e-10)
    assert math.isnan(evaporation_time(rows_from([5, 0, 0]), 0.0, 3))      # not completed
    assert evaporation_time(rows_from([5, 1e-30, 0, 0]), 1e-29, 3) == pytest.approx(2e-10)  # tolerance


def test_evaporation_run_broken_by_skipped_row():
    rows = rows_from([5, 0, 0, 0, 0, 0, 0], status={2: "skipped"})
    rows[2].pop("volume")
    assert evaporation_time(rows, 0.0, 3) == pytest.approx(4e-10)


def test_summary_mean_angle_fraction_and_missing():
    rows = [{"time": i, "status": "ok", "volume": 1.0, "r_eq": 1.0, "contact_angle": a, "contact_radius": 1.0}
            for i, a in enumerate([math.nan, 80.0, 70.0, 10.0, math.nan])]
    s = Settings(mean_angle_percent=50.0)   # ceil(5*0.5)=3 rows: nan, 80, 70 -> 75
    out = summarize(rows, s)
    assert out["mean_angle_rows"] == 3
    assert out["mean_angle"] == pytest.approx(75.0)
    assert math.isnan(out["initial_angle"])          # first row missing stays missing
    assert out["final_angle"] == pytest.approx(10.0)  # last valid from the end


# ---------------------------------------------------------------- phases, temperature, pressure
def build_case(tmp_path, rho, rhoN=None, T=None, zones=None, time="1e-10", procs=0):
    xs, ys, zs = make_grid(n=4, nz=4)
    pts, faces, own, nei, patches = grid_mesh(xs, ys, zs)
    write_mesh(tmp_path, pts, faces, own, nei, patches, zones)
    (tmp_path / "constant" / "moleculeProperties").write_text(MOLPROPS)
    flds = {"rhoM_water": (rho, "[1 -3 0 0 0 0 0]")}
    if rhoN is not None:
        flds["rhoN_water"] = (rhoN, "[0 -3 0 0 0 0 0]")
    if T is not None:
        flds["temperature_water"] = (T, "[0 0 0 1 0 0 0]")
    n = len(rho)
    if procs:
        parts = np.array_split(np.random.default_rng(0).permutation(n), procs)
        for p, a in enumerate(parts):
            pd = tmp_path / f"processor{p}"
            (pd / "constant" / "polyMesh").mkdir(parents=True)
            (pd / "constant" / "polyMesh" / "cellProcAddressing").write_text(
                foam_label_list(a))
            for name, (v, dims) in flds.items():
                write_field(pd / time / name, np.asarray(v)[a], dims)
    else:
        for name, (v, dims) in flds.items():
            write_field(tmp_path / time / name, v, dims)
    return cell_volumes(xs, ys, zs)


def foam_label_list(a):
    return "FoamFile\n{\n format ascii;\n class labelList;\n}\n" + f"{len(a)}\n(\n" + "\n".join(map(str, a)) + "\n)\n"


def test_phase_margins_and_boundaries(tmp_path):
    n = 64
    rho = np.full(n, 20.0)
    rho[0] = 50.0     # == gas_max -> gas
    rho[1] = 50.0001  # margin
    rho[2] = 499.9    # margin
    rho[3] = 500.0    # == liquid_min -> liquid
    rho[4] = 0.0      # == gas_min -> gas (empty cell still counts in volume)
    vol = build_case(tmp_path, rho)
    c = CaseData.open("c", tmp_path, tmp_path)
    s = Settings(region_zones=[], temperature_enabled=False, pressure_enabled=True, number_density_field="")
    m = c.load_mesh()
    row = analyze_time(c, "1e-10", s, build_context(c, m, s))
    gas = np.ones(n, bool); gas[[1, 2, 3]] = False
    assert row["n_margin_cells"] == 2
    assert row["gas_n_cells"] == int(gas.sum())
    assert row["gas_volume"] == pytest.approx(vol[gas].sum())
    assert row["region_liquid_volume"] == pytest.approx(vol[3])


def test_temperature_number_weighted_and_pressure(tmp_path):
    n = 64
    rho = np.full(n, 10.0); rho[:8] = 900.0
    rhoN = np.full(n, 2e26); rhoN[:8] = 3e28; rhoN[8] = 0.0
    T = np.full(n, 350.0); T[:4] = 300.0; T[4:8] = 400.0; T[8] = 0.0
    vol = build_case(tmp_path, rho, rhoN, T)
    c = CaseData.open("c", tmp_path, tmp_path)
    s = Settings(region_zones=[])
    m = c.load_mesh()
    row = analyze_time(c, "1e-10", s, build_context(c, m, s))
    Nl = rhoN[:8] * vol[:8]
    assert row["temp_mean"] == pytest.approx((Nl * T[:8]).sum() / Nl.sum(), rel=1e-12)
    g = slice(8, None)
    Ng = rhoN[g] * vol[g]
    assert row["gas_N"] == pytest.approx(Ng.sum(), rel=1e-12)
    assert row["gas_volume"] == pytest.approx(vol[g].sum(), rel=1e-12)
    assert row["gas_p"] == pytest.approx(K_B * (Ng * T[g]).sum() / vol[g].sum(), rel=1e-12)
    # uniform n, T over occupied gas cells -> p = N k T / V with V including the empty cell
    assert row["gas_T"] == pytest.approx(350.0)


def test_pressure_zero_molecules_vs_no_cells(tmp_path):
    rho = np.full(64, 0.0); rho[:4] = 900
    build_case(tmp_path, rho, np.where(rho > 0, 3e28, 0.0), np.where(rho > 0, 300.0, 0.0))
    c = CaseData.open("c", tmp_path, tmp_path)
    m = c.load_mesh()
    s = Settings(region_zones=[])
    row = analyze_time(c, "1e-10", s, build_context(c, m, s))
    assert row["gas_p"] == 0.0 and row["gas_volume"] > 0 and "N=0" in row["gas_reason"]
    s2 = Settings(region_zones=[], gas_min=1.0, gas_max=2.0)
    row2 = analyze_time(c, "1e-10", s2, build_context(c, m, s2))
    assert math.isnan(row2["gas_p"]) and "V=0" in row2["gas_reason"]


def test_missing_temperature_does_not_stop_droplet(tmp_path):
    rho = np.full(64, 0.0); rho[:10] = 900
    build_case(tmp_path, rho)
    c = CaseData.open("c", tmp_path, tmp_path)
    m = c.load_mesh()
    s = Settings(region_zones=[])
    row = analyze_time(c, "1e-10", s, build_context(c, m, s))
    assert row["status"] == "ok" and row["volume"] > 0
    assert math.isnan(row["temp_mean"]) and "temperature unavailable" in row["temp_reason"]


def test_phase_codes_and_slice_grid():
    from mdfoam_droplet_viewer.viz import (PHASE_BELOW_GAS, PHASE_GAS, PHASE_LIQUID, PHASE_MARGIN, PHASE_OUTSIDE,
                                   phase_codes, slice_grid)
    s = Settings(gas_min=1.0, gas_max=50.0, liquid_min=500.0)
    rho = np.array([0.0, 1.0, 50.0, 50.1, 499.9, 500.0, 900.0, 0.0])
    region = np.array([True] * 7 + [False])  # last cell: e.g. the solid substrate zone
    code = phase_codes(rho, s, region, None)
    assert code.tolist() == [PHASE_BELOW_GAS, PHASE_GAS, PHASE_GAS, PHASE_MARGIN, PHASE_MARGIN,
                             PHASE_LIQUID, PHASE_LIQUID, PHASE_OUTSIDE]
    # slice: 2x2x2 grid; pick the upper z layer (xy plane)
    cc = [[x, y, z] for z in (0.5, 1.5) for y in (0.5, 1.5) for x in (0.5, 1.5)]
    grid = {"cc": cc, "rho": list(range(8)), "phase": [0, 1, 2, 3, 4, 4, 3, 2]}
    val, xs, ys, ph, rh = slice_grid(grid, "xy", 1.4)
    assert val == 1.5 and xs.tolist() == [0.5, 1.5] and ys.tolist() == [0.5, 1.5]
    assert ph.tolist() == [[4, 4], [3, 2]] and rh.tolist() == [[4, 5], [6, 7]]


def test_zone_filter_requires_explicit_selection(tmp_path):
    rho = np.full(64, 0.0)
    zones = {"solid1": np.arange(16), "zone1": np.arange(16, 64)}
    vol = build_case(tmp_path, rho, zones=zones)
    c = CaseData.open("c", tmp_path, tmp_path)
    m = c.load_mesh()
    s = Settings(temperature_enabled=False)  # region_zones None -> auto
    ctx = build_context(c, m, s)
    assert ctx.region_zones == []
    row = analyze_time(c, "1e-10", s, ctx)
    assert row["gas_volume"] == pytest.approx(vol[16:].sum())


# ---------------------------------------------------------------- processor data
def test_processor_split_equals_reconstructed(tmp_path):
    rng = np.random.default_rng(3)
    rho = rng.uniform(0, 1000, 64)
    rhoN = rho / 2.99e-26
    T = rng.uniform(250, 400, 64)
    a = tmp_path / "rec"; b = tmp_path / "dec"
    build_case(a, rho, rhoN, T)
    build_case(b, rho, rhoN, T, procs=3)
    s = Settings(region_zones=[])
    ra = analyze_time(ca := CaseData.open("a", a, a), "1e-10", s, build_context(ca, ca.load_mesh(), s))
    rb = analyze_time(cb := CaseData.open("b", b, b), "1e-10", s, build_context(cb, cb.load_mesh(), s))
    assert cb.time_entry("1e-10").source == "processor"
    for k in ("volume", "temp_mean", "gas_p", "contact_angle"):
        if isinstance(ra[k], float) and math.isnan(ra[k]):
            assert math.isnan(rb[k])
        else:
            assert rb[k] == pytest.approx(ra[k], rel=1e-12)


def test_reconstructed_preferred_over_processor(tmp_path):
    rho = np.full(64, 900.0)
    build_case(tmp_path, rho, procs=2)
    write_field(tmp_path / "1e-10" / "rhoM_water", rho)
    c = CaseData.open("c", tmp_path, tmp_path)
    assert [t.source for t in c.times] == ["reconstructed"]


# ---------------------------------------------------------------- parser
def test_parser_uniform_and_mismatch(tmp_path):
    write_field(tmp_path / "u", None, uniform=3.5)
    assert foam.read_scalar_field(tmp_path / "u", 5).values.tolist() == [3.5] * 5
    write_field(tmp_path / "f", [1.0, 2.0, 3.0])
    with pytest.raises(foam.FoamFormatError, match="mesh has 4 cells"):
        foam.read_scalar_field(tmp_path / "f", 4)
    (tmp_path / "bad").write_text("FoamFile\n{\n class volScalarField;\n}\ninternalField nonuniform List<scalar> 3\n(\n1\n2\n)\n;")
    with pytest.raises(foam.FoamFormatError, match="declared 3"):
        foam.read_scalar_field(tmp_path / "bad")
    (tmp_path / "compact").write_text("FoamFile\n{\n class labelList;\n}\n4{7}\n")
    assert foam.read_list(tmp_path / "compact", "label").tolist() == [7, 7, 7, 7]


def test_discover_parent_and_case_root(tmp_path):
    for name in ("run002", "run010", "run001"):
        build_case(tmp_path / name / "main", np.zeros(64))
        (tmp_path / name / "initial").mkdir()
    (tmp_path / "run003" / "main").mkdir(parents=True)  # not started
    found = discover_cases(tmp_path)
    assert [f[0] for f in found] == ["run001", "run002", "run003", "run010"]
    assert found[2][2] is None
    one = discover_cases(tmp_path / "run001")
    assert len(one) == 1 and one[0][2].name == "main"


def test_discover_one_level_deeper(tmp_path):
    # e.g. distance_recheck_100runs/1.0A/run001_x001 scanned from distance_recheck_100runs
    for name in ("run001", "run002"):
        build_case(tmp_path / "1.0A" / name / "main", np.zeros(64))
    (tmp_path / "old_logs").mkdir()
    found = discover_cases(tmp_path)
    assert [f[0] for f in found] == ["1.0A/run001", "1.0A/run002"]


# ---------------------------------------------------------------- settings / csv
def test_settings_roundtrip_and_validation():
    s = Settings(liquid_min=400, gas_max=40, region_zones=["zone1"], temp_average="volume")
    from mdfoam_droplet_viewer.settings import Settings as GenericSettings
    s2 = GenericSettings.from_dict(s.to_dict())
    with pytest.raises(ValueError, match="unknown settings"):
        GenericSettings.from_dict(s.to_dict() | {"unknown_key": 1})
    assert s2 == s and s2.hash() == s.hash()
    assert Settings(gas_max=600).validate()
    assert Settings(liquid_min=float("nan")).validate()
    assert not Settings().validate()


def test_summary_time_sd_is_sample_sd_over_the_mean_rows():
    rows = [{"time": i, "status": "ok", "volume": 1.0, "r_eq": 1.0, "contact_angle": a, "contact_radius": 1.0,
             "temp_mean": T, "gas_p": 1000.0}
            for i, (a, T) in enumerate([(70.0, 300.0), (80.0, 310.0), (math.nan, 320.0), (10.0, 330.0)])]
    out = summarize(rows, Settings(mean_angle_percent=75.0))  # first 3 rows -> angles 70, 80 (nan dropped)
    assert out["mean_angle"] == pytest.approx(75.0)
    assert out["mean_angle_sd"] == pytest.approx(np.std([70.0, 80.0], ddof=1))
    assert out["mean_temp_sd"] == pytest.approx(np.std([300, 310, 320, 330], ddof=1))
    assert out["mean_gas_p_sd"] == pytest.approx(0.0)
    one = summarize(rows[:1], Settings())
    assert math.isnan(one["mean_angle_sd"])  # a single value has no SD


def test_describe_and_statistics_csv():
    from mdfoam_droplet_viewer.export import describe
    d = describe([1.0, 2.0, 3.0, 4.0, None, float("nan")])
    assert d["n"] == 4 and d["mean"] == pytest.approx(2.5) and d["median"] == pytest.approx(2.5)
    assert d["sd"] == pytest.approx(np.std([1, 2, 3, 4], ddof=1)) and d["se"] == pytest.approx(d["sd"] / 2)
    assert describe([5.0])["sd"] is None
    res = {n: {"name": n, "status": "done", "rows": [{"time": 1e-10, "status": "ok", "volume": 1e-27}],
               "summary": {"evaporation_time": et, "mean_angle": a}, "meta": {}, "errors": [], "skipped": []}
           for n, et, a in (("a", 9e-9, 80.0), ("b", 10e-9, 70.0), ("c", math.nan, 75.0))}
    z = zipfile.ZipFile(io.BytesIO(build_csv_zip(res, ["a", "b", "c"], {"time": "ns"}, {})))
    lines = z.read("statistics.csv").decode("utf-8-sig").splitlines()
    hdr = lines[0].split(",")
    et = dict(zip(hdr, next(l for l in lines if l.startswith("evaporation_time")).split(",")))
    assert et["unit"] == "ns" and et["n_cases"] == "2"  # the not-evaporated case is excluded, not counted as 0
    assert float(et["mean"]) == pytest.approx(9.5) and float(et["sd"]) == pytest.approx(np.std([9, 10], ddof=1))
    ang = dict(zip(hdr, next(l for l in lines if l.startswith("mean_angle,")).split(",")))
    assert ang["n_cases"] == "3" and float(ang["sd"]) == pytest.approx(5.0)


def test_csv_missing_is_blank_not_zero():
    res = {"a": {"name": "a", "status": "done", "rows": [
        {"time": 1e-10, "time_name": "1e-10", "status": "ok", "volume": 0.0, "contact_angle": math.nan}],
        "summary": {"evaporation_time": math.nan, "final_volume": 0.0}, "meta": {}, "errors": [], "skipped": []}}
    z = zipfile.ZipFile(io.BytesIO(build_csv_zip(res, ["a"], {"volume": "nm³"}, {"x": 1})))
    ts = z.read("timeseries.csv").decode("utf-8-sig").splitlines()
    hdr = ts[0].split(",")
    vals = ts[1].split(",")
    assert vals[hdr.index("volume [nm³]")] == "0"
    assert vals[hdr.index("contact_angle [deg]")] == ""
    assert "run_meta.json" in z.namelist()


# ---------------------------------------------------------------- theory


def test_select_times_range_stride_newest():
    from types import SimpleNamespace
    from mdfoam_droplet_viewer.settings import water_settings as Settings, select_times
    v = [0.0, 1e-10, 2e-10, 3e-10, 4e-10, 5e-10]
    assert select_times(v) == set(range(6))
    assert select_times(v, 2) == {0, 2, 4, 5}                       # newest always kept
    assert select_times(v, 1, 2e-10 * (1 + 1e-12), 4e-10) == {2, 3, 4}  # bounds tolerate round-off
    assert select_times(v, 10, 1e-10) == {1, 5}
    assert select_times(v, 1, 9e-10) == set()
    items = [SimpleNamespace(value=x) for x in v]
    assert [x.value for x in Settings(time_stride=3).pick_times(items)] == [0.0, 3e-10, 5e-10]
    assert Settings(time_stride=0).validate() and Settings(time_min=2.0, time_max=1.0).validate()


def test_gas_by_distance_and_temperature_source(tmp_path):
    from mdfoam_droplet_viewer.analysis import analyze_time, build_context, gas_cells
    from mdfoam_droplet_viewer.case import CaseData
    from mdfoam_droplet_viewer.settings import water_settings as Settings
    # 4 x 4 x 4 cells of 3 nm (x, y periodic, 12 nm) x 2.25 nm (z from -1 nm); one liquid cell in a corner
    n_cells = 64
    case_dir = tmp_path / "c"
    build_case(case_dir, np.zeros(n_cells))
    c0 = CaseData.open("c", case_dir, case_dir)
    cc = c0.load_mesh().cell_centres / NM
    corner = int(np.argmin(np.abs(cc[:, 0] + 4.5) + np.abs(cc[:, 1] + 4.5) + np.abs(cc[:, 2] - 0.125)))
    rho = np.full(n_cells, 1.0)
    rho[corner] = 1000.0
    T = np.full(n_cells, 100.0)
    T[corner] = 350.0
    build_case(case_dir, rho, rhoN=rho / 2.99e-26, T=T)
    c = CaseData.open("c", case_dir, case_dir)
    m = c.load_mesh()
    s = Settings(gas_mode="distance", gas_min_distance=4e-9, gas_min_height=1e-9, region_zones=[])
    gas, margin = gas_cells(rho, s, build_context(c, m, s))
    across = int(np.argmin(np.abs(cc[:, 0] - 4.5) + np.abs(cc[:, 1] + 4.5) + np.abs(cc[:, 2] - 0.125)))
    far = int(np.argmin(np.abs(cc[:, 0] - 1.5) + np.abs(cc[:, 1] - 1.5) + np.abs(cc[:, 2] - 6.875)))
    assert not gas[corner] and not margin[corner]           # liquid
    assert margin[across] and not gas[across]               # 3 nm away through the periodic x boundary
    assert gas[far]
    s2 = Settings(gas_mode="distance", gas_min_distance=0, gas_min_height=2e-9, region_zones=[])
    g2, _ = gas_cells(rho, s2, build_context(c, m, s2))
    assert not g2[np.abs(cc[:, 2] - 0.125) < 1e-6].any() and g2[np.abs(cc[:, 2] - 2.375) < 1e-6].all()  # wall layer out
    # pressure uses the chosen temperature: cells (100 K) or the droplet (350 K)
    row_cell = analyze_time(c, "1e-10", Settings(gas_max=10, region_zones=[]), build_context(c, m, Settings(region_zones=[])))
    s3 = Settings(gas_max=10, gas_temp="liquid", region_zones=[])
    row_liq = analyze_time(c, "1e-10", s3, build_context(c, m, s3))
    assert row_cell["gas_T"] == pytest.approx(100.0) and row_liq["gas_T"] == pytest.approx(350.0)
    assert row_liq["gas_p"] == pytest.approx(3.5 * row_cell["gas_p"])
    s4 = Settings(gas_max=10, gas_temp="fixed", gas_temp_fixed=300.0, region_zones=[])
    assert analyze_time(c, "1e-10", s4, build_context(c, m, s4))["gas_T"] == 300.0
    assert Settings(gas_temp="fixed", gas_temp_fixed=0).validate() and Settings(gas_mode="x").validate()
