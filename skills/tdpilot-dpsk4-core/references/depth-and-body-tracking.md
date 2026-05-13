# Depth & Body Tracking — Sensor Choice and Pipeline

> Verified: TD 2025.32820

Sensor decisions are the load-bearing pillar of any interactive install. Most "the tracking is laggy / jittery / dropping" complaints trace back to a mismatch between the sensor's coordinate space and what the patch downstream is trying to do with it. This file catalogs the working sensors in TD 2025, what they emit, and how to wire them into particle systems without the standard mistakes.

## Sensor Coverage

In rough descending order of how friendly each is to a TD 2025 install:

### Azure Kinect TOP — Win-only, the workhorse for body tracking

- **Operator:** `Kinect Azure TOP` (color, depth, IR, point cloud) + `Kinect Azure CHOP` (body skeleton, IMU).
- **Streams:** Color (BGRA, up to 3840×2160 @ 30 fps), Depth (16-bit greyscale, 320×288 / 640×576 / 512×512 / 1024×1024 @ 5–30 fps depending on FOV mode), IR (16-bit), and a derived point cloud TOP.
- **Body tracking:** 32 joints per body (Microsoft updated from 25 mid-Body-Tracking-SDK 1.x), up to **6 simultaneous bodies**.
- **Coordinate space:** **camera-relative, +X right, +Y down, +Z forward, units = millimeters**. Trips up nearly every shader port because most renderers want meters and +Y up.
- **Platform:** Windows only natively. **Mac/Linux workarounds** require a Windows machine on the network running a NDI or Spout-to-Syphon bridge that re-broadcasts the depth + skeleton; latency adds 1–2 frames and skeleton joint count drops to whatever the bridge encodes. The bridge approach is the only path on Mac as of 2026.
- **TDPilot tools:** `td_get_operator_doc(opType="kinectAzureTOP")` for full param list. `td_create_node` with `nodeType="kinectAzure"` (TOP and CHOP variants).

### ZED TOP — Stereo depth, outdoor-capable, cross-platform

- **Operator:** `ZED TOP` (TD 2025 restructure consolidated what used to be ZED Image TOP / ZED Depth TOP / ZED Body Track TOP into one central node with a Sources page selecting which streams to emit). Companion `ZED CHOP` for skeleton/IMU.
- **Streams:** Left/right RGB images, depth (16-bit float, in meters), confidence map, normals, point cloud, **plane detection** (floor/wall masks), and skeleton.
- **Body tracking:** 34 or 38 joints (ZED 2i / ZED X with neural body track), up to **~2 reliable bodies** before tracking quality degrades. Plane-detection output is the strength here — finding the floor under a dancer for projection is a one-tool operation.
- **Coordinate space:** camera-relative, **+Z forward, +Y up, units = meters**. The "+Y up" choice (opposite Kinect) means ZED → render works without a flip; Kinect → render requires `y *= -1`.
- **Platform:** Windows, Linux, **macOS** (TD 2025 ships ZED SDK 4.x bindings on Mac via the Sources page; previously Mac users were locked out). **Outdoor capable** — stereo depth doesn't depend on IR projection, so direct sunlight doesn't kill it the way it kills Kinect.
- **Resolution / fps:** HD2K (2208×1242) @ 15 fps, HD1080 @ 30, HD720 @ 60, WVGA @ 100.

### RealSense TOP — Intel D400/D455 series, cross-platform

- **Operator:** `RealSense TOP` (color + depth + IR) and `RealSense CHOP` (IMU on D435i / D455).
- **Streams:** Color (BGRA), Depth (16-bit, mm), IR-left/right. **No native body tracking** — pair with MediaPipe or Cubemos SDK if you need a skeleton.
- **Resolution / fps:** 1280×720 @ 30 fps depth (D435), 848×480 @ 90 fps (low-latency interactive mode), color up to 1920×1080 @ 30.
- **Coordinate space:** camera-relative, **+X right, +Y down, +Z forward, units = meters**.
- **Platform:** Windows, Linux, macOS. The most portable hardware depth sensor for cross-platform installs.

