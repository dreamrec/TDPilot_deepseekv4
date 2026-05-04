---
name: TouchDesigner Operator Families
description: TOP/CHOP/SOP/DAT/MAT/COMP family overview with common op types and naming rules
category: reference
---

# Operator Families

TouchDesigner organises every operator into one of six families. The
family suffix is part of the op type name (case-sensitive, camelCase)
and you MUST include it when calling `td_create_node`.

| Family | Suffix | What it carries | Cook trigger |
|---|---|---|---|
| **TOP** | TOP | Texture / image data on the GPU | Frame ticks if displayed or pulled |
| **CHOP** | CHOP | Channel / sample / time-series numbers | Time-dependent inputs cook every frame |
| **SOP** | SOP | Geometry (points, primitives) on the CPU | Pulled by viewer or downstream |
| **DAT** | DAT | Text or table data (scripts, configs, JSON) | Pull-cooked; explicit `cook(force=True)` for scripts |
| **MAT** | MAT | Materials applied to SOPs | Cook with the SOP that references them |
| **COMP** | COMP | Components: containers, geometry, panels, cameras, lights | Cook when displayed or referenced |

## Common op types per family

### TOP (textures)
- `noiseTOP`, `levelTOP`, `transformTOP`, `compositeTOP`, `feedbackTOP`
- `moviefileinTOP` (lowercase between 'movie' and 'TOP')
- `videodeviceinTOP` (lowercase between 'video' and 'TOP')
- `renderTOP`, `nullTOP`, `outTOP`, `inTOP`
- `glslTOP`, `glslmultiTOP`, `mathTOP`, `lookupTOP`
- `chopToTOP` (chopto), `topToCHOP` (topto)

### CHOP (channels / samples)
- `constantCHOP`, `lfoCHOP`, `noiseCHOP`, `mathCHOP`, `selectCHOP`
- `audiofileinCHOP`, `audiodeviceinCHOP`, `audiodeviceoutCHOP`
- `analyzeCHOP`, `audiospectrumCHOP`, `audiobandeqCHOP`
- `keyframeCHOP`, `lagCHOP`, `filterCHOP`, `trailCHOP`
- `nullCHOP`, `oscinCHOP`, `oscoutCHOP`, `midiinCHOP`, `midioutCHOP`
- `timerCHOP`, `beatCHOP`, `mergeCHOP`, `shuffleCHOP`

### SOP (geometry)
- `boxSOP`, `sphereSOP`, `gridSOP`, `circleSOP`, `lineSOP`, `tubeSOP`
- `noiseSOP`, `transformSOP`, `mergeSOP`, `nullSOP`, `outSOP`
- `copySOP`, `dividedSOP`, `extrudeSOP`, `subdivideSOP`
- `magnetSOP`, `lsystemSOP`, `metaballSOP`, `pointgenSOP`

### DAT (data)
- `textDAT`, `tableDAT`, `executeDAT`, `parameterexecuteDAT`,
  `chopexecuteDAT`, `datexecuteDAT`, `panelexecuteDAT`
- `webserverDAT`, `webclientDAT`, `oscinDAT`, `oscoutDAT`,
  `udpinDAT`, `udpoutDAT`, `tcpipDAT`, `serialDAT`
- `evaluateDAT`, `mergeDAT`, `nullDAT`, `selectDAT`, `sortDAT`,
  `transposeDAT`, `convertDAT`, `substituteDAT`

### MAT (materials)
- `phongMAT`, `pbrMAT`, `glslMAT`, `constantMAT`, `wireframeMAT`
- `lineMAT`, `pointMAT`, `nullMAT`

### COMP (components)
- `geometryCOMP`, `cameraCOMP`, `lightCOMP`
- `baseCOMP`, `containerCOMP`, `panelCOMP`
- `windowCOMP`, `replicatorCOMP`, `selectCOMP`
- `parameterCOMP`, `actorCOMP`
- Panel widgets inside containerCOMP: `buttonCOMP`, `fieldCOMP`,
  `sliderCOMP`, `tableCOMP`, `listCOMP`

## Family-detection helpers (Python in `td_exec_python`)

```python
node = op('/project1/something')
node.isTOP   # bool
node.isCHOP
node.isSOP
node.isDAT
node.isMAT
node.isCOMP
node.isPOP   # POPs are technically a SOP-family extension; isSOP also true
node.family  # 'TOP' / 'CHOP' / etc.
```

## When in doubt

Call `td_list_families` for the authoritative type list under any path.
Don't guess type names — TD case-sensitively rejects typos and
`Unknown operator type` errors don't auto-correct.
