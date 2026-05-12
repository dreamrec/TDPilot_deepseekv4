> Verified: TD 2025.32820

# NDI & Cross-Application Streaming

How to move pixels between machines (NDI) and between apps on the same machine (Syphon/Spout) from inside TouchDesigner — without inventing a routing layer where TD already has one. Most "NDI doesn't work" sessions trace to a subnet mismatch, a stale source name menu, or someone trying to use NDI for what should be a TOP wire.

## The Three Pipes

| Pipe | Scope | Operator (TOP) | Platform |
|---|---|---|---|
| NDI | LAN, cross-machine | `ndiinTOP` / `ndioutTOP` | Cross-platform |
| Syphon/Spout | Same machine, cross-app | `syphonspoutinTOP` / `syphonspoutoutTOP` | Mac (Syphon) + Win (Spout) — unified TOP |
| TOP wire | Inside one TD project | (just a wire) | Always — use this when possible |

**Rule of thumb:** if both endpoints are inside the same `.toe` file, use a wire. If they're in different apps on the same machine, use `syphonspoutinTOP`/`syphonspoutoutTOP`. If they're on different machines on the same LAN, use NDI.

## NDI In — `ndiinTOP`

Receives an NDI source from the network. Key parameters:

- **Source Name** (`sourcename`) — menu of all NDI sources currently visible on the LAN. The menu is populated dynamically by NDI's mDNS discovery.
- **Bandwidth** (`bandwidth`) — `Highest` (full-resolution) vs `Lowest` (proxy, ~640×360, lower bitrate). Use `Lowest` for monitoring; `Highest` for the actual signal path.
- **Extra Search IPs** (`extrasearchips`) — space-separated IPs to query when the sender is on a different subnet. mDNS does not cross subnet boundaries; this is the workaround.
- **Audio** — feed the audio sidecar via the companion **Audio NDI CHOP**, not the TOP. The TOP carries video only.

### Source Discovery from Python

TD 2025+ does not expose `TDU.NDISources` as a documented module. Two reliable patterns instead:

```python
# Pattern A — the modern, authoritative source list: NDI DAT
# Create an ndiDAT once; it lists ALL NDI sources on the LAN with callbacks
# for added/removed/changed. Use this for switchers and routing logic.
ndi_dat = op('/project1/ndi_sources')   # type: ndiDAT
for row in range(1, ndi_dat.numRows):
    name = ndi_dat[row, 'name'].val
    print(name)
```

```python
# Pattern B — the per-operator menu (simpler, but only reflects what
# THIS ndiinTOP sees on its last refresh)
sources = op('/project1/ndi_in1').par.sourcename.menuNames
print(sources)
```

If `Pattern A` is available (NDI DAT exists in the project), prefer it — the DAT has explicit `onSourceAdded` / `onSourceRemoved` callbacks for live source-list maintenance. Fall back to `Pattern B` when you just want a one-shot list at agent-build time.

**Do NOT** try `TDU.NDISources` or `app.ndiInputs` — those are not stable TD 2025 APIs.

## NDI Out — `ndioutTOP`

Broadcasts a TOP onto the LAN as an NDI source. Key parameters:

- **Source Name** (`sourcename`) — the string other receivers see. Default is the machine name; override for clarity (`PROJECTOR_LEFT`, `MAIN_OUTPUT`).
- **Tally** (`tally`) — when ON, exposes NDI Tally state (program/preview flags) that downstream switchers can read. **Leave OFF for installations**; some routers and broadcast tools react to Tally state changes by re-routing or muting.
- **NDI HX** (`ndihx`) — opt-in to NDI High Efficiency codec (HEVC-style). Lower bandwidth, higher CPU at encode, slight added latency. Default off (uncompressed-ish SpeedHQ).
- **Frame Rate Numerator / Denominator** (`framerate_num` / `framerate_den`) — explicit frame rate. For 29.97 use 30000/1001, for 30.00 use 30/1, for 60.00 use 60/1. Wrong frame rate causes 1–2 frame stutters at receivers.
- **Audio** — `ndioutTOP` does not carry audio. Pair with **NDI Out CHOP** when audio is needed; the receiver matches them by Source Name.

