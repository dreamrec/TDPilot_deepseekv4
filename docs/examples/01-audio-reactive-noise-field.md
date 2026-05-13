# Audio-reactive noise field — kick drum drives a Noise TOP, end-to-end in one chat

**Prerequisites:** TouchDesigner 2025.30000+, TDPilot v2.4.0+ (standalone .tox or Claude Code plugin).

**Setup:** Empty `.toe` project. A music file ready to hand to the agent (any `.wav` / `.aif` / `.mp3` on disk — bass-heavy material is easiest to verify against). Or use TD's default audioDeviceIn for live input.

## The prompts

Paste these into the chat, one at a time. Wait for each turn to finish before sending the next.

1. `Build an audio-reactive chain from scratch in /project1. Audio file: <absolute path to a .wav>. Use audiofileinCHOP, then the empirically verified spectrum chain (audiospectrumCHOP timeslice=ON, outputmenu=setmanually, outlength=256, then a mathCHOP gain=10, then a chopToTOP dataformat=r layout=rowscropped). Don't use lagCHOP or filterCHOP anywhere — they break timeslice on a 256-sample spectrum.`
2. `Add a noiseTOP after the chopToTOP for now just so the canvas has something. Don't wire the spectrum to it yet — I want to look at the bare CHOP-to-TOP first.`
3. `Screenshot the chopToTOP. Should show a horizontal stripe of varying brightness, not flat gray.`
4. `Now wire the spectrum into the noise field. Make a glslTOP downstream that samples the spectrum texture at y=0.25 — bass at x=0.02 and x=0.05 averaged, mids at x=0.30, highs at x=0.85 — and uses the bass value to push the noiseTOP's amp parameter. The noiseTOP should be input 0 of the glslTOP, spectrum CHOP-to-TOP input 1.`
5. `Add a constantTOP rgba32float feeding the glslTOP as a time source (input would push input 1 and 2 down — restructure so time is input 0, noise is input 1, spectrum is input 2). Set value0name=uTime, value0=absTime.seconds, mode=ParMode.EXPRESSION.`
6. `Run td_get_errors recursive on the parent and screenshot the glslTOP. If the shader is dead, show me the compile error.`
7. `Smooth the bass in-shader using a feedbackTOP back-reference — attack rate 0.15. Don't try to use a lagCHOP for this.`
8. `Save this as a technique with td_memory_save: title "Audio-reactive noise field", tags ["audio","glsl","spectrum"].`

## Expected tool sequence

The agent should call these tools in roughly this order (variations are fine — what matters is the rough shape):

- `td_get_hints` — preflight on audiospectrumCHOP and glslTOP for known quirks
- `td_get_info` — check TD build + license tier
- `td_snapshot_scene` — safety net before building
- `td_project_lifecycle` (`start_undo_block`) — group the build as one undo
- `td_create_node` xN — audiofileinCHOP, audiospectrumCHOP, mathCHOP, chopToTOP, noiseTOP, glslTOP, constantTOP, feedbackTOP, nullTOP
- `td_set_params` — `timeslice=True`, `outputmenu='setmanually'`, `outlength=256` on audiospectrum; `gain=10` on math; `dataformat='r'`, `layout='rowscropped'` on chopToTOP
- `td_connect_nodes` — chain assembly
- `td_set_content` — write the GLSL fragment shader with the y=0.25 sampling and feedback smoothing
- `td_exec_python` — set `value0name="uTime"`, `value0.expr="absTime.seconds"`, `value0.mode=ParMode.EXPRESSION` on the glslTOP
- `td_get_errors` (recursive) — confirm clean compile
- `td_screenshot` — visual verify (chopToTOP first, then glslTOP)
- `td_project_lifecycle` (`end_undo_block`)
- `td_memory_save` — persist the recipe

## The result

A glslTOP in `/project1` whose brightness pulses with the bass of the input audio. The chopToTOP shows a horizontal spectrum stripe. With the feedback smoothing in place the pulse has visible inertia — kicks blow it up, the decay relaxes over ~7 frames. No "Not enough sources specified" warnings should be acted on without first screenshotting.

<!-- TODO: screenshot -->

## Variations to try

- `Add a mid-band sample at x=0.30 and use it to modulate noiseTOP.period. Higher mids = finer detail.`
- `Drive a hueTOP from the high band (x=0.85) so sibilance shifts the color.`
- `Swap the noiseTOP for a 4D Simplex noise (TD 2025.32820+) and read the derivatives instead of finite-differencing downstream.`