### MediaPipe via Script TOP + Python — Software-only, no special hardware

- **Operator:** Custom `Script TOP` (or `Script CHOP`) running Google's `mediapipe` Python package against any video source.
- **Outputs:** 33 pose landmarks, 21×2 hand landmarks, 478 face landmarks, all as normalized 0–1 image-space coordinates.
- **Cross-platform:** Yes. **Doesn't need a depth sensor** — works off any webcam, Video Device In TOP, or pre-recorded `Movie File In TOP`. This is the only path for installations where you can't add hardware to a venue (kiosk, museum, retail).
- **Limits:** **No real 3D.** MediaPipe's "world landmarks" are inferred per-frame from monocular cues and drift unpredictably; trust the 2D image-space landmarks, not the z-axis. Frame rate is CPU-bound — expect 15–30 fps on a recent CPU, down to ~10 on integrated graphics.
- **Latency:** ~80–120 ms end-to-end vs. ~30 ms for Kinect/ZED. Plan for it.
- **Wiring:** `Video Device In TOP` → `Script TOP` (runs MediaPipe per-frame, returns landmarks as table data) → downstream `Table DAT` / `CHOP Execute DAT` to fan landmarks into CHOP channels.

### Kinect v2 / Kinect for Xbox One — Legacy Win-only

- **Operator:** `Kinect TOP` and `Kinect CHOP`.
- **Streams:** Color (1920×1080 @ 30), Depth (512×424 @ 30), IR, Body Index, Skeleton (25 joints, up to 6 bodies).
- **Coordinate space:** camera-relative, +Y up, units = meters.
- **Status:** Deprecated by Microsoft. Hardware still works, drivers are stable on Windows 10/11, but no new features land. **Use Azure Kinect instead for new builds** — only stick with v2 if hardware is already on-site or budget rules out the ~$400 Azure unit.
- **Critical:** **Do NOT run Kinect v2 and Azure Kinect on the same USB controller.** They contend for the same USB 3.0 bandwidth and fight; the v2 will appear to work then silently drop the skeleton stream. Use separate PCIe USB cards or pick one sensor.

### Kinect v1 — Even more legacy

- **Operator:** `Kinect TOP` (older driver mode).
- **Resolution / fps:** 640×480 @ 30 depth, ~20 joint skeleton.
- **Status:** Only worth using if the hardware is the only thing available and someone else is paying. Replace it.

### Orbbec / Structure.io / iPhone TrueDepth — Brief notes

- **Orbbec Astra / Femto:** Drop-in Kinect-v2 replacement; some models use the same Body Tracking SDK as Azure Kinect. TD support via **`OpenNI2 TOP`** or vendor SDK plugin. Cross-platform-ish.
- **Structure.io Mark II:** iPad-mounted depth sensor. Talks to TD via NDI bridge from a paired iPad app. Latency is rough (~3 frames).
- **iPhone TrueDepth (Face ID camera):** Front-facing structured-light depth, 640×480 @ 30. Reach TD via **ARKit → Unity/Unreal → Spout/Syphon → TD**, or use the Live Link Face app (NDI bridge). Excellent for face-tracking installs, useless for body.

## Body Tracking → Particle Systems (POP-side)

The point of all this is usually "drive particles from a person." The pattern:

### Pattern 1: Skeleton joints → POP instance grid

```
Kinect Azure CHOP (skeleton channels)
  → Select CHOP (rename `body0:joint_pelvis:tx` etc. to clean names)
  → Math CHOP (scale mm → meters; multiply by 0.001)
  → CHOP-to-DAT  (one row per joint, columns = tx,ty,tz)
  → DAT to SOP / DAT to POP
  → POP particle emitter (instanced at joint positions)
```

Or, more directly in POP-only flow:

