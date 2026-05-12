# MIDI & OSC Control — Wiring Hardware to TouchDesigner

> Verified: TD 2025.32820

External control is where most live-show projects break at curtain time: a fader stops responding, an OSC packet arrives on the wrong port, the controller's LEDs disagree with TD's state. This reference covers the verified operators, the auto-learn patterns, the bidirectional-feedback tricks, and the controller-specific recipes that prevent those failures.

## MIDI Operator Cheat-Sheet

| Operator | Mode | Use when |
|---|---|---|
| **MIDI In CHOP** | Channel-per-CC stream | You want a single channel per CC/Note that updates every cook. Bind via parameter expressions or CHOP Export. |
| **MIDI Out CHOP** | Channel-driven send | Sending values from TD to a controller (motorized faders, LED rings) — channel values map to outgoing CC/Note. |
| **MIDI Map CHOP** | Learn + reorder | Auto-learn workflow: hit a control on the hardware, it appears as a named channel. Best for ad-hoc mapping. |
| **MIDI In DAT** | Raw message log | Debugging unknown controllers — every message arrives as a row with status/data1/data2. Cook once, inspect, then build the proper CHOP chain. |
| **MIDI Event DAT** | Per-message callback | Triggering Python on Note On / CC change. Use sparingly — see anti-patterns below. |

**Default rule:** for normal parameter control, use **MIDI In CHOP** with Channel Mode binding. For learn workflows, use **MIDI Map CHOP**. For per-message scripted reactions (clip launches, mode toggles), use **MIDI Event DAT**.

## OSC Operator Cheat-Sheet

| Operator | Mode | Use when |
|---|---|---|
| **OSC In CHOP** | Address-as-channel | Numeric parameters streaming from TouchOSC, Lemur, custom apps. One OSC address = one CHOP channel. |
| **OSC Out CHOP** | Channel-as-address | Sending TD CHOP channels to remote displays / lighting boards / other TD instances. |
| **OSC In DAT** | Address + payload rows | OSC where the payload is heterogeneous (strings, blobs, mixed types) or where you need pattern matching across addresses. |
| **OSC Out DAT** | Scripted send | Triggering bundled or hand-crafted OSC messages from Python (`op('oscout1').sendOSC('/cue/go', [1])`). |

**Text vs CHOP-channel modes:**

- **OSC In CHOP** parses numeric arguments into channels. Fast, cookable, bindable. No string handling.
- **OSC In DAT** stores each incoming message as a row (`address`, `args`). Slower per-message but handles strings, blobs, and address wildcards.

Use the CHOP when the data is "knob turned to 0.42." Use the DAT when the data is "/scene/load 'intro'" or "/render/state.json {…}."

## Auto-Learn Patterns

### MIDI Map CHOP — Built-in Learn

The MIDI Map CHOP has a `Learn` toggle on its parameters page. The workflow:

