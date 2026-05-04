---
name: TouchDesigner Python Idioms
description: me / op() / parent() / comp.storage / me.run / cookalways and other patterns specific to TD's embedded Python
category: reference
---

# TD Python Idioms

TouchDesigner's bundled Python (3.11 in TD 2025) injects several magic
globals into every module + DAT script. Idioms below are the canonical
patterns — using them keeps code TD-version-portable.

## Magic globals

| Name | What it is | Where it's defined |
|---|---|---|
| `me` | The op that owns the currently-running module / callback | Auto-injected per DAT's module |
| `op('/path')` | Look up an op by absolute or relative path | Global function — NOT a module |
| `parent()` | The parent COMP of `me` (chained: `parent(2)` = grandparent) | Global function |
| `parent.X` | Custom param X of the nearest ancestor COMP | Magic param accessor |
| `project` | The Project object (cookRate, realTime, save(), undo) | Global |
| `app` | Application object (cli args, scriptArgs, pidFile, etc.) | Global |
| `absTime.frame` | Absolute frame counter (increments per cook) | Global |
| `absTime.seconds` | Wall-clock seconds since project start | Global |
| `tdu` | Utility module (Color, Vector, Position, etc.) | Importable: `import tdu` |
| `td` | Type module (OP, CHOP, TOP, ...) | Importable: `import td` |
| `ui` | UI control (panels, undo stack, dialogs) | Global |

## Common patterns

### Reach the project root from a child COMP
```python
# WRONG — parent() is the COMP that runs your code
op = parent().op('something')                 # may not be /project1!

# RIGHT — use absolute path
op('/project1/something')
```

### Cook an op once (escape pull-cooking)
```python
# Force a one-shot cook — registers callbacks, materialises data
some_op.cook(force=True)
```

### Per-frame Python that is RELIABLE
```python
# DON'T rely on op.run(code, delayFrames=N) — silently dropped in
# TD 2025 sometimes
# DO use a force-cooked executeDAT.onFrameStart, OR a constantCHOP
# with absTime.frame expression + chopExecuteDAT
```

### Persist state across module reloads
```python
# DON'T put long-lived state in module-level vars — TD recompiles
# the module on text change and any module-level set/list/dict resets
# DO use comp.storage:
parent().store('my_state', value)
val = parent().fetch('my_state', default=None)
```

### Schedule deferred Python on TD's queue
```python
# .run is a METHOD on Op instances, not on the global op() function
me.run("print('fires next frame')", delayFrames=1)
op('/project1/foo').run("foo.par.x = 1", delayFrames=10)
```

### Read or write DAT content
```python
text = op('myTextDAT').text                   # read whole text
op('myTextDAT').text = "new content"          # write whole text

t = op('myTableDAT')
val = t[1, 'col_name'].val                    # cell read by row/col
t.appendRow(['a', 'b', 'c'])                  # append a row
```

### CHOP samples
```python
chop = op('myCHOP')
n_samples = chop.numSamples
n_chans = chop.numChans
chans = chop.chans()                          # list of channel objs
chans[0].vals[0]                              # first sample of first channel
```

### Custom parameters from Python
```python
# Read
v = parent().par.MyparamName.eval()
# Write
parent().par.MyparamName.val = 0.5
# Pulse (for Pulse-type params)
parent().par.MyparamName.pulse()
```

### Threading rules
- TD's Python API is **NOT thread-safe**. Reading/writing op state
  from a worker thread crashes TD with THREAD CONFLICT.
- DAT callbacks (`onHTTPRequest`, `onWebSocketReceiveText`, etc.) run
  on the cook thread — safe to mutate ops from there.
- `urllib.urlopen` blocks → must run on a worker thread.
- Marshal worker results back to cook via a `Queue` and drain in
  `executeDAT.onFrameStart` (force-cooked).

### "Always cook" via parameter expression
```python
# A constantCHOP with value0 expression = "absTime.frame" is a frame
# counter that increments every frame — guarantees the CHOP cooks.
# Combined with a chopExecuteDAT.onValueChange you get a reliable
# per-frame trigger when executeDAT.onFrameStart misbehaves.
```
