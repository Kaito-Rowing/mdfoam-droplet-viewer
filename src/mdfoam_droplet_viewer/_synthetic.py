"""Write small synthetic OpenFOAM cases with known geometry for tests."""

from __future__ import annotations

from pathlib import Path

import numpy as np

HDR = """FoamFile
{{
    version     2.0;
    format      ascii;
    class       {cls};
    object      {obj};
}}
"""


def grid_mesh(xs, ys, zs, periodic_xy=True):
    """Hex grid -> (points, faces, owner, neighbour, patches). Cell id c = i + nx*(j + ny*k)."""
    xs, ys, zs = map(np.asarray, (xs, ys, zs))
    nx, ny, nz = len(xs) - 1, len(ys) - 1, len(zs) - 1
    P = lambda i, j, k: i + (nx + 1) * (j + (ny + 1) * k)
    C = lambda i, j, k: i + nx * (j + ny * k)
    pts = np.array([[xs[i], ys[j], zs[k]] for k in range(nz + 1) for j in range(ny + 1) for i in range(nx + 1)])
    faces, own, nei = [], [], []
    for k in range(nz):
        for j in range(ny):
            for i in range(1, nx):
                faces.append([P(i, j, k), P(i, j + 1, k), P(i, j + 1, k + 1), P(i, j, k + 1)]); own.append(C(i - 1, j, k)); nei.append(C(i, j, k))
    for k in range(nz):
        for j in range(1, ny):
            for i in range(nx):
                faces.append([P(i, j, k), P(i, j, k + 1), P(i + 1, j, k + 1), P(i + 1, j, k)]); own.append(C(i, j - 1, k)); nei.append(C(i, j, k))
    for k in range(1, nz):
        for j in range(ny):
            for i in range(nx):
                faces.append([P(i, j, k), P(i + 1, j, k), P(i + 1, j + 1, k), P(i, j + 1, k)]); own.append(C(i, j, k - 1)); nei.append(C(i, j, k))
    # sort internal faces by (owner, neighbour) like OpenFOAM (not required by the reader, but realistic)
    order = np.lexsort((np.array(nei), np.array(own)))
    faces = [faces[o] for o in order]; own = [own[o] for o in order]; nei = [nei[o] for o in order]
    patches = []

    def add(name, flist, olist, extra):
        patches.append({"name": name, "nFaces": len(flist), "startFace": len(faces), **extra})
        faces.extend(flist); own.extend(olist)

    xp = ([P(nx, j, k), P(nx, j + 1, k), P(nx, j + 1, k + 1), P(nx, j, k + 1)] for k in range(nz) for j in range(ny))
    xm = ([P(0, j, k), P(0, j, k + 1), P(0, j + 1, k + 1), P(0, j + 1, k)] for k in range(nz) for j in range(ny))
    yp = ([P(i, ny, k), P(i, ny, k + 1), P(i + 1, ny, k + 1), P(i + 1, ny, k)] for k in range(nz) for i in range(nx))
    ym = ([P(i, 0, k), P(i + 1, 0, k), P(i + 1, 0, k + 1), P(i, 0, k + 1)] for k in range(nz) for i in range(nx))
    zp = ([P(i, j, nz), P(i + 1, j, nz), P(i + 1, j + 1, nz), P(i, j + 1, nz)] for j in range(ny) for i in range(nx))
    zm = ([P(i, j, 0), P(i, j + 1, 0), P(i + 1, j + 1, 0), P(i + 1, j, 0)] for j in range(ny) for i in range(nx))
    cyc = (lambda nb: {"type": "cyclic", "neighbourPatch": nb}) if periodic_xy else (lambda nb: {"type": "wall"})
    add("periodicX_half0", list(xp), [C(nx - 1, j, k) for k in range(nz) for j in range(ny)], cyc("periodicX_half1"))
    add("periodicX_half1", list(xm), [C(0, j, k) for k in range(nz) for j in range(ny)], cyc("periodicX_half0"))
    add("periodicY_half0", list(yp), [C(i, ny - 1, k) for k in range(nz) for i in range(nx)], cyc("periodicY_half1"))
    add("periodicY_half1", list(ym), [C(i, 0, k) for k in range(nz) for i in range(nx)], cyc("periodicY_half0"))
    add("top", list(zp), [C(i, j, nz - 1) for j in range(ny) for i in range(nx)], {"type": "wall"})
    add("bottom", list(zm), [C(i, j, 0) for j in range(ny) for i in range(nx)], {"type": "wall"})
    return pts, faces, np.array(own), np.array(nei), patches


