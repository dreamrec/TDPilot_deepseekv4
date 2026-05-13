# Feedback loop from scratch — the canonical `src → fb → over` trail pattern, wired correctly the first time

**Prerequisites:** TouchDesigner 2025.30000+, TDPilot v2.4.0+ (standalone .tox or Claude Code plugin).

**Setup:** Empty `.toe` project. Nothing else needed.

## The prompts

Paste these into the chat, one at a time. Wait for each turn to finish before sending the next.

1. `Build the canonical feedback recipe from SKILL.md §11 in /project1. Nodes: a noiseTOP as the source (call it 'src'), a feedbackTOP ('fb'), a levelTOP ('level'), an overTOP ('over'), and a nullTOP ('out') as the terminator. Don't wire fb's output back to its own input — use the par.top reference parameter instead.`
2. `Wire it: src trifurcates into fb input 0 (the seed), over input 0 (the BG — fresh frame, NOT feedback's output), and a dry path we can ignore for now. Then fb → level → over input 1 (the OVERLAY). over → out.`
3. `Set fb.par.top = op('over') — the OP reference, NOT the path string. This is what closes the loop without a visible wire. Use td_exec_python for the reference assignment.`
4. `On level: set opacity=0.9 on the Post page (this is the trail decay, NOT brightness1), brightness1=1.0.`
5. `On over: set size='input1' so the output sizes from the overlay (level) input.`
6. `Now make src move so we actually see a trail. Set noiseTOP transform tx with expression "absTime.seconds * 0.3", mode ParMode.EXPRESSION. Don't forget to set .mode — .expr alone sits dormant.`
7. `Set display=True on the 'out' nullTOP and viewer=True on the parent. Screenshot 'out' and check the trail is visible. If you see "Not enough sources specified" on fb, screenshot first — that's a static-analyzer warning, not a runtime failure.`
8. `Save this as a technique: title "Canonical feedback trail", tags ["feedback","trail","canonical"].`

## Expected tool sequence

The agent should call these tools in roughly this order (variations are fine — what matters is the rough shape):

- `td_get_hints` — preflight on feedbackTOP (the par.top trick is a known quirk)
- `td_snapshot_scene` — safety net
- `td_project_lifecycle` (`start_undo_block`)
- `td_create_node` xN — noiseTOP (src), feedbackTOP (fb), levelTOP, overTOP, nullTOP (out)
- `td_connect_nodes` — src to fb/over input 0, fb→level→over input 1, over→out
- `td_exec_python` — `op('/project1/fb').par.top = op('/project1/over')` (OP ref, not string)
- `td_set_params` — `opacity=0.9` on level (Post page), `brightness1=1.0`, `size='input1'` on over
- `td_exec_python` — `op('/project1/src').par.tx.expr = "absTime.seconds * 0.3"`; `op('/project1/src').par.tx.mode = ParMode.EXPRESSION`
- `td_exec_python` — `op('/project1/out').viewer = True` and display flag
- `td_get_errors` (recursive) — confirm clean, ignore the cycle warning if output renders
- `td_screenshot` — visual verify the trail
- `td_project_lifecycle` (`end_undo_block`)
- `td_memory_save`

## The result

A drifting noise source leaves a soft tail behind it. The `out` nullTOP shows a 1280×720 image with the source visible in front and decaying repeats trailing across the frame. The trail length is governed by `level.opacity` — at 0.9 it stretches a long way; drop to 0.7 and trails shorten dramatically. Critical: `out` has display flag ON, not `fb` — display-flagging the feedback node itself will pin its viewer to the seed frame and you'll never see the trail.

<!-- TODO: screenshot -->

## Variations to try

- `Change level.opacity to 0.97 to push trails toward infinite-decay, then to 0.6 for snappy short trails.`
- `Add a blurTOP between fb and level — softer trails, like ink in water.`
- `Replace noiseTOP src with a circleTOP whose tx/ty are sin/cos of absTime — orbital trail. Then increase the orbit radius and watch the trail wrap.`
