# GLSL shader driven by mouse — Values-page `value0`/`value1` uniforms, the right way

**Prerequisites:** TouchDesigner 2025.30000+, TDPilot v2.4.0+ (standalone .tox or Claude Code plugin).

**Setup:** Empty `.toe` project. Mouse cursor available in the TD window (you'll be moving it over the network to drive the shader).

## The prompts

Paste these into the chat, one at a time. Wait for each turn to finish before sending the next.

1. `Build a mouse-driven GLSL shader in /project1. Chain: mouseInCHOP → nullCHOP → constantCHOP that exposes tx and ty as named channels, then bind those into the glslTOP's Values page. Don't use uTDCurrentTime — that uniform doesn't exist in current GLSL TOP. Use the Values page for everything.`
2. `On the glslTOP, set up two Values-page uniforms: value0name='uMouseX', value0 bound to op('null1')['tx'] with mode ParMode.EXPRESSION; value1name='uMouseY', value1 to op('null1')['ty'] with mode ParMode.EXPRESSION. Confirm with td_get_param_help that those param names are right before writing.`
3. `Write the fragment shader into a docked textDAT using td_set_content. Uniforms: uniform float uMouseX, uMouseY. Output: a radial gradient centered at (uMouseX, uMouseY) in normalized 0..1 UV space, with vUV.st as the base UV. Use #define TD_NUM_2D_INPUTS 0 since we don't need texture inputs. Or set the input count on the Vectors page.`
4. `Wire the textDAT into the glslTOP's pixelshader parameter and run td_get_errors recursive on the glslTOP. If it says 'unknown identifier sTD2DInputs', the define is missing or the input count is unset on Vectors page.`
5. `Set the glslTOP outputresolution=custom, output resolution to (1280, 720). Set the pixel format to rgba8 for now — we don't need float precision for a gradient.`
6. `Add an absTime.seconds-driven third uniform value2name='uTime' so we can pulse the gradient. Don't forget value2.mode = ParMode.EXPRESSION — .expr alone sits dormant.`
7. `Modulate the gradient radius by sin(uTime * 2.0) in the shader. Set viewer=True on the glslTOP and screenshot it. Move your mouse around the network and screenshot again — the gradient center should follow.`

## Expected tool sequence

The agent should call these tools in roughly this order (variations are fine — what matters is the rough shape):

- `td_get_hints` — preflight on glslTOP (Values-page conventions, input count)
- `td_snapshot_scene`
- `td_project_lifecycle` (`start_undo_block`)
- `td_create_node` xN — mouseInCHOP, nullCHOP, glslTOP, textDAT (for the shader)
- `td_connect_nodes` — mouseInCHOP → null1; textDAT → glslTOP.pixelshader
- `td_get_param_help` — confirm `value0name`, `value0`, `value0.mode` are valid on glslTOP
- `td_set_params` on glslTOP — `value0name="uMouseX"`, `value1name="uMouseY"`, `value2name="uTime"`, `outputresolution="custom"`, resolution (1280, 720)
- `td_exec_python` — bind the expressions and modes:
  - `op('/project1/glsl1').par.value0.expr = "op('null1')['tx']"`
  - `op('/project1/glsl1').par.value0.mode = ParMode.EXPRESSION`
  - (repeat for value1, value2)
- `td_set_content` — write the fragment shader into the textDAT
- `td_get_errors` (recursive) — confirm clean compile
- `td_screenshot` — visual verify before and after mouse move
- `td_project_lifecycle` (`end_undo_block`)

## The result

A radial gradient TOP whose center tracks the mouse cursor in real time, with a slow breathing pulse on the radius from the time uniform. Moving the cursor across the network window moves the gradient bright spot to match. Critical: if you only set `.expr` without `.mode`, the gradient pins at (0,0) and never moves — the dormant-expression bug from anti-patterns.md.

<!-- TODO: screenshot -->

## Variations to try

- `Add value3name='uClick' bound to mouseInCHOP's left button channel — pulse the gradient brightness on click.`
- `Drive the gradient color from a hue cycle: add a uniform vec3 uColor and bind value4/value5/value6 to an HSV-to-RGB expression on absTime.seconds.`
- `Swap the radial gradient for a Voronoi cell shader where uMouseX/uMouseY shift the cell density. Watch the cells reshape under the cursor.`