1. Set `MIDI Map CHOP.par.activedevice` to the controller's device name (or `*` to listen to all devices).
2. Click `learn` (or set `op('midimap1').par.learn = True` from Python).
3. Move a control on the hardware. A new channel appears, named like `cc23` or `n60` (CC #23, Note #60).
4. Rename the channel via the operator's `Channels` page — that becomes the stable name the rest of the network references.
5. Toggle `learn` off when done. Existing channels persist across project save/load.

The renamed channels become first-class CHOP channels — bind them with CHOP Export or read them in parameter expressions.

### OSC In DAT — Address Pattern Matching

OSC In DAT supports OSC's wildcard address matching natively. The `Filter Address` parameter accepts patterns like:

| Pattern | Matches |
|---|---|
| `/scene/*` | `/scene/load`, `/scene/save`, `/scene/clear` |
| `/track/?` | `/track/1`, `/track/2`, but not `/track/10` |
| `/{kick,snare}/*` | `/kick/level`, `/snare/pan`, etc. |
| `/fx/[1-4]/*` | `/fx/1/wet` through `/fx/4/wet` |

For auto-learn, sniff all addresses (`Filter Address = *`) for a few seconds, then promote the observed patterns into explicit filters for the production chain.

### Preset-System Integration

The preset-systems pattern uses auto-learn to bind hardware controls directly to parameters — see `preset-systems-and-ui.md` §11 for the `MIDIMapper` class that combines learn-mode flag, CC-to-param dictionary, and per-parameter range remapping (0–127 MIDI → param's normMin/normMax). The same shape works for OSC by swapping `cc` for `address`.

## Bidirectional Feedback Patterns

One-way control feels fine until two operators want the same control source. Bidirectional feedback keeps hardware state in sync with TD state — essential for motorized faders, encoder LED rings, and any "scene recall" workflow where the controller has to show the new state.

### Motorized Faders (BCF2000, FaderPort)

The hardware has motorized faders that physically move when TD sends a CC. Build a feedback loop:

```
slider in TD → param → MIDI Out CHOP (channel cc7) → hardware
hardware fader move → MIDI In CHOP (channel cc7) → param
```

The MIDI Out CHOP needs the same channel name as the MIDI In CHOP's channel. To avoid feedback storms (TD sends, hardware echoes, TD sends again), gate the outgoing message:

```python
# In a CHOP Execute DAT on the param-source CHOP
def onValueChange(channel, sampleIndex, val, prev):
    if abs(val - prev) > 0.001:   # ignore round-trip noise
        op('midiout1')[channel.name] = val
```

### Encoder LED Rings (Push, Launch Control XL, Maschine)

Encoder rings display the current value as a ring of lit LEDs. The protocol varies — most use CC value 0–127 mapped to LED position, some require sysex. For Push and Push 2, the user-mode LED protocol is CC on channel 1.

```python
# Light ring 0 (encoder 0) to position matching paramVal in [0..1]
midiVal = int(paramVal * 127)
op('midiout1').sendControl(0, 71, midiVal)  # CC 71 = encoder 0 on Push
```

### Bidirectional OSC (TouchOSC, Lemur, custom)

OSC clients usually listen on a different port than they send from. Set up two operators:

- **OSC In CHOP** listening on TD's inbound port (e.g., 9000).
- **OSC Out CHOP** with `Network Address` pointing at the iPad/phone's IP and the client's listening port (e.g., 9001).

TouchOSC and Lemur both support remote screen layouts that auto-update from incoming OSC — set `OSC Out CHOP.par.activechannels = *` and the controller mirrors TD's state immediately.

## Common Controllers — TD-Side Recipes

### Ableton Push 1 / Push 2

- **Pads:** MIDI channel 1, Notes 36–99 (8×8 grid, bottom-left = 36). Velocity 0–127 maps to pad pressure.
- **Encoders:** MIDI channel 1, CC 71–78 (top row), CC 14–21 (lower rows depending on Push version). Relative mode (centered around 64) — set `MIDI In CHOP.par.ccmode = 'relative'`.
- **Buttons:** MIDI channel 1, CCs in the 1–119 range; layout differs Push 1 vs Push 2.
- **LEDs:** Send Note On/Off with velocity = LED color (Push 2 uses an extended palette; check Ableton's Push 2 MIDI doc).

### Novation Launchpad (Mini/MK2/Pro/X)

- **Grid pads:** MIDI Notes in an 8×8 grid. The exact note numbering depends on the model — Mini and MK2 use ascending across the grid (Mini: 0–63 with offsets; MK2: 11–88 with gaps); Pro and X use a different layout. Read the model's programmer's reference once.
- **RGB LEDs:** Send Note On with velocity = palette index (most Launchpads use a 128-entry palette). RGB sysex messages allow full 7-bit-per-channel color on Pro/X.
- **MIDI Out CHOP** with `Active Channels = *` and CHOP Execute DAT writes the lighting state per cook.

### Behringer BCF2000 / BCR2000

Standard MIDI CC mapping out of the box. The BCF2000's motorized faders make it the cheap-and-cheerful choice for bidirectional control. CCs are user-configurable but default to CC 1–8 (faders), CC 81–88 (encoders).

### PreSonus FaderPort (8/16)

DAW-protocol device — speaks Mackie Control Universal (MCU), not raw MIDI CC. Use a **MIDI Map CHOP** with the FaderPort's MCU profile, or write a custom Python translator from MCU sysex to CHOP channels.

### Generic HID (Korg nanoKONTROL, custom gamepads)

When MIDI doesn't expose enough buttons or the device is HID-only (game controllers, sliders/joysticks that don't speak MIDI), the **DirectXInput CHOP** or **Joystick CHOP** handles HID natively on Windows. On macOS, use the **MIDI In CHOP** if the device exposes a MIDI mode, otherwise fall back to a Python `pyusb` script via `td_exec_python` — last resort.

## Common Gotchas

### MIDI Clock vs MIDI Time Code

- **MIDI Clock** (24 PPQN pulses per quarter note) syncs *tempo* — TD's Beat CHOP can lock to incoming clock via the MIDI In CHOP. Use for live tempo sync from a DAW or hardware sequencer.
- **MIDI Time Code (MTC)** transmits absolute clock time (HH:MM:SS:FF). Use the **Time Code CHOP** to lock TD's timeline to a video deck or theatrical timecode source.

These are different protocols carried over the same MIDI port. Pick the one the upstream device sends — never assume.

### OSC Port Collisions

UDP ports are exclusive per process. If two apps both try to listen on port 9000, the second one fails silently (or, on macOS, raises a `bind: address in use` error in the Textport).

- Pick a TD-specific inbound port (9090, 9101) and reserve it in project docs.
- Outbound port doesn't collide — multiple senders can target the same destination port.
- If OSC In CHOP shows `Status = Error` and no channels appear, check Activity Monitor / Task Manager for another process holding the port.

### Latency Hierarchy

Rule of thumb at 1 ms scale:

```
Serial (USB-HID, FTDI)  <  MIDI (USB or DIN)  <  OSC over UDP (LAN)  <  OSC over WiFi
~1ms                     ~3ms                     ~5ms                  ~20-50ms jitter
```

For tight performance — drum-trigger response, beat-locked light cues — use MIDI or serial. For loose UI control (sliders, color picks), OSC over LAN is fine. OSC over WiFi has jitter that breaks rhythmic timing.

### Channel Offset (1-indexed vs 0-indexed)

TD's MIDI CHOP parameters are **1-indexed** for MIDI channels (channel 1 = first channel). Most MIDI hardware also uses 1-indexed channel labels. But many MIDI libraries (mido, rtmidi, web MIDI) use **0-indexed** internally. When porting code from external sources, audit the channel numbers — off-by-one here silently routes to the wrong device or instrument.

For CC and Note numbers, TD uses **0-indexed** (CC 0–127, Note 0–127). MIDI hardware also uses 0-indexed numbering for CC and Note.

## Anti-Patterns

### Don't: Poll `op('midiin1')[0]` every frame in a Python expression

```python
# WRONG — Python expression on a parameter, fired every cook
op('myParam').par.value.expr = "op('midiin1')[0]"
```

This works but rebuilds the OP-reference + channel-lookup chain every frame. At 60 FPS with 30 such params, it's a measurable cook-time cost.

**Fix:** use **Channel Mode** binding instead. Right-click the parameter → `Bind` → select the MIDI In CHOP and channel. TD caches the binding at network-build time.

### Don't: Use OSC over UDP for life-safety or money-on-the-line

UDP is fire-and-forget — packets can be dropped under network congestion, and OSC doesn't retransmit. For show-control cues where a missed packet means the wrong scene fires (or worse — a pyrotechnic doesn't trigger), use **TCP-OSC** (OSC In/Out DAT with `Protocol = TCP`) or **serial** over a dedicated cable.

For mission-critical theatrical control, the canonical protocols are **OSC over TCP**, **MIDI Show Control (MSC)**, or **DMX/Art-Net** with feedback — never bare UDP.

### Don't: Trust hardware feedback as ground truth

Motorized faders and LED rings show *what TD told them*, not necessarily *what the param is*. If a feedback loop breaks (cable unplugged, MIDI Out CHOP cooking disabled), the hardware lies silently. The Python parameter value remains the source of truth.

**Fix:** during debug, `td_get_params(path=<target>)` against TD's state, not against the controller's LEDs.

### Don't: Bind a MIDI Event DAT to a hot CC (e.g., a fader)

MIDI Event DAT fires its `onReceiveMIDI` callback once per incoming message. A fader sending 100 CC messages per second through Python is fine for one fader; with 16 of them, it becomes the cook-time bottleneck.

**Fix:** use **MIDI In CHOP** for continuous controls (faders, knobs). Reserve **MIDI Event DAT** for discrete events (Note On/Off, program change, button presses).

## TDPilot Tool Pairings

- `td_create_node(operator_type="midiinCHOP", ...)` — verify with `td_get_param_help` before binding; some MIDI params (`activedevice`, `ccmode`) only appear after the device is selected.
- `td_search_official_docs(query="MIDI Map CHOP learn workflow")` — official docs cover device-specific quirks (Push relative encoders, Launchpad note layouts) that aren't surfaced in `td_get_hints`.
- `td_exec_python` with `op('midimap1').par.learn = True` — toggle learn from the agent side when building auto-learn UI.
- `td_get_errors(path=<oscin_chop>)` — port collisions surface here as `bind error`.
- `td_screenshot(path=<midiin_chop>)` — viewer of MIDI In CHOP shows live channel values; quick way to confirm a controller is talking to TD before chasing higher-level issues.

## See Also

- `preset-systems-and-ui.md` §11 — `MIDIMapper` auto-learn class and bidirectional `sendFeedback` pattern, with full code.
- `anti-patterns.md` — broader catalog of TD traps; the MIDI Event DAT polling trap above belongs to the same family as "Don't poll `top.save()` for animation."
- SKILL.md §0 — preflight rules; verify operator + params before wiring hardware-bound chains.
