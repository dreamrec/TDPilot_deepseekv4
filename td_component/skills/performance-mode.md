---
name: performance-mode
description: Optimization discipline — profile before optimizing, avoid common slowdowns
auto_load: false
priority: 5
triggers: [optimize, slow, lag, fps, performance, profile]
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
| FPS drops linearly with viewer size | High-res TOPs cooking unnecessarily | Drop resolution or use `levelTOP.par.cookpulsewhennotviewed=False` |
| Spikes when interacting with UI | Panel COMPs cooking on every mouse event | Audit `panelExecuteDAT` callbacks for heavy ops |
| Slow project load | Many ops cooking at startup | Set `cookpulsewhennotviewed=True` on non-essential ops |
| Audio chops drop samples | Audio chain stalls on cook | Add a `nullCHOP` with cookalways at the end of audio chains |

## Optimization checklist

After profiling shows a hot path:

- [ ] **Resolution** — Are TOPs cooking at higher res than they need? Use
      `crop` or `resolution` settings to right-size.
- [ ] **Pull-cooking** — Ops that aren't displayed shouldn't cook every
      frame. Set `cookpulsewhennotviewed=False` on intermediate ops.
- [ ] **GPU vs CPU** — Heavy SOPs on the CPU? Move to POPs (GPU) where
      possible.
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
3. `td_screenshot` to confirm visual quality unchanged.
