# TouchDesigner Component

This folder contains the TouchDesigner-side component and helper scripts.

Files:
- `tdpilot-dpsk4.tox`: drag into `/local` (persists across project opens) or import into your project.
- `callbacks/`: HTTP callback handler code (the `mcp_webserver_callbacks` textDAT body). Composed at .tox-build time by `callbacks/_composer.py`. PR-16 (v1.8.3) split the pre-1.8.3 single-file `mcp_webserver_callbacks.py` into this package — see [callbacks/__init__.py](callbacks/__init__.py) for the contract.
- `ws_callbacks.py`: websocket callback code for event streaming.
- `event_emitter.py`: TD event emitter helper.
- `build_export_mcp_tox.py`: builds a reusable `tdpilot-dpsk4.tox` and installs it into `/local` by default.

Quick setup in Textport (auto-installs into `/local`):

```python
exec(open("/ABS/PATH/TDPilot/setup_mcp_in_td.py").read(), globals(), globals())
```

To install into a specific project instead:

```python
import os
os.environ["TD_MCP_PARENT_PATH"] = "/project1"
```

To export the .tox only (no live install):

```python
import os
os.environ["TD_MCP_PARENT_PATH"] = ""
```