## Syphon/Spout — `syphonspoutinTOP` / `syphonspoutoutTOP`

A single unified operator in TD that handles **Syphon on macOS** and **Spout on Windows** transparently. No platform branching in the network — the operator picks the right backend at runtime.

Key parameters:

- **Sender Name** (`servername` on `syphonspoutoutTOP`) — the string other apps see.
- **Source / Active Server** (`activeservername` on `syphonspoutinTOP`) — menu of available senders on the local machine.

**Build-check pattern.** If a project will run on both platforms, gate downstream logic on `app.osName`:

```python
# Inside an executeDAT.onStart or a project init script
if app.osName == 'Mac':
    # Syphon backend is in use; no extra setup
    pass
elif app.osName == 'Windows':
    # Spout backend; ensure GPU is NVIDIA or AMD (Intel iGPU not supported by Spout)
    pass
```

**Hardware gotcha (Windows-only):** Spout requires an NVIDIA or AMD GPU. Intel integrated graphics cannot host Spout senders or receivers. If a Windows machine has only Intel iGPU, fall back to NDI even for same-machine routing.

## Cross-Platform Conditional Wiring

Common scenario: project must move pixels from a Mac to a Windows machine (or vice versa), and within each machine also share with another app (Resolume, OBS, vvvv).

**Recommended topology:**

- **Across machines** → NDI (`ndiinTOP` / `ndioutTOP`)
- **Within a machine** → Syphon (Mac) or Spout (Win), both via `syphonspoutoutTOP`

The agent should NOT branch the network by platform — `syphonspoutoutTOP` already handles both. Branch only when behavior must differ (e.g., a hardware gate like the Intel-iGPU case above).

```python
# Macro example: publish the final render to both NDI (LAN) and local Syphon/Spout
# Wire: render1 → final_null → [ndiout1, syphonspoutout1]
op('/project1').create(nullTOP, 'final_null')
op('/project1').create(ndioutTOP, 'ndiout1')
op('/project1').create(syphonspoutoutTOP, 'syphonspoutout1')

op('/project1/final_null').inputConnectors[0].connect(op('/project1/render1'))
op('/project1/ndiout1').inputConnectors[0].connect(op('/project1/final_null'))
op('/project1/syphonspoutout1').inputConnectors[0].connect(op('/project1/final_null'))

op('/project1/ndiout1').par.sourcename = 'MAIN_LAN_OUT'
op('/project1/syphonspoutout1').par.servername = 'MAIN_LOCAL_OUT'
```

The Mac will publish a Syphon server named `MAIN_LOCAL_OUT`; the Windows machine running the same project publishes a Spout server with the same name. NDI exposes `MAIN_LAN_OUT` identically on both.

## Multi-Machine Sync via Timeline CHOP

When several TD machines render different parts of a wall or stage and feed back via NDI, the receivers drift because each machine's `Timer CHOP` or `absTime.frame` runs independently.

The reliable sync path:

1. One machine is **timecode master** (usually the one driving the show).
2. Master emits a continuous beat or frame count via a **timecodeCHOP** or an OSC broadcast.
3. Each slave reads the master's frame count and uses it as the index into shaders, samplers, and movie players — NOT `absTime.frame`.
4. NDI itself adds 1–4 frames of latency depending on `bandwidth` and `NDI HX` settings. Budget that frame offset into the sync (a `Lag CHOP` with `lag1 = 0.066` ≈ 4 frames at 60 Hz on the slow side, applied to the master signal, often gets you close).

For tight visual sync (<1 frame), NDI is the wrong transport. Use a sync card (Quadro Sync, AMD S400) or a hardware genlock; NDI is good to ~2-frame accuracy at best on a quiet LAN.

## Common Gotchas

