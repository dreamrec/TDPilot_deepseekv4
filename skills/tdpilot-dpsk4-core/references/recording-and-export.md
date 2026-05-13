# Recording & Video Export

Decisions before you press record, and traps to avoid. The default failure mode here is: the recording starts, runs for 4 minutes, and the resulting file is 0 bytes / a single frame / the wrong resolution / the wrong codec for the user's delivery.

## License-Tier Codec Matrix

| Codec | Non-Commercial | Commercial | Notes |
|---|---|---|---|
| ProRes (mac) | ✓ | ✓ | macOS-only; large files; high quality. First choice on Mac. |
| MJPEG (`mjpa`) | ✓ | ✓ | Cross-platform fallback. Larger files than H.264, still works. |
| H.264 | ✗ | ✓ | Standard small-file delivery. Commercial license required. |
| H.265 / HEVC | ✗ | ✓ | Better compression than H.264. Commercial only. |
| AV1 | ✗ | ✓ (2025.32820+) | Movie File Out gained AV1 in the May 2026 build. |
| VVC | ✗ | ✓ (2025.32820+) | Also added in the May 2026 build. |

**Always check the license tier first** with `td_get_info`. If the project is Non-Commercial and the user asks for an H.264 / H.265 / AV1 export, **say so and propose ProRes (mac) or MJPEG** — don't silently fail at record time.

## Resolution Cap on Non-Commercial

Non-Commercial TouchDesigner clamps every TOP output to **1280×1280**. If a downstream TOP requests more (e.g., a Render TOP at 1920×1080), TD silently caps it to 1280×720 and the user sees stretched output in the final file.

Workaround: set the output TOP's `outputresolution = 'custom'` and explicitly request a resolution that fits the cap (e.g., `(1280, 720)` for 16:9 delivery). Document the constraint in the user's project so it doesn't bite at delivery time.

For 1920×1080 or 4K output, the user needs a Commercial license — flag this early in the conversation.

## TOP.save() Is Useless for Animation

`top.save(path)` snapshots the **current GPU texture** at the moment of the call. If you call it in a loop or via Execute DAT, each call captures the **same texture** (the one resident at call time) — you get N identical PNGs, not an animation.

```python
# WRONG — captures one frame N times, not N frames
for i in range(100):
    op('out1').save(f'/tmp/frame_{i:04d}.png')
```

Always use `moviefileoutTOP` for animation. Its cooks are frame-locked to the timeline (or to `playmode`), so each frame written is the frame that was rendered.

`top.save()` is only correct for single-frame stills at deliberate moments — e.g., capturing a final composition after a manual setup.

## TD 2025.32820 Movie File Out Additions

The May 2026 build added:

- **VVC** and **AV1** video codecs (Commercial)
- **AAC** and **Opus** audio codecs
- **Exif metadata** embedding
- **Stereo** and **Spherical** layout metadata (180° / 360° projection metadata, side-by-side / top-bottom)

If the user is delivering for VR / 360° platforms, set the spherical layout metadata at export — the player needs it to map the equirectangular frames correctly. Without metadata, the file plays as a flat distorted rectangle.

## Pre-Recording Checklist

Before starting a `moviefileoutTOP` record, verify:

1. **FPS > 0.** `td_cooking_info` or `td_get_state_vector` shows the project cook rate. A frozen project records a frozen movie.
2. **Output is non-black.** `td_screenshot(path=<source_top>)` and look at the image. A working shader at preview time has often broken at record-resolution because of feedback-init or texture-format mismatches.
3. **Output path is set and writable.** Don't trust the previous record's path — projects move, drives unmount. `os.access(os.path.dirname(path), os.W_OK)` from `td_exec_python` if uncertain.
4. **Disk has room.** ProRes at 1080p eats ~700 MB per minute; MJPEG ~400 MB; H.264 ~100 MB. Plan accordingly for long runs.
5. **Audio source is routed if needed.** If recording sound, the audio chain has to terminate at the moviefileoutTOP's audio input — separate from the video input.
6. **Timeline behavior is what you want.** Set `playmode = 'sequential'` to record exactly N frames, or `'continuous'` to record until the timeline loops back to start.
7. **Resolution matches delivery spec.** Confirm with the user before starting; re-encoding to fix resolution mismatches is lossy.

## Render-Then-Encode Pipeline

For long renders or expensive shaders, decouple the render from the delivery codec:

1. **Render pass:** moviefileoutTOP → MJPEG or ProRes (fast write, large file).
2. **Encode pass:** outside TD, run `ffmpeg` on the intermediate to produce the delivery codec.

```bash
ffmpeg -i intermediate.mov -c:v libx264 -crf 18 -preset slow delivery.mp4
```

This avoids dropping frames when the encoder can't keep up with the cook rate. Non-Commercial users can use this path to get H.264 delivery files (TD writes ProRes/MJPEG, ffmpeg transcodes outside TD's license boundary).

## Common Failure Modes

| Symptom | Likely cause |
|---|---|
| File is 0 bytes after recording | Record never started; `record` pulse fired but no frames cooked. Check FPS > 0. |
| File is one frame repeated | Source TOP not cooking; `cooktype` may be `'selective'` and not requested. Wire a Null TOP that's always visible. |
| File is correct duration but wrong resolution | License cap (Non-Commercial 1280×1280) or output TOP at unexpected resolution. Verify with `td_get_params` on the moviefileoutTOP. |
| Audio is desynced | Audio chain has a Delay CHOP or Filter CHOP introducing latency. Route raw audio direct to moviefileoutTOP's audio input. |
| File won't open in player | Codec mismatch with the container, or interrupted write (TD crashed mid-record). Re-encode with ffmpeg. |

## TDPilot Tool Pairings

- `td_get_state_vector` — single call gives project FPS, health, snapshots, and timeline state before record.
- `td_screenshot(path=<source_top>)` — preview verification.
- `td_set_params({"file": "<absolute_path>"})` on the moviefileoutTOP — set output before pulsing record.
- `td_pulse_param(path=<moviefileoutTOP>, paramName="record")` — start record from the agent side.
- `td_get_info` — check license tier (`Pro` / `Commercial` / `Non-Commercial`) before promising specific codecs or resolutions.