def cell_centres(xs, ys, zs):
    xc = 0.5 * (np.asarray(xs)[1:] + np.asarray(xs)[:-1])
    yc = 0.5 * (np.asarray(ys)[1:] + np.asarray(ys)[:-1])
    zc = 0.5 * (np.asarray(zs)[1:] + np.asarray(zs)[:-1])
    Z, Y, X = np.meshgrid(zc, yc, xc, indexing="ij")
    return np.column_stack([X.ravel(), Y.ravel(), Z.ravel()])


def cell_volumes(xs, ys, zs):
    dx, dy, dz = np.diff(xs), np.diff(ys), np.diff(zs)
    Z, Y, X = np.meshgrid(dz, dy, dx, indexing="ij")
    return (X * Y * Z).ravel()


def write_mesh(d: Path, pts, faces, own, nei, patches, zones=None):
    pm = d / "constant" / "polyMesh"
    pm.mkdir(parents=True, exist_ok=True)
    (pm / "points").write_text(HDR.format(cls="vectorField", obj="points") + f"{len(pts)}\n(\n" +
                               "\n".join(f"({x:.12g} {y:.12g} {z:.12g})" for x, y, z in pts) + "\n)\n")
    (pm / "faces").write_text(HDR.format(cls="faceList", obj="faces") + f"{len(faces)}\n(\n" +
                              "\n".join(f"{len(f)}({' '.join(map(str, f))})" for f in faces) + "\n)\n")
    (pm / "owner").write_text(HDR.format(cls="labelList", obj="owner") + f"{len(own)}\n(\n" + "\n".join(map(str, own)) + "\n)\n")
    (pm / "neighbour").write_text(HDR.format(cls="labelList", obj="neighbour") + f"{len(nei)}\n(\n" + "\n".join(map(str, nei)) + "\n)\n")
    body = f"{len(patches)}\n(\n"
    for p in patches:
        body += f"    {p['name']}\n    {{\n        type {p['type']};\n        nFaces {p['nFaces']};\n        startFace {p['startFace']};\n"
        if "neighbourPatch" in p:
            body += f"        neighbourPatch {p['neighbourPatch']};\n"
        body += "    }\n"
    (pm / "boundary").write_text(HDR.format(cls="polyBoundaryMesh", obj="boundary") + body + ")\n")
    if zones:
        zb = f"{len(zones)}\n(\n"
        for name, cells in zones.items():
            zb += f"{name}\n{{\n    type cellZone;\n    cellLabels List<label> {len(cells)}\n(\n" + "\n".join(map(str, cells)) + "\n)\n;\n}\n"
        (pm / "cellZones").write_text(HDR.format(cls="regIOobject", obj="cellZones") + zb + ")\n")


def write_field(path: Path, values, dims="[1 -3 0 0 0 0 0]", uniform=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    if uniform is not None:
        internal = f"internalField   uniform {uniform};"
    else:
        internal = f"internalField   nonuniform List<scalar> \n{len(values)}\n(\n" + "\n".join(f"{v:.12g}" for v in values) + "\n)\n;"
    path.write_text(HDR.format(cls="volScalarField", obj=path.name) + f"dimensions      {dims};\n\n{internal}\n\nboundaryField\n{{\n}}\n")
