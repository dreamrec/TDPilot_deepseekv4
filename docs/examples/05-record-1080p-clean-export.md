# Record a clean 1080p export — pre-flight checklist before you press record

**Prerequisites:** TouchDesigner 2025.30000+, TDPilot v2.4.0+ (standalone .tox or Claude Code plugin). For 1920×1080 output: a **Commercial** TouchDesigner license. Non-Commercial clamps to 1280×1280.

**Setup:** A `.toe` project with an existing animated source — any glslTOP, render chain, or feedback network terminating at a nullTOP. If you don't have one, build Example 02 (feedback loop) first and use its `out` nullTOP as the source.

## The prompts

Paste these into the chat, one at a time. Wait for each turn to finish before sending the next.

1. `Pre-flight: call td_get_info and report the license tier. If Non-Commercial, warn me — 1920×1080 is going to get clamped to 1280×720, and H.264 is unavailable. On Non-Commercial we'll record ProRes (mac) or MJPEG and ffmpeg-transcode afterward.`
2. `Now the pre-recording checklist from references/recording-and-export.md. Run td_tool_batch: td_get_state_vector (verify FPS > 0), td_screenshot on /project1/out (the source TOP — must be non-black), td_cooking_info on /project1 (no oscillating cook times). Report a green/red status for each check.`
3. `Create a moviefileoutTOP at /project1/recorder. Set: file='<absolute path with .mov extension>', fps=60, outputresolution='custom' with explicit (1920, 1080) — or (1280, 720) on Non-Commercial. Codec: ProRes if on Mac, otherwise MJPEG (mjpa). Don't pick H.264 on Non-Commercial.`
4. `Lock the project cook rate to exactly 60 Hz: set the timeline rate parameter, confirm with td_get_state_vector. A drifting cook rate produces a drifting movie.`
5. `Wire the source TOP into the moviefileoutTOP's input 0 (video). If audio is in play, route the audio chain into the moviefileoutTOP's audio input directly — no Delay or Filter CHOPs in between, those introduce desync.`
6. `Confirm os.access on dirname(file) returns writable via td_exec_python before starting. Don't trust the previous record's path.`
7. `Pulse the moviefileoutTOP's record parameter with td_pulse_param. Then wait ~5 seconds and screenshot the moviefileoutTOP — its viewer should show the most recent written frame, not black.`
8. `After ~30 seconds of recording, pulse record again to stop. Verify the file size on disk is > a few MB and the frame count matches the duration × fps roughly. If file is 0 bytes, FPS was 0 or the source TOP wasn't cooking.`
9. `If on Non-Commercial and the user wants H.264 delivery: build the ffmpeg command — ffmpeg -i <intermediate.mov> -c:v libx264 -crf 18 -preset slow <delivery.mp4> — and print it. Don't run ffmpeg from inside TD.`

## Expected tool sequence

The agent should call these tools in roughly this order (variations are fine — what matters is the rough shape):

- `td_get_info` — license tier first, gates every downstream choice
- `td_tool_batch` wrapping:
  - `td_get_state_vector` — FPS, snapshots, health
  - `td_screenshot` on the source TOP — non-black verify
  - `td_cooking_info` — stable cook times
- `td_create_node` — moviefileoutTOP at `/project1/recorder`
- `td_set_params` — `file`, `fps=60`, `outputresolution="custom"`, resolution `(1920, 1080)` or `(1280, 720)`, codec choice
- `td_exec_python` — `os.access(os.path.dirname(path), os.W_OK)` writability check
- `td_connect_nodes` — source TOP → recorder input 0 (and audio source → recorder audio input if needed)
- `td_pulse_param` — `record` pulse to start
- `td_screenshot` on the recorder — verify it's writing real frames
- `td_pulse_param` — `record` pulse to stop
- `td_exec_python` — `os.path.getsize(path)` sanity check

## The result

A `.mov` file on disk, the duration the user expected, the resolution the user expected, at exactly 60 fps with no dropped frames. On Mac+Commercial: a 1920×1080 ProRes 422 file at ~700 MB/min. On Non-Commercial: a 1280×720 ProRes or MJPEG intermediate ready for ffmpeg transcoding. Critical: file size > a few MB (not 0 bytes), the player opens it without "unknown codec", and frame 0 is not the same as frame N (use any video player's scrub bar to confirm the animation actually moves).

<!-- TODO: screenshot -->

## Variations to try

- `Record 30 seconds at 24 fps for cinematic delivery — set fps=24, lock cook rate, re-pulse.`
- `Record with spherical metadata for VR delivery: set the spherical layout metadata on the moviefileoutTOP (TD 2025.32820+).`
- `Render-then-encode: write MJPEG at 60fps, then build the ffmpeg command for H.265 (HEVC) at CRF 22 for a 10x smaller delivery file.`
