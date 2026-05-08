---
name: performance-mode
description: Optimization discipline — profile before optimizing, inspect params before setting, avoid common slowdowns
auto_load: false
priority: 5
triggers: [optimize, slow, lag, fps, performance, profile]
surface: standalone
---

# Performance Mode

Activate when the user reports lag, slowdowns, low FPS, or asks to optimize.

## Diagnose before optimizing

ALWAYS profile first. Don't guess where the bottleneck is.

1. **Profile cook times** via `td_cooking_info({path: '/project1', recurse: true,
   sort_by: 'cookTime', limit: 20})`. The top entries are your bottlenecks.

2. **Read TD's perform-mode CHOPs** — `op('/perform/perform').par.cookrate`
   shows live FPS. Aim for >= 60 unless the user spec'd lower.

3. **Check thread blocking** — heavy `td_exec_python` blocks the cook
   thread. If a single op shows high cookTime AND it's a script DAT,
   the script is the cost.

## Common slowdown patterns

| Symptom | Likely cause | Fix |
|---|---|---|
| FPS drops linearly with viewer size | High-res TOPs cooking unnecessarily | Drop the TOP's resolution; if it's a TOP family that exposes a "cook only when viewed" param, set it (see "Inspect first" below). |
| Spikes when interacting with UI | Panel COMPs cooking on every mouse event | Audit `panelExecuteDAT` callbacks for heavy ops |
| Slow project load | Many ops cooking at startup | Identify the top-N cooking ops via `td_cooking_info`, then check each one's parameters for an "always cook" / "viewer cooks" toggle. |
| Audio chops drop samples | Audio chain stalls on cook | Add a `nullCHOP` with `cookalways=True` at the end of audio chains |

## Inspect first — don't memorise parameter names

Operator families have **different cook controls**: TOPs have one set,
CHOPs another, SOPs/POPs a third. The exact parameter names also
shift across TD builds — pre-1.7.2 this skill cargo-culted
`cookpulsewhennotviewed` as a universal recipe, which only exists on
some TOPs and was misleading on CHOP perf issues.

Always inspect first:

1. `td_get_params({path: '<hot op>', page: 'Common'})` — TD's
   universal "Common" parameter page is where the cook-related
   controls live (when they exist). Look for: `cookalways`,
   `cookpulsewhennotviewed`, `cookrate`, `cookpulsemode`.
2. If the param doesn't exist on this op type, check the
   family-specific page via `td_get_params` without the page filter.
3. Reach for `td_get_operator_doc({op_type: '<type>'})` if you're
   not sure which knobs are available.

Setting a parameter that doesn't exist returns silently in some TD
builds — don't assume your fix took effect. Re-read the param
afterward to confirm.

## Optimization checklist

After profiling shows a hot path:

- [ ] **Resolution** — Are TOPs cooking at higher res than they need? Use
      `crop` or `resolution` settings to right-size.
- [ ] **Pull-cooking** — Ops that aren't displayed shouldn't cook every
      frame. Inspect for `cookpulsewhennotviewed` (or the family's
      equivalent) before assuming the param exists.
- [ ] **GPU vs CPU** — Heavy SOPs on the CPU? Move to POPs (GPU) where
      possible. **Note (1.7.0+)**: 2D-input Polygonize SOP/POP migrations
      now go through `tracePOP` + `triangulatePOP` instead.
- [ ] **Cache** — Static intermediate data? Use `cacheTOP` /
      `cacheCHOP` to freeze the cooked result.
- [ ] **Cook order** — Are critical ops competing for thread time?
      Use `Wires/Trail/Cook` settings to control ordering.

## What NOT to do

- DON'T cargo-cult "optimization" tweaks without profile data. Wrong
  optimizations make things worse.
- DON'T disable cooking on visible ops to save FPS — the user will see
  them freeze.
- DON'T add cacheTOPs to dynamic chains — caches stale values.

## Verification

After applying optimizations:
1. Re-profile via `td_cooking_info` and confirm the hot path moved.
2. Check FPS via `op('/perform/perform').par.cookrate`.
3. `td_screenshot` to confirm visual quality unchanged. **Caveat**:
   `td_screenshot` itself triggers a cook on the screenshotted TOP —
   don't lean on it during perf-debugging unless you genuinely need
   visual confirmation, otherwise you're measuring with the
   measurement tool poking the system.
