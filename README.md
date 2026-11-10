# mdFOAM Droplet Viewer

**Inspect, compare and model mdFOAM droplets — from a browser, a JSON CLI, or an AI assistant over MCP.**

A local Python application for batch analysis of molecular simulation results stored as ASCII OpenFOAM fields. Choose your species, fields, phase thresholds and substrate orientation. Add your own theoretical models as expressions or installed Python extensions.

[日本語ガイド](docs/README.ja.md) · [AI operation guide](docs/AI.md) · [Model extensions](docs/EXTENSIONS.md)

![Synthetic droplet comparison](docs/img/overview.png)

## Features

- Analyze multiple cases, including partially completed simulations. Cache unchanged timesteps and process new output incrementally.
- Calculate liquid volume, equivalent radius, spherical-fit contact angle/radius, temperature and an ideal-gas pressure estimate.
- Inspect density slices, 2D sphere fits, 3D cells and available molecule positions; export CSV, PNG and GIF.
- Read local reconstructed or decomposed cases; retrieve reconstructed data through SSH/SFTP with host-key verification.
- Configure arbitrary field/species/zone names, explicit phase thresholds, substrate normal (`±X`, `±Y`, `±Z`) and optional substrate position.
- Compare measured quantities with `y=f(x, parameters)`, including bounded least-squares fits and reproducible model snapshots.
- Use the same versioned operations from the GUI, JSON CLI and local stdio MCP server.

## Install and run

Python 3.10+ is required. Windows, Linux and macOS are tested in CI.

```sh
git clone https://github.com/Kaito-Rowing/mdfoam-droplet-viewer.git
cd mdfoam-droplet-viewer
python -m venv .venv
```

Activate the virtual environment:

```sh
# Windows PowerShell
.venv\Scripts\Activate.ps1
# macOS / Linux
source .venv/bin/activate
```

```sh
python -m pip install ".[mcp]"
mdfoam-droplet-viewer
```

On Windows, `run.bat` also creates an environment and opens the application. Without environment activation, use `.venv\Scripts\python -m mdfoam_droplet_viewer` (Windows) or `.venv/bin/python -m mdfoam_droplet_viewer` (Unix).

The application listens on **127.0.0.1:8765**. Use `--port 8800` to choose another port or `--no-browser` to disable automatic browser opening. This is a local application, not a hosted multiuser service.

## Try synthetic data

No simulation results or research screenshots are bundled. Generate two small fictional droplets:

```sh
python -m mdfoam_droplet_viewer.demo demo-data
```

In the browser, scan the full path to `demo-data`. Expand **All settings (JSON, SI)**, paste the contents of `demo-data/settings.json`, then click **Apply JSON** and **Analyze**. Select a case to inspect its fields and visualize individual timesteps. A single field candidate can be suggested automatically; multiple candidates require a selection.

For your data, choose a density field and set liquid/gas density thresholds in that field's units. Select temperature and number-density fields as available. Molecule mass comes from `constant/moleculeProperties` or an explicit input in kg; there is no implicit water fallback. The optional water preset is an illustrative starting point, and its thresholds should be checked for your model and temperature.

No zone is automatically excluded based on its name. Select gas/temperature and droplet zones explicitly when the mesh contains substrate cells.

## AI & automation

Start with capability discovery:

```sh
mdfoam-droplet schema
mdfoam-droplet analyze demo-data --settings demo-data/settings.json --save demo --workers 1
mdfoam-droplet call register_model --input demo-data/register.json
mdfoam-droplet call fit_model --input demo-data/fit.json --saved demo
```

CLI responses include `api_version`, `ok`, and either `result` or `error.code` / `error.reason`. Progress goes to stderr; stdout stays JSON. Exit code 0 means success, 2 means input/operation failure, and 130 means interruption. A fit's scientific outcome is in `result.status` (`converged` or `failed`); callers must check it.

The complete runnable example generates cases, analyzes them, fits an example model and exports a figure:

```sh
python examples/ai_workflow.py workflow-output
```

For MCP-capable clients, configure a local stdio server using the **absolute path** to the installed environment's executable:

```json
{
  "mcpServers": {
    "mdfoam-droplet": {
      "command": "/absolute/path/to/.venv/bin/mdfoam-droplet-mcp",
      "args": ["--root", "/absolute/path/to/analysis-workspace"]
    }
  }
}
```

On Windows use `.venv\Scripts\mdfoam-droplet-mcp.exe`, with backslashes escaped in JSON. The server exposes typed tools for discovery, inspection, validation, analysis, progress/cancellation, saved results, model registration/evaluation/fitting, figure export and SSH access. It owns one dataset and one active analysis job per process. An AI assistant can complete the workflow without opening the GUI.

See [the AI guide](docs/AI.md) for job handling, request schemas and reproducible workflows. `mdfoam-droplet call schema` describes every request. The browser calls these same operations under `/api/v1/{operation}`; `/docs` documents the local HTTP endpoints.

## Bring your own model

Expressions accept `x`, declared parameters, `pi`, `e`, `+ - * / **`, parentheses and `exp`, `log`, `log10`, `sqrt`, `sin`, `cos`, `tan`, `tanh`, `abs`, `minimum`, `maximum`. Expressions are parsed and interpreted; they are not evaluated as Python code.

Models declare x/y quantity names, unit labels, and parameter initial values and optional bounds. Analysis/model values use SI (angles in degrees and normalized quantities dimensionless). Unit labels do not perform automatic conversions. Fit a selected case or supply explicit arrays and an optional x interval. Non-finite measured points are excluded; domain errors, insufficient points and non-convergence are reported with reasons.

For numerical models, install a Python extension using the documented `definition` and `predict(x, parameters, context)` contract. A template is included in `examples/model-plugin`. Python extensions run as normal local Python code and should be installed deliberately. The HTTP and MCP interfaces accept expression definitions, not Python source.

The application stores the complete definition and fitted curves with snapshots, so saved fits can be displayed/exported without the original plugin. Re-evaluating or refitting a Python model requires the plugin to be installed. See [extensions](docs/EXTENSIONS.md).

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

## Development

```sh
python -m pip install -e ".[dev]"
python -m pytest -q
python -m build
```

Tests include deterministic geometry, nonuniform volumes, decomposed fields, arbitrary species/zones, all six substrate directions, model domain/fit failures, CLI/HTTP agreement, an actual stdio MCP client workflow, and SSH/SFTP against a loopback test server. CI runs tests and verifies wheel installation on Windows, Linux and macOS.

Contributions should keep numerical behavior in the shared service and computational modules. UI/CLI/MCP adapters should not reimplement formulas. Include a synthetic example and a behavioral test for new capabilities.

## License

MIT © 2026 Kaito-Rowing. See [LICENSE](LICENSE) and [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md). OpenFOAM is an external data format; no OpenFOAM source or executable is bundled.