```
Kinect Azure CHOP → CHOP to POP   (channels become point attributes)
  → POP Particle Generator (count=joint_count, source attribute=`P`)
```

### Pattern 2: Hand landmarks (MediaPipe) → Noise field sample point

For a single hand driving a noise distortion:

```
Script TOP (MediaPipe)
  → CHOP from TOP-row (sample landmark 8 = index finger tip)
  → Math CHOP (remap 0..1 → -1..1)
  → Export to GLSL TOP uniform `uHand`
```

The shader uses `uHand` as the center of a falloff or noise sample. Don't try to wire 21 hand landmarks individually — fan them through a CHOP-to-TOP if the shader needs all of them.

### Pattern 3: Skeleton CHOP channels → CHOP-driven parameter expression

For audio-like reactivity where one joint drives one parameter:

```python
# in a CHOP-driven parameter expression on a POP Force node
op('skeleton_chop')['body0:joint_wrist_right:tx']
```

**Anti-pattern:** Do NOT do this from a Python expression that polls `op('skeleton_chop')` inside a generic `expr()` field — that's slower and re-binds the OP reference every frame. Use the CHOP channel reference syntax (the form above) — TD compiles it into a direct binding.

## Coordinate-Space Gotchas

This is where most chains silently break. A summary table:

| Sensor | Origin | +X | +Y | +Z | Units |
|---|---|---|---|---|---|
| Azure Kinect | Camera | right | **down** | forward | **mm** |
| Kinect v2 | Camera | right | up | forward | meters |
| ZED | Camera | right | up | forward | meters |
| RealSense | Camera | right | down | forward | meters |
| MediaPipe (image) | top-left | right | **down** | (depth lies) | **normalized 0–1** |
| MediaPipe (world) | hip mid-point | right | up | forward | meters (drifty) |
| TD world space | Project origin | right | up | (away from cam) | depends on scale |

Two conversion steps almost everyone needs:

```python
# Azure Kinect mm → TD meters, +Y down → +Y up
tx = az['tx'] * 0.001
ty = az['ty'] * -0.001   # FLIP
tz = az['tz'] * 0.001
```

```glsl
// MediaPipe 0..1 image space → centered -1..1 with aspect
vec2 uv = vec2(lm.x, 1.0 - lm.y);    // flip Y (image origin top-left)
uv = uv * 2.0 - 1.0;
uv.x *= uTDOutputInfo_res.x / uTDOutputInfo_res.y;
```

For **camera → world**, you need the sensor's pose in your scene. Build it once via a `Constant CHOP` with the rig measurement (height + tilt + offset), and apply it via a `Transform CHOP` downstream of the sensor CHOP. Don't bake it into the shader — it'll bite you the next time you move the sensor.

## Common Installations

### Single-user interactive wall (1 Kinect, 1 person)

- **Sensor:** Azure Kinect (Win) or ZED 2i (cross-platform / outdoor).
- **Mount:** Top of projection wall, ~3 m height, ~10° downward tilt. Avoid mounting at user eye level (skeleton wraps unpredictably when arms cross the camera).
- **Tracking range:** Azure Kinect Wide FOV reliably tracks 0.5–4.5 m; ZED 2i reliably 0.3–10 m.
- **Patch:** Skeleton CHOP → joint-by-joint Select CHOP → Transform CHOP for the sensor pose → POP emitter.

### Multi-user (2–6 people)

- **Sensor:** Azure Kinect supports 6 bodies natively. ZED 2i comfortably handles 2; degrades above 4. For >6, **sensor fusion** is required: two or more sensors, calibrated via a checkerboard, with skeleton IDs reconciled in a Script DAT.
- **Watch for:** Body-ID swaps when two users cross paths. Hold-and-track with `body0:joint_pelvis:tracking_state` channel — drop the skeleton if confidence drops to 0 for >0.5 s rather than chasing a corrupted skeleton.

### Outdoor