- **NDI vs NDI HX**: HX is HEVC-style compressed, lower bandwidth but adds ~1 frame of latency. Default NDI is "SpeedHQ" — lighter compression, lower latency. Don't enable HX unless network bandwidth is the bottleneck.
- **Tally state**: Enabling `tally` on `ndioutTOP` means downstream NDI-aware switchers may treat the source as part of a program/preview matrix. In installations and live visuals where you do NOT want routers reacting to your output, leave Tally OFF.
- **Network discovery requires same subnet**: NDI uses mDNS (multicast DNS). Most managed networks block mDNS across VLANs. If the receiver doesn't see the sender, check the subnet first; use `extrasearchips` only as a fallback.
- **Firewall blocks**: macOS Gatekeeper and Windows Defender Firewall both prompt the first time TD opens NDI ports. If the prompt was missed, TD will run with NDI silently broken until the firewall rule is added manually.
- **Source name collisions**: Two `ndioutTOP` instances with the same `sourcename` on the same LAN produce undefined behavior at receivers — one or the other wins, and the menu may flicker. Always set explicit, distinct source names.
- **Latency**: budget 1–4 frames for NDI standard, 2–5 for NDI HX, ~0.5–1 for Syphon/Spout. Plan transitions accordingly — don't expect frame-accurate sync across NDI without the timecode-master pattern above.

## Anti-Patterns

### Don't: Use NDI for local-machine routing

```python
# WRONG — round-trips through NDI's encode/decode + LAN stack on the same machine
op('/project1/glsl1').outputs → ndioutTOP → ndiinTOP → op('/project1/composite')
```

NDI is doing real network encoding/decoding even when source and receiver are on the same machine. You pay 1–4 frames of latency, CPU, and potentially a network round-trip — all to move pixels you could have wired directly.

**Fix:** Wire the TOPs together. Inside a single project: TOP wire. Across projects on the same machine: `syphonspoutoutTOP` → `syphonspoutinTOP`.

### Don't: Enable Tally in installations

If the output of an installation is connected to a broadcast or stage switcher that consumes NDI Tally state, enabling Tally on your `ndioutTOP` makes your signal participate in program/preview logic. The switcher can blank or re-route your output based on Tally without warning.

**Fix:** Leave `tally = OFF` on `ndioutTOP` unless the installation explicitly requires Tally signaling.

### Don't: Branch the network by platform when `syphonspoutoutTOP` already handles both

```python
# WRONG — over-engineered; the unified operator already handles this
if app.osName == 'Mac':
    op('/project1').create(syphonOutTOP, 'syph')    # doesn't exist in modern TD
else:
    op('/project1').create(spoutOutTOP, 'spt')      # doesn't exist either
```

There is no `syphonOutTOP` or `spoutOutTOP` in TD 2025 — `syphonspoutoutTOP` is the operator on both platforms. Skip the branching entirely.

**Fix:** Use `syphonspoutoutTOP` unconditionally. Only branch on `app.osName` when there's a real platform-specific behavior (e.g., the Intel-iGPU Spout limitation).

### Don't: Trust the source-name menu to update without a refresh

`ndiinTOP.par.sourcename.menuNames` reflects the last NDI discovery cycle, which can be 1–5 seconds stale. A source that just started broadcasting may not be in the menu yet.

**Fix:** Use the NDI DAT for live source tracking with callbacks (`onSourceAdded` / `onSourceRemoved`). Reserve the menu for one-shot lookups at build time.

## TDPilot Tool Pairings

- `td_create_node(family='TOP', type='ndiin')` (and `ndiout`, `syphonspoutin`, `syphonspoutout`) — preferred over `td_exec_python` for building the chain.
- `td_get_param_help(path=<ndiin_top>, paramName='sourcename')` — confirm available sources before setting the name.
- `td_exec_python` with `op(path).par.sourcename.menuNames` — quick one-shot enumeration of visible NDI sources.
- `td_screenshot(path=<ndiin_top>)` — verify the NDI source is actually delivering pixels (not just listed in the menu). A connected-but-silent source shows a black or last-frame frozen image.
- `td_get_errors(path=<ndi_top>)` — surfaces "Source not found" and "No license" (some NDI HX setups need an NDI Tools install) errors that the menu doesn't reveal.

## See Also

- `glsl-idioms.md` — output formats and Null TOP discipline for the final composited frame before NDI/Syphon out.
- `recording-and-export.md` — when you want to record what you're also streaming, branch the `final_null` TOP into both `moviefileoutTOP` and `ndioutTOP`.
- SKILL.md §11 — render pipeline verification (screenshot, not just `td_get_errors == 0`).
