---
name: TouchDesigner Common Pitfalls
description: Type-name traps, path traps, threading traps, and pull-cooking gotchas that bite agents
category: reference
---

# Common Pitfalls

## Type-name capitalisation (case-sensitive)

TD rejects operator type names with the wrong case. Common surprises:

| WRONG | RIGHT |
|---|---|
| `videoDeviceInTOP` | `videodeviceinTOP` |
| `movieFileInTOP` | `moviefileinTOP` |
| `audioDeviceInCHOP` | `audiodeviceinCHOP` |
| `audioDeviceOutCHOP` | `audiodeviceoutCHOP` |
| `oscInCHOP` / `oscIn DAT` | `oscinCHOP` / `oscinDAT` |
| `oscOutCHOP` / `oscOut DAT` | `oscoutCHOP` / `oscoutDAT` |
| `audioFileInCHOP` | `audiofileinCHOP` |
| `parameterExecuteDAT` | `parameterexecuteDAT` |
| `chopExecuteDAT` | `chopexecuteDAT` |
| `datExecuteDAT` | `datexecuteDAT` |
| `panelExecuteDAT` | `panelexecuteDAT` |

**Rule of thumb**: names with multiple lowercase words are
**all lowercase** between the family suffix.  Single-word ops are
camelCase ending in capital family suffix.

When `td_create_node` returns "Unknown operator type":
1. **DON'T** retry the same name. It will fail again.
2. Call `td_list_families` to see the valid names under the parent.
3. Look up alternatives in this knowledge corpus or
   `td_search_official_docs`.

## Family suffix is mandatory

`box` is not an op type. `boxSOP` is. The same applies to `noise`,
`level`, `sphere`, etc.

## Path traps in `td_exec_python`

Inside `td_exec_python`, `parent()` resolves to **the COMP that runs
the agent** (typically `/project1/tdpilot_API`), NOT the project root.
Always use absolute paths to reach things outside the agent COMP:

```python
# WRONG — finds /project1/tdpilot_API/something OR fails
op = parent().op('something')

# RIGHT
op('/project1/something')
op('/project1')                         # the project root itself
```

## Pull-cooking traps

TD only cooks an op when something downstream needs it OR it's
displayed in a viewer. Programmatically-created executeDATs,
chopExecuteDATs, and webserverDATs sometimes get orphaned from the
cook chain and silently never fire their callbacks — even with
`active=True` and `framestart=True` set.

**Fix**: call `dat.cook(force=True)` once after creation. That
registers the callbacks with TD's frame ticker and they fire every
frame thereafter for the lifetime of the op.

## `op.run` is `<op>.run`, not `op.run`

`op` is a path-lookup function, not a module. `run` is a METHOD on Op
INSTANCES.

```python
# WRONG — op is a function, not an object with attributes
op.run("foo()", delayFrames=1)

# RIGHT
me.run("foo()", delayFrames=1)
op('/some/path').run("foo()", delayFrames=1)
```

## `<op>.run(delayFrames=N)` can be silently dropped

In TD 2025.32460 we've seen scheduled Python via `me.run` simply not
fire — the Run object is created and `.run()` returns success, but the
deferred callback never executes. **Don't build per-frame loops on
this.** Use a force-cooked executeDAT.onFrameStart instead.

## Module-level state resets on text change

A textDAT's module recompiles every time its text is edited. Module-
level `_my_set = set()` resets to empty on edit. Any state that needs
to survive live edits belongs in `comp.storage`:

```python
def _get_state():
    s = parent().fetch('my_key', None)
    if s is None:
        s = set()
        parent().store('my_key', s)
    return s
```

## Threading

The Python API is **NOT thread-safe**. From a worker thread, even
calling `str(my_op)` can trigger TD's THREAD CONFLICT detector and
freeze TD.

When the agent loop uses urllib (blocking), the urlopen call runs on a
worker thread but **all TD ops MUST be touched on the cook thread**.
The pattern: worker enqueues a (call_id, fn_name, args) request, cook
thread drains the queue in `onFrameStart`, runs the function, and
notifies the worker via a `threading.Condition`. See the
`CookThreadDispatcher` class in `tdpilot_api_runtime.py`.

## DAT callback threads

DAT callbacks (`onHTTPRequest`, `onWebSocketReceiveText`,
`onTableChange`, etc.) **run on the cook thread**. Safe to mutate ops
directly from inside them. Don't spawn threads from there unless you
plan to marshal results back via the same cook-thread queue pattern.

## Save / undo before risky changes

A multi-step build that's halfway done leaves the project in an
inconsistent state if something fails. **Before risky multi-step
builds, call `td_project_lifecycle action=save`** so you can
`action=undo` back if something breaks. The agent's system prompt
encodes this rule but it's easy to forget when chains get long.

## webRenderTOP origins and WebSockets

A webRenderTOP loaded via `file://` cannot open `ws://` connections —
Chromium's mixed-origin policy blocks it. Always serve the HTML from
the same TD webserverDAT the WebSocket lives on (`http://127.0.0.1:port/`)
so origin matches.

## Windows specifics

- HTTPS verification: `ssl.create_default_context()` loads the Windows
  cert store automatically. Don't search for cafile paths on Windows.
- Sound playback with volume: use `ctypes.windll.winmm.mciSendStringW`
  with `setaudio alias volume to N` (0-1000). `winsound` doesn't
  support volume.
- Default sounds in `%WINDIR%\Media\` exist on most installs but are
  pruned on LTSC and Server SKUs — always have a fallback chain.
- Localhost binds (`127.0.0.1`) don't trigger Defender Firewall
  prompts. Don't bind to `0.0.0.0` unless you genuinely need LAN.
