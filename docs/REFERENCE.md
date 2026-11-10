# Data, methods & reference

[← Back to the project](../README.md)

## Input, methods and limits

Cases can contain `constant/polyMesh` and numeric timestep folders directly or under `main/`; a different subfolder can be specified. Metadata files `case_info.txt` and `batch_status.txt` are optional. Local `processor*/` data is assembled using processor addressing. If reconstructed data exists at a time, it takes precedence. SSH retrieves reconstructed data only; reconstruct on the remote machine first.

Only ASCII OpenFOAM fields are supported; binary input is rejected. Volume is the sum of cells above the chosen density threshold. Equivalent radius is the radius of a sphere of that volume. Contact angle uses an isodensity surface and a sphere fitted over the selected relative height range. The normal points away from the substrate; a manual plane position uses the original world-axis coordinate in metres. Visualization coordinates use the two tangential axes followed by the oriented normal, with the basis reported in frame metadata. `z_base` / `sphere_zc` retain their historical names and refer to this oriented normal coordinate.

Gas pressure uses the ideal-gas approximation, not a saturation-pressure measurement. Temperature/molecule-count inputs may be unavailable; affected quantities have missing values and reasons. Results may depend substantially on phase thresholds and the fit range. The method sums all selected liquid cells; it does not independently track multiple droplets. Curved substrates and arbitrary oblique normals are not supported in this release. No Kelvin, fixed evaporation-theory, water-property correlations or molecular-departure analysis is bundled.

## Storage and exports

Application data lives in `~/.mdfoam_droplet_viewer` (`MDFOAM_DROPLET_CACHE` overrides). CLI `--root` and MCP `--root` choose a separate workspace. The private predecessor's saved format is not imported. Snapshots contain source paths and analysis settings; inspect them before sharing.

Generic figure specs support `timeseries`, `overlay`, `compare`, `section`, and arbitrary `xy` series. Figure-spec display units default to ns/nm/nm³/kPa, while direct model requests use SI. Override figure `units` explicitly when needed. Render or inspect a spec with:

```sh
mdfoam-droplet fig presets
mdfoam-droplet fig render figure.json -o figure.png
mdfoam-droplet fig check figure.json --strict
```

SSH uses your existing `~/.ssh/config`, keys and known_hosts. Register a new host with your regular SSH client before connecting. Passwords/passphrases are held in memory for the connection and are not saved in snapshots. SSH tar streaming falls back to SFTP.

