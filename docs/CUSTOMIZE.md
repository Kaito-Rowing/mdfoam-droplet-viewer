# Make it yours, with AI

[← Back to the project](../README.md)

This project is a starting point for your research workflow. Bring a coding assistant, fork the repository, and build the measurements, models, plots, or interface you want to use. Small additions and substantial adaptations are both welcome.

You can start with the existing expression/Python model interfaces, then modify the application when your workflow needs more. The MIT license allows you to adapt and share the code under its terms.

## Get running together

Use a coding assistant that can read/edit the checkout and run commands on your computer. Give it the repository URL or open your fork in its workspace, then paste:

```text
Help me set up mdFOAM Droplet Viewer in this workspace.
Read README.md and docs/AI.md. Check Python, create an isolated virtual
environment, and install the project with MCP support. Run:
    python examples/ai_workflow.py workflow-output
Confirm that it generates synthetic cases, completes analysis and fitting,
and writes fit.png. Tell me where the results are and how to start the GUI.
```

The runnable example uses a new output directory. If that directory already contains an earlier run, choose a fresh directory for the next run.

## Describe the research outcome

Tell the assistant what you want to learn, what your inputs look like, and what result you want to inspect. Include a small example when possible. For a new scientific calculation, provide the definition or reference, units, and expected behavior.

Useful starting requests:

- “Add my theoretical model and fit these parameters to equivalent radius over time.”
- “Add a measurement from these fields and export it alongside the existing quantities.”
- “Make a plot comparing this quantity across these cases, with these labels and units.”
- “Adapt the interface to our lab's repeated analysis workflow.”

### Add a model

```text
Read docs/EXTENSIONS.md. Help me add this model: [formula or algorithm].
The input quantity is [name and unit], the output is [name and unit],
and the parameters are [names, initial values, and bounds]. Use an expression
if the existing math functions are enough; otherwise follow the installed
Python model template. Verify it against a small example with known values,
then show me how to evaluate, fit, and export it.
```

### Change the application

```text
Read docs/CUSTOMIZE.md and inspect the relevant code before editing.
I want [describe the measurement, plot, input format, or interface change].
Use these sample inputs and expected results: [details]. Identify any missing
scientific definitions or units with me. Implement the change in my fork,
keep computation shared across the applicable GUI/CLI/MCP operations,
and add a meaningful synthetic example or regression test. Run the relevant
checks and document the new behavior and its assumptions.
```

## Where to make changes

Paths below are relative to the checkout. Application modules live under `src/mdfoam_droplet_viewer/`.

| What you want to change | Starting point |
| --- | --- |
| Formula or numerical model | [Extension contract](EXTENSIONS.md), [`examples/model-plugin/`](../examples/model-plugin), `models.py` |
| Per-timestep measurements or case summaries | `analysis.py`; add configuration in `settings.py` and quantity metadata/export support in `export.py` as needed |
| Contact geometry or substrate coordinates | `droplet.py`; inspect both analysis and visualization consumers |
| Field parsing or case discovery | `foam.py`, `case.py`; use a synthetic input fixture to check behavior |
| Browser controls or plots | `static/public.html`, `static/public.js`, `static/public.css` |
| A capability shared with AI clients | `service.py`; adapters live in `cli.py`, `mcp_server.py`, and `server.py` |
| Declarative figure output | `figures.py`, `export.py` |
| Remote discovery or data transfer | `remote.py`; check the fake-SFTP and loopback-SSH tests |

For a new result quantity, follow it from computation through result rows, quantity labels/units, exports, and whichever interface should expose it. Changing one graph alone may not add the underlying quantity to the other interfaces.

## Verify the change together

Ask the assistant to show the result, explain the assumptions, and report the checks it actually ran.

- For numerical work, compare with a known result using synthetic data. Include relevant units, missing inputs, or domain limits.
- For shared capabilities, check the applicable GUI, CLI, and MCP paths against the same inputs.
- For interface/documentation changes, inspect the rendered result and relevant links.
- Keep source simulation cases unchanged. Generate examples and outputs in separate directories.

For development:

```sh
python -m pip install -e ".[dev]"
python -m pytest -q
```

Run the tests relevant to the change while iterating; run the suite before sharing a change to the analysis engine. Add a usage example and update the documentation so the next person—or their assistant—can use what you built.

## Share what you build

Keep a specialized workflow in your fork, or open an issue/pull request for improvements that could help other users. Include a synthetic example, the expected behavior, and the checks you ran. Research-specific models can remain independent Python extensions.
