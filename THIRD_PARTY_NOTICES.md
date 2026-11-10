# Source and dependency notices

The application derives from Kaito-Rowing's mdFOAM Viewer codebase. This public edition is distributed under the MIT license in LICENSE. Its source tree includes no simulation datasets, private connection settings, manuscript figures, or predecessor Git history.

No third-party source snippets with separate copyright notices were identified in the retained application source. OpenFOAM source and executables are not included; the application reads files in the OpenFOAM format. The synthetic mesh/field generator is part of this project's source.

Dependencies remain under their own licenses. This project does not relicense them. Wheels depend on separately installed distributions; they do not vendor the libraries below. Plotly JavaScript is served from the installed Plotly distribution, including its upstream license header.

| Direct dependency | Upstream license family |
| --- | --- |
| NumPy | BSD-3-Clause |
| SciPy | BSD-3-Clause (and notices for included components) |
| FastAPI | MIT |
| Uvicorn | BSD-3-Clause |
| Paramiko | LGPL-2.1-or-later |
| Matplotlib | Matplotlib/PSF-derived license (and bundled component notices) |
| Pillow | MIT-CMU |
| Plotly | MIT |
| PyYAML | MIT |
| Pydantic | MIT |
| MCP Python SDK (optional) | MIT |

Tests/build tools include pytest (MIT), HTTPX (BSD-3-Clause), and build (MIT). Transitive dependencies carry their own metadata and license files in the installed distributions. Consult those installed files for the exact version and complete notices, especially when redistributing a self-contained environment.

Before the first release, installed dependency metadata was inspected and the source/output inventory was checked for personal paths, credentials, research figures and private hostnames. No self-contained runtime is shipped.
