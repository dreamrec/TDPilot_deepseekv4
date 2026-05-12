# Audio-Reactive GLSL — Signal Chain & Spectrum Sampling

A field-tested chain for driving GLSL shaders from live audio spectrum data, with the magic numbers that make it actually work. Most "why is my shader not reacting to audio" sessions trace to one of the empirically verified rules below.

## Signal Chain

```
AudioFileIn CHOP
  → AudioSpectrum CHOP   (FFT=512, outputmenu='setmanually', outlength=256, timeslice=ON)
  → Math CHOP            (gain=10)
  → CHOP-to-TOP          (dataformat='r', layout='rowscropped')
  → GLSL TOP   input 1

Constant TOP (rgba32float, channel=time) → GLSL TOP input 0
GLSL TOP → Null TOP → MovieFileOut
```

## Empirically Verified Rules

These look small. Each one wastes hours to discover the hard way.

1. **`timeslice = ON` on AudioSpectrum CHOP.** With it OFF, the operator processes the entire audio history each cook — sample count explodes and the spectrum stops updating in real time.

2. **`outputmenu = 'setmanually'`, `outlength = 256`.** Auto-length picks values that don't divide cleanly into the GLSL UV space. 256 samples gives a clean `1/256 = 0.00390625` step that maps naturally to texture coordinates.

3. **DO NOT use Lag CHOP for spectrum smoothing.** Lag CHOP operates in timeslice mode and expands the 256-sample spectrum into 2400+ samples per cook. The CHOP-to-TOP downstream becomes a wall of stale data. **Smooth inside the GLSL shader using a feedback texture instead** — keep the CHOP path raw.

4. **DO NOT use Filter CHOP either.** Same timeslice-expansion problem.

5. **`Math CHOP gain = 10`, not 5.** Raw spectrum magnitudes in the bass range hover around 0.19. Gain 5 leaves the shader starved; gain 10 puts bass at ~1.9, which clamps cleanly to 1.0 in the shader.

6. **CHOP-to-TOP: `dataformat = 'r'`, `layout = 'rowscropped'`.** This packs the 256-channel CHOP into a 256×1 single-channel TOP. Other layouts pack multi-channel data in ways that misalign the shader's UV lookup.

7. **Sample at `y = 0.25`, not `y = 0.0` or `y = 0.5`.** `rowscropped` lays the data in a band offset from the texture top edge; 0.25 hits the data row reliably across resolutions.

## GLSL Spectrum Sampling

```glsl
// input 0: time texture (rgba32float, single pixel constant TOP)
// input 1: spectrum texture (256×1 single-channel)

float iTime = texture(sTD2DInputs[0], vec2(0.5)).r;

float bass = (
    texture(sTD2DInputs[1], vec2(0.02, 0.25)).r +
    texture(sTD2DInputs[1], vec2(0.05, 0.25)).r
) / 2.0;

float mid  = texture(sTD2DInputs[1], vec2(0.30, 0.25)).r;
float high = texture(sTD2DInputs[1], vec2(0.85, 0.25)).r;
```

## Why Each Band Lives Where It Lives

- **x ∈ [0.00, 0.10]** — bass / kick energy. Two-tap average smooths the lowest bin's noise.
- **x ∈ [0.10, 0.45]** — body / midrange. Most music's perceived loudness lives here.
- **x ∈ [0.45, 0.95]** — air / cymbals / sibilance. Watch for hiss when no music is playing.
- **x ≥ 0.95** — Nyquist artifacts; avoid.

## In-Shader Smoothing (Replace Lag CHOP)

Since Lag/Filter CHOPs break the timeslice contract, smooth on the GPU instead. Add a third input to the GLSL TOP feeding back its previous frame (see `glsl-idioms.md` for the Feedback TOP wiring):

```glsl
// input 2: previous frame of this shader (via Feedback TOP)
float prevBass = texture(sTD2DInputs[2], vUV.st).r;
float currentBass = (
    texture(sTD2DInputs[1], vec2(0.02, 0.25)).r +
    texture(sTD2DInputs[1], vec2(0.05, 0.25)).r
) / 2.0;
float smoothedBass = mix(prevBass, currentBass, 0.15); // 0.15 = attack rate
```

The `mix` factor controls smoothing: lower = slower / more inertia; higher = snappier.

## Connecting to TDPilot Workflows

- Use `td_create_node` for each operator, then `td_connect_nodes` for the chain. Don't try to build wiring inside `td_exec_python` — it bypasses validation.
- After building, `td_get_errors(path="<containing_comp>", recursive=true)`.
- `td_screenshot(path="<glsl_top_path>")` to confirm non-black output before recording.
- If the GLSL TOP shows `uniform sTD2DInputs not found`, the GLSL needs `#define TD_NUM_2D_INPUTS 3` (or whatever input count) at the top of the shader, or set the input count explicitly on the GLSL TOP's Vectors page.

## Verification Pattern

After building the chain, before claiming it works:

1. `td_screenshot` the spectrum CHOP-to-TOP — should show a horizontal stripe of varying brightness, NOT a uniform gray.
2. `td_screenshot` the GLSL TOP — should change visibly when you change audio.
3. If neither moves: check `td_get_errors` for `tdAttributeError` on the AudioSpectrum CHOP (most likely cause — a param name typo).
4. If only the GLSL TOP is static: the spectrum is reaching the TOP but the shader isn't sampling correctly. Re-verify `y = 0.25` in the shader's `texture()` call.