- **Sensor:** ZED only. Kinect / RealSense IR projectors are washed out by direct sunlight. ZED's stereo depth is wavelength-agnostic.
- **Watch for:** Surface texture matters — stereo depth needs visual features. Pure white walls and reflective surfaces produce holes. A noisy texture on the projection surface (chalk lines, gravel) actually helps depth quality.

## Anti-Patterns

### Don't: Poll skeleton in a Python expression per-frame

```python
# WRONG — Python evaluation per frame per parameter
op('skeleton_chop').sample(t=absTime.frame)['body0:joint_wrist_right:tx']
```

This re-runs Python every cook, every parameter. With 5+ parameters bound this way the project drops below realtime fast.

**Fix:** Use the CHOP channel reference syntax (`op('skeleton_chop')['body0:joint_wrist_right:tx']`) or a `CHOP Execute DAT` that pushes values into storage once per frame. SKILL.md §9 covers the expression/channel-reference distinction.

### Don't: Trust sub-pixel landmark accuracy from MediaPipe

MediaPipe's landmarks are smoothed across frames internally, but the smoothing window is too short for sub-pixel stability — the landmarks lie at the 0.5–1 pixel level. If your shader uses them as a UV sample point at high resolution, you'll see jitter that has no audio/scene cause.

**Fix:** Apply a `Lag CHOP` (lag = 0.15) downstream of the MediaPipe landmark CHOP before using it for any UV-precision operation. Lag CHOP is fine here — landmarks come in at 30 fps, not timeslice-expanded like AudioSpectrum.

### Don't: Run Kinect v2 + Azure Kinect on the same USB bus

USB 3.0 bandwidth contention. The v2 will silently drop skeleton; the Azure will sometimes drop entire frames. Symptoms look like sensor flakiness; root cause is USB topology.

**Fix:** Separate PCIe USB cards, or eliminate one sensor. Use `lsusb -t` (Linux) / `usbview.exe` (Windows) to verify they're on different controllers.

### Don't: Assume depth values are in the units you expect

Azure Kinect depth in mm, RealSense in mm, ZED in meters. Mixing them in the same patch — `Depth from Azure + threshold meant for ZED` — produces images that look broken but compile clean.

**Fix:** Always check `op(depth_top).par.depthunits` (where available) and normalize early in the chain with a `Math CHOP / TOP` to a known unit. Document the choice in a `Text DAT` next to the sensor node.

### Don't: Rely on body tracking through transparent surfaces

IR-based sensors (Kinect, RealSense) see through clear glass into whatever's behind it, then report a confused skeleton. Stereo (ZED) sees the reflection.

**Fix:** Move the sensor in front of the glass, or accept that you need to filter by depth range to ignore everything beyond ~5 m.

## TDPilot Tool Pairings

- `td_get_operator_doc(opType="kinectAzureTOP")` / `kinectAzureCHOP` / `zedTOP` / `realsenseTOP` — full parameter docs.
- `td_get_hints(nodeType="kinectAzure")` — sensor-specific quirks (default joint count, USB notes).
- `td_get_info()` — shows current TD build; confirm 2025.32820+ for the ZED restructure.
- After wiring the skeleton CHOP, `td_chop_data(path="<skeleton_chop>")` to verify channel names match the expected pattern (`body0:joint_pelvis:tx`, etc.). If channel names differ from the docs, the SDK version is older and downstream selectors won't match.
- For MediaPipe-via-Script-TOP setups, `td_python_env_status()` confirms `mediapipe` is importable before the patch tries to use it.

## See Also

- `glsl-idioms.md` — UV/coordinate-system patterns that show up when sampling sensor textures in shaders.
- `audio-reactive-glsl.md` — the same CHOP-channel-reference vs Python-expression distinction applies to spectrum data.
- `anti-patterns.md` — general patching traps; sensor-specific ones live here.
- SKILL.md §9 — expression vs channel reference rules.
- SKILL.md §11 — render-pipeline pitfalls (some apply downstream of sensor data: NaN at instance positions, etc.).
