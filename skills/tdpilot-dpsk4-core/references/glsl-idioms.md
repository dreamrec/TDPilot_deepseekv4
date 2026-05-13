# GLSL TOP — TouchDesigner Idioms

GLSL in TD differs from raw OpenGL or web shadertoys in three ways that bite every newcomer: time, inputs, and feedback wiring. Get these right and every shader becomes easier.

## Time

**There is no `uTDCurrentTime` uniform.** That name is from an older API and isn't available in current GLSL TOP shaders.

Wire time via the Values page instead:

1. Open the GLSL TOP's Values page in the parameters.
2. Set `value0name = "uTime"`.
3. Set `value0` to an expression: `absTime.seconds`.
4. Set `value0.mode = ParMode.EXPRESSION` (otherwise the expression sits dormant and the parameter uses its raw `.val`).
5. In the shader, read it as a normal uniform: `uniform float uTime;`.

For frame-locked animation use `absTime.frame` instead of `absTime.seconds`. For BPM-synced time, expose a beat phase from `td_get_timescale_state` via a custom parameter.

## Inputs

GLSL TOP exposes connected TOP inputs as `sTD2DInputs[N]` (or `sTD3DInputs`, `sTDCubeInputs` depending on type):

```glsl
uniform sampler2D sTD2DInputs[2];

void main() {
    vec4 a = texture(sTD2DInputs[0], vUV.st);
    vec4 b = texture(sTD2DInputs[1], vUV.st);
    fragColor = mix(a, b, 0.5);
}
```

If the shader fails to find inputs, check that TD knows how many to bind. Either:

- Set the input count explicitly on the GLSL TOP's Vectors page, OR
- Add `#define TD_NUM_2D_INPUTS 2` at the top of the shader (TD substitutes this before compile).

## Resolution and UV

TD provides `vUV.st` for the standard 0..1 UV. For pixel-space access:

```glsl
uniform vec2 uTDOutputInfo_res;  // (width, height) of this TOP
vec2 pixel = vUV.st * uTDOutputInfo_res;
```

For aspect-corrected centered UV (useful for circles, radial effects):

```glsl
vec2 uv = vUV.st * 2.0 - 1.0;        // -1..1
uv.x *= uTDOutputInfo_res.x / uTDOutputInfo_res.y;  // aspect correct
```

## Feedback TOP — Use the `top` Parameter

The Feedback TOP does NOT take feedback by wiring its output back to its input. That cycles cooks. The correct pattern uses the `top` reference parameter to close the loop without a visible wire:

```
src → over → level → feedback1   (Feedback TOP, terminal node)
              ↑
              └── feedback1.par.top = over   (closes the loop via parameter ref)
```

Set `feedback1.par.top = op('over')` — an OP reference parameter assignment — and place the Feedback TOP **after** the compositor in the visible signal flow.

The "Not enough sources specified" warning that may appear on first cook is benign — it's a TD static-analyzer artifact of the cyclic reference, not a runtime failure. After the first cook completes, output stabilizes. **Do not trust this warning as a failure signal — screenshot the output before assuming the chain is broken.**

See `tdpilot-dpsk4-core` SKILL.md §11 for the verified node-by-node feedback recipe (`src ┬ fb / over / dryWetMix`), including the exact opacity / clamp / blur values that produce stable trails.

## Common Compile Errors

| Symptom | Likely cause |
|---|---|
| `unknown identifier sTD2DInputs` | Input count not declared; set Vectors page count or `#define TD_NUM_2D_INPUTS N` |
| `texture undefined` | Using `texture2D` (legacy GLSL ≤120). TD uses GLSL 330+; use `texture(sampler, uv)` |
| Output is solid black, no compile error | Output format mismatch — set the GLSL TOP's pixel format to match what `fragColor` writes (rgba8 vs rgba32float). Common when shader writes `vec4(0.5)` but output is 8-bit and gamma-converted to ~0.18. |
| Output is flat color where it should animate | `.par.value0.mode` is not `ParMode.EXPRESSION`; the `absTime.seconds` expression isn't evaluating |
| Shader compiles but pixels are NaN/black streaks | Division by zero somewhere; common with `normalize(vec3(0))` or `pow(neg, 0.5)`. Add `max(x, 0.0001)` guards. |

## Multi-Input Patterns

### Two-input compositor

```glsl
uniform sampler2D sTD2DInputs[2];
uniform float uMix;

void main() {
    vec4 a = texture(sTD2DInputs[0], vUV.st);
    vec4 b = texture(sTD2DInputs[1], vUV.st);
    fragColor = mix(a, b, uMix);
}
```

### Texture-driven displacement

```glsl
uniform sampler2D sTD2DInputs[2];  // [0]=source, [1]=displacement
uniform float uAmount;

void main() {
    vec2 disp = texture(sTD2DInputs[1], vUV.st).rg * 2.0 - 1.0;
    vec2 uv = vUV.st + disp * uAmount;
    fragColor = texture(sTD2DInputs[0], uv);
}
```

### Audio-spectrum sampling

See `audio-reactive-glsl.md` for the empirically verified chain and the `y = 0.25` sampling row.

## TDPilot Tool Pairings

- `td_get_param_help(path=<glsl_top>, paramName="value0name")` — confirm Values-page param names before writing expressions.
- `td_set_params({"value0name": "uTime"})` — declarative declaration of input uniform names.
- `td_exec_python` with `op(path).par.value0.expr = "absTime.seconds"; op(path).par.value0.mode = ParMode.EXPRESSION` — when the expression needs to be set programmatically and `td_set_params` doesn't expose `.mode` directly.
- After any shader edit, `td_get_errors(path=<glsl_top>, recursive=false)` will surface compile errors immediately.
- `td_set_content` for editing the shader text in a docked DAT (preferred over inline string-rewriting through `td_exec_python`).

## Performance Notes

- GLSL TOPs cook every frame their output is requested. Add a Null TOP downstream and only request it when needed (selected, exported, or wired into a renderer).
- For static content, set the GLSL TOP's `cookrate` to a low value (e.g., 1 Hz) or `cooktype = 'selective'` to avoid burning GPU on a still image.
- Multi-pass shaders (5+ chained GLSL TOPs) can saturate even high-end GPUs at 1080p; if performance drops, profile with `td_cooking_info` to find the bottleneck pass.
