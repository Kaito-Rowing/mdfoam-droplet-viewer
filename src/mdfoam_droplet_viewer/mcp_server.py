"""Local stdio MCP adapter. stdout is reserved for MCP protocol messages."""
from __future__ import annotations

import argparse
import inspect
import sys
from pathlib import Path

from .service import REQUESTS, operate


def create_server():
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:
        raise RuntimeError('Install MCP support with: pip install ".[mcp]"') from exc
    server = FastMCP("mdFOAM Droplet Viewer", instructions=(
        "Start with schema, discover and inspect. Select fields and explicit density thresholds; "
        "do not assume water. analyze returns a process-scoped job_id; poll job_status until terminal. "
        "Model inputs use SI units. Only explicitly installed Python model extensions execute Python code."
    ))
    descriptions = {
        "schema": "Get all operation/settings/model schemas, quantities and explicit presets.",
        "discover": "Load local cases from a case folder or parent folder; replaces the active dataset.",
        "inspect": "Inspect a case's mesh, fields, species, zones and candidate settings.",
        "validate_settings": "Validate explicit analysis settings without running analysis.",
        "analyze": "Start background analysis of the active dataset and return a job id.",
        "job_status": "Get progress, logs and terminal state of the active job.",
        "cancel": "Request cancellation of the active job; poll until stopped.",
        "results": "Get analysis rows and stored model fits for the active dataset.",
        "list_models": "List registered expressions and explicitly installed Python model extensions.",
        "register_model": "Persist a validated expression model; never accepts Python source code.",
        "evaluate_model": "Evaluate a registered model on finite x values in SI units.",
        "fit_model": "Fit a model to supplied x/y data or an analyzed case; store the fit for export.",
        "save": "Save active analysis and model fits under a name; overwrite requires true.",
        "open": "Open a previously saved analysis and model fits.",
        "list_saved": "List named saved analyses.",
        "compare": "Load up to eight saved result sets for cross-case comparison.",
        "export_figure": "Render a declarative generic figure spec to a local output file.",
        "export_model_figure": "Render observed data and a stored converged model fit to PNG.",
        "remote_hosts": "List local SSH config aliases and available keys.",
        "remote_connect": "Connect to an SSH host using known_hosts verification; credentials are not saved.",
        "remote_disconnect": "Disconnect the active SSH session.",
        "remote_list": "List a folder on the connected SSH host.",
        "remote_discover": "Discover cases on the connected SSH host; analyze downloads selected fields.",
    }

    def make_tool(name, request_type):
        def tool(**kwargs):
            return operate(name, kwargs)
        tool.__name__ = name
        tool.__doc__ = descriptions[name]
        # Expose the same typed fields as the service schema rather than a free-form JSON argument.
        parameters = []
        annotations = {"return": dict}
        for field_name, field in request_type.model_fields.items():
            default = inspect.Parameter.empty if field.is_required() else field.get_default(call_default_factory=True)
            parameters.append(inspect.Parameter(field_name, inspect.Parameter.KEYWORD_ONLY,
                                               default=default, annotation=field.annotation))
            annotations[field_name] = field.annotation
        tool.__signature__ = inspect.Signature(parameters, return_annotation=dict)
        tool.__annotations__ = annotations
        return tool

    for name, request_type in REQUESTS.items():
        server.tool(name=name)(make_tool(name, request_type))
    return server


def main():
    parser = argparse.ArgumentParser(description="mdFOAM Droplet Viewer local stdio MCP server")
    parser.add_argument("--root", type=Path, help="isolated cache / saved results / models root")
    args = parser.parse_args()
    from . import server
    if args.root:
        server.CACHE_ROOT = args.root
        server.S = server.State()
    try:
        create_server().run(transport="stdio")
    finally:
        server.S.analyzer.stop()
        if server.S.analyzer._thread:
            server.S.analyzer._thread.join()
        if server.S.remote:
            server.S.remote.close()


if __name__ == "__main__":
    main()
