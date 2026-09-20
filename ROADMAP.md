# PixelFoundry IDE — Roadmap

> Audience: maintainers and prospective contributors. This is a living document, updated as the project evolves.
> 中文版（Chinese）: [ROADMAP_CN.md](ROADMAP_CN.md)。

## 1. Vision

Turn the full "AI generation → pixelization → polish → game assets" flow into a **pixel-first, all-in-one tool**:

- Input: text prompt / your own reference image / image-to-video
- Output: pixel animations (GIF/APNG/PNG sequences), sprites, **map tiles & tile maps**, game-engine-ready assets

## 2. Guiding Principles

1. **Pixel-first**: every operation (scaling / keying / clustering / seamless tiling) must keep pixels crisp and sharp — "Perfect Pixel" is this project's brand promise.
2. **Demo offline**: the Mock API must always run the full pipeline, so new users can try it with zero setup.
3. **Provider-agnostic**: the API adapter layer isolates providers; adding one means configuration, not code changes.
4. **Quality over quantity**: every feature ships at "pixel-grade" completion — no half-finished pile-ups.
5. **Sustainable maintenance**: tests, CI, i18n, and docs evolve together with features.

## 3. Current State (v1.1.0)

| Module | Status |
|--------|--------|
| Solo one-click pipeline (text→image→video→pixelize→key→export) | ✅ Working (reference i2i, loop closing, background stability, multi-provider adapters, size auto-fallback) |
| IDE step workspace (6 steps, timeline, per-step params panel) | ✅ Working (v1.1: top step bar + three columns, first-frame/frame-sequence connected) |
| Pixel editor (shapes/symmetry/wrap/transforms + selection/layers, color families, color wheel, import/export) | ✅ Working |
| Sprite workflow (grid sheet → crop → key → export, IDE sync) | ✅ Working |
| Standalone pixel board (resolution settings, two-way sync, video first-frame) | ✅ Working (v1.1: draggable dock sections, one-row filter, slimmer right panel) |
| **Relay (aggregator) API setup — auth / endpoint probing / field paths / cURL import / one-click adapt** | ✅ Working (v1.1 main line, see below) |
| **Tilemap mode (5th mode, v0.2–v0.3 main line)** | ✅ Working |
| **Krita-style shell (menu bar / contextual toolbar / docker panels / resizable splitters / stacked fill)** | ✅ Working (all 5 modes) |
| zh/en i18n + UI scaling + DSH-style icons | ✅ Working (no residue in either direction) |
| CI (GitHub Actions, Py3.11/3.13 × Win/Linux), **700** tests | ✅ Running |
| Windows packaging (PyInstaller onedir) + GitHub Release | ✅ v1.1.0 |

### What's new in v1.1.0

- **One-click relay endpoint adapt**: `core/api/endpoint_probe.py` probes the common video submit endpoints
  (harmless GETs only by default), normalises pasted Base URLs (a full endpoint URL is split correctly), and
  a result table writes the submit/poll URLs and provider adapter back in one click. Plus six auth styles,
  15 template placeholders, configurable submit method and poll body, field-path fallbacks, and
  *Preview request / Test & detect fields / Import from cURL*. See `docs/api_setup.md`.
- **IDE mode rebuilt**: top step bar (✓ done / ▶ current / ○ pending, click to switch) + three columns
  (assets | preview·edit·prompts with the timeline underneath | params + log); parameters expanded by
  default. **Two blocking bugs fixed**: images could not be added to the frame sequence, and
  first-frame → video got stuck.
- **Standalone canvas layout**: the dividers inside the asset dock (pack list / folder tree / thumbnails)
  are draggable and the hard height caps are gone; category dropdown and search share one row; the right
  dock went from three panels to two.
- **Quality**: ~70 new English strings and three real i18n defects fixed; tests 600 → 700; CI green on all
  four platform/Python combinations.

### What's new in v1.0.0 (previous)

- **Krita-inspired shell**: menu bar + contextual toolbar + mode rail + status bar; one unified docker
  system (collapsible headers, whole-dock collapse into a vertical tab, drag-resizable and remembered
  widths, **collapsing frees space** for the other expanded dockers in the same column); every mode's
  parameter column is a draggable splitter; dark/light QSS rewritten.
- **Practical pixel-editor tools**: line/rectangle/ellipse shapes with live preview, symmetry drawing
  (horizontal / vertical / four-quadrant), wrap-around drawing for seamless tiles, canvas transforms
  (flip / rotate / crop / resize / integer scale), 1×–8× export, copy to clipboard, GIMP `.gpl`
  palette import/export, tool shortcuts.
- **In-pack folder browsing**: every level of an imported tile pack (`atlas/`, `tiles/<terrain>/`,
  `textures/`, `source/`, `pieces/`, `props/`) is browsable and can be placed on the canvas; plain
  image folders without a manifest are supported too.
- **Quality**: bilingual switching leaves no residue; the tile cache verifies object identity
  (no more rare wrong-tile renders); dead code and duplicate implementations removed
  (English pack 1028 → 977 keys, shared modules extracted); tests 527 → 600.

### Tilemap mode (shipped)

- **Terrain ecosystems**: one prompt → 2×2×3 sheet (base terrain + 3 features); automatic grid-frame
  stripping, text detection (constant-stroke-width signature) and 9-cell median texture to kill one-off
  text, offset-quilt splicing for **pixel-exact wrap equality**, then **aligned composition** per mask so
  adjacent tiles are pixel-identical along shared edges; band depth / layered outline / bevel / AO measured
  from the AI art; irregular inward noise on non-interior edges; **block-noise percolation** across
  terrain — and across *different tile packs* — removes hard boundaries.
- **Buildings**: wall **16-tile family** (straight / corner / inner+outer corner / tee / cross / end /
  isolated) with fixed cross-section geometry, transparent exterior for overlay compositing, procedural
  1px outline, top-face + front shading, AI-derived doors and pillars, plus a **live auto-stitching wall
  layer** in the map view.
- **Props**: variant-grid generation → background key chosen from the prompt (pure white, or pure black
  for light subjects such as snow) with range deletion + border flood fill → alpha hardened to 0/255 →
  trimmed and bottom-aligned; placement is scalable (25–400 %).
- **Tile packs & export**: complete tileset folder + zip (47-tile atlas 8×6, every single tile,
  textures/pieces/props, all metadata, README); import from folder / zip / `.tilepack`.
- **Map preview**: usable **without generating first**; mix several packs (terrain / buildings / props),
  grid toggle, Ctrl+left-drag pan, wheel zoom, brush / eraser / rotate / scale; **Perlin-noise big world**
  (up to 400×400, optional scattered buildings, mask-cached tile composition).
- **Top-down 2.5D**: a top-face layer (47-tile family) plus a cliff layer (16-tile family derived from the terrain art);
  height can be painted live in the preview, and long cliffs stay continuous tile-to-tile.
- **Reference image (i2i)**: tileset generation accepts a reference image to keep palette/style consistent.
- **Layout compatibility**: 47-tile (8×6 blob convention), 16-tile wall family, and **dual grid** for both
  generation and preview.
- 64 algorithmic invariants locked down by tests (shared-edge pixel equality, no text/frame residue,
  47-class coverage, transparent exteriors, zero background residue, deterministic output).

Known tech debt: large onedir package (~250 MB), some workflow logs not yet i18n'd, GUI details not fully
covered by tests, no Tiled `.tmx/.tsx` import/export yet, map layering limited to terrain + overlay + wall.

## 4. Phase Plan

### Phase A: Map Tile Generation + Tile-Map Editor (main body done in v0.2–v0.3 ✅)

**A1 Tile-set generation** ✅ Done
- Text-to-image for a 2×2×3 terrain-ecosystem sheet (base terrain + 3 features), a 2×2 building sheet and
  prop variant grids, with built-in prompts for equal cells, solid background, **seamless tiling**,
  consistent style, and no text/frames/grid lines;
- Frame stripping (expected-position ± tolerance darkest-run search + residual sweep), text detection and
  patching, offset-quilt splicing (wrap-equal), 9-cell median texture; aligned composition derives the whole
  tile family (band / outline / bevel / AO measured from the AI art);
- Export a **complete tileset folder + zip** (47-tile atlas 8×6, every single tile, metadata), compatible
  with common Tiled layouts.
- DoD met: adjacent tiles are pixel-identical along shared edges; long runs and walls show no per-tile seam.

**A2 Tile-map editor** ✅ Done (5th mode)
- Tile-grid canvas with multi-pack mixing (terrain / buildings / props), brush / eraser / rotate / scale,
  grid toggle, Ctrl+left-drag pan;
- **Live auto-stitching wall layer** (16-tile piece chosen from the four neighbours), building overlay layer,
  transparent-exterior compositing;
- **Perlin-noise big world** preview (procedural continents / rivers / mountains, optional scattered
  buildings) and **dual-grid** rendering;
- Export PNG preview + map JSON (overlay name/rotation/scale, wall layer, dual flag).
- TODO: Tiled `.tmx/.tsx` import/export, flood fill / rectangle / multi-layer stack.

**A3 Pipeline integration** ✅ Mostly done
- Tile packs (folder/zip) load into any preview and mix freely; one pack format for terrain, buildings and
  props;
- TODO: push tiles/maps back into IDE and pixel mode for polishing.

**A4 Large-map performance** 🚧 Partial
- Done: tile composition cached by (terrain art, mask) — a 96×64 map renders in about a second; cross-pack
  blending and the wall layer are vectorized;
- TODO: viewport rendering, async export of very large maps, lazy tileset loading.

### Phase B: Solo Performance & Generation Quality

**B1 Performance**
- **Result caching**: hash prompts/images; repeated generations reuse results (saves tokens and time);
- **Parallel pixelization**: numpy vectorization + multi-frame thread/process pools;
- **Fewer tokens**: compress LLM templates (already max_tokens 800), keep min/max side for first-frame images, downsize image requests when possible;
- Async large-image preview/export with finer progress granularity.

**B2 Quality**
- **Multi-candidate generation**: `n=2–4` outputs scored objectively (sharpness / grid purity / subject completeness), best auto-picked, user can pick from the UI;
- **Prompt template upgrade**: few-shot examples + parameterized style presets (pixel style / palette / outline strength);
- **Video consistency**: per-frame color-histogram matching after sampling, better loop start/end detection;
- **Keying upgrade**: optional outline pass, edge anti-aliasing, feathering;
- **Seed control**: reproducible generation.

**B3 Quality regression baseline**
- A sample image set + objective scoring script; every change runs the baseline to prevent quality regressions (optional CI job).

### Phase C: Pixel Editor & Sprite Refinements

**C1 Pixel editor**
- More tools: line / rectangle / ellipse / symmetry / magic wand / pixel-font text;
- **Real layer stack** (replacing the single floating layer) + layers panel;
- In-editor animation preview (play frame sequences inline), onion-skin strength control;
- Customizable shortcuts, pattern brushes.

**C2 Sprite**
- **In-page per-frame editing** (click a cell to edit directly, no IDE sync first);
- Crop improvements: content bounding box + configurable padding, border-removal params UI;
- Multi-action sheets (one sheet, multiple action rows), collision-box annotation;
- Multi-candidate generation picker.

### Phase D: Continuous Polish (throughout)

- **D1 Engineering**: PyInstaller onefile + icon + version self-check; optional auto-update; CI packaging job + codecov;
- **D2 UX**: empty-state onboarding, shortcut help, simplified settings, friendlier error messages, log levels & search;
- **D3 i18n**: 100% English coverage, more language packs (e.g. Japanese), font adaptation;
- **D4 Docs**: user manual (zh/en), sample gallery, API adapter docs, FAQ;
- **D5 Community**: issue templates, Contributing guide, first external contribution.

## 5. New Plans (Phases E–I, v1.1 → v2.0)

> Phases A–D were written during v0.2–v1.0 and are kept verbatim for traceability; E–I below are the directions
> added by the maintainer after the v1.0.0 release and **do not rewrite any of the history above**. How the five
> fit together: E and G are new engines, F and I deepen existing pipelines, and H is a capability layer that cuts
> across every generation path.

### Phase E: Strict Isometric (Rhombus) Tilemaps

**Goal**: on top of the existing top-down / top-down 2.5D modes, add a **strict isometric (2:1 rhombus) tile**
system — generation, mask derivation, preview editing and export all the way down, instead of skewing top-down
tiles.

**E1 Geometry & coordinates** (new module `core/tilemap/isometric.py`)
- Rhombus geometry (2:1 plus configurable ratios): grid ↔ screen conversion `x=(c−r)·tw/2, y=(c+r)·th/2` and the inverse hit test;
- **Depth sorting**: paint by `c+r` (painter's algorithm) with multi-layer support per cell (ground / decoration / building / unit) and ordering fixes for half-cell-offset objects;
- Isometric four-neighbourhood (rhombus edges) + redefined diagonal semantics: in isometric view the "corner" is a traversability junction, so the mask family needs its own definition.

**E2 Masks & derivation** (reuse aligned composition, rewrite the mask layer)
- Isometric **47-variant family**: straight / inner+outer corners / ends along rhombus edges, plus diagonal transitions;
- **Ramps and stairs**: 8-direction ramps (low→high), steps, and isometric variants of cliff sides;
- Height layers: multi-level plateaus (0–N), each with a rhombus top face plus left/right cliff faces and automatic piece selection from neighbour height deltas; shares the art derivation of the top-down cliff 16-tile family but recomputes geometry isometrically (side walls stay 1:1 pixels, never stretched by the projection, so pixels stay sharp);
- Reuses: inward edge noise, cross-terrain/cross-pack **block percolation blending**, and the shared-edge pixel-identity constraint (in isometric view the shared edges are the two slanted sides of the rhombus).

**E3 Generation pipeline adaptation**
- Prompt templates gain isometric variants (explicitly "isometric 2:1 rhombus, 30° top-down, no perspective distortion");
- Post-processing: grid-frame removal and mask cropping on the rhombus grid (the existing 2×2×3 sheet flow stays, only cropping/derivation geometry changes);
- Pack metadata gains `projection: "isometric"` plus ratio fields; export isometric atlases (rhombus layouts, both 2:1 and 1:1).

**E4 Preview & editing**
- Isometric brush (rhombus hit test + automatic neighbour stitching), height brush (raise / lower / smooth), ramp brush;
- Grid/coordinate readout (cell + world coordinates), isometric axis guides, eyedropper (pick the cell under the cursor);
- Perlin big world mapped to isometric (procedural terrain → isometric height map, with rivers/mountains/coastlines generating ramps and cliffs automatically).

**E5 Export & interop**
- Export: isometric atlas PNG + JSON, Tiled maps with `orientation="isometric"` (`.tmx` + `.tsx`), plus metadata guidance for Godot 4 isometric `TileSet` and Unity isometric Tilemap;
- Round-trip with the pixel canvas: "grab a region" from the map into the canvas for touch-up and write it back (merges with the A3 to-do).

**Definition of done**
- Isometric tiles are pixel-identical along their shared slanted edges; the same map data stays **topologically identical** between the top-down and isometric views;
- Exported `.tmx/.tsx` opens in Tiled with layers, tilesets and properties intact; cliffs never break at any plateau height combination.

### Phase F: Image-to-Video → Sequence-Frame Algorithm Refinement

**Goal**: lift "AI video → frame extraction → pixel sequence frames" from "it runs" to "stable, loopable, low-flicker,
sensibly framed" — output that can go straight into a game.

**F1 Extraction strategy**
- Content-aware extraction: inter-frame similarity (perceptual hash / colour histogram / structural similarity) plus motion-energy peak detection, so action keyframes are never sampled away;
- Even sampling and target-frame-count constraints side by side (`max_frames` / minimum frame spacing), with "keep motion" and "keep duration" modes;
- Keyframe protection + forced first/last frame inclusion; visualised extraction (kept frames highlighted on the timeline).

**F2 Stability (de-jitter & alignment)**
- Global motion estimation: per-frame translation/scale/rotation (phase correlation with a feature-point fallback) to align the subject back to the reference frame;
- Background estimation: temporal median/percentile composite for a stable background, suppressing the "boiling" of AI video backgrounds;
- Optional subject segmentation: reuse the keying pipeline to separate the subject, then align the subject and composite onto the fixed background.

**F3 Loops & pacing**
- Loop closure: first/last similarity detection → drop redundant trailing frames or interpolate back; optional cross-fade;
- Pacing resampling: frame count adapts to motion amplitude (more motion → more frames), and GIF export emits a **per-frame delay table** (variable-duration animation);
- Loop quality metrics: closure error and velocity continuity (no hitch at the loop point).

**F4 Colour consistency & pixelization hand-off**
- Per-frame colour stabilisation: match histograms/palettes against the whole clip's median palette to remove brightness and hue drift;
- **Grid drift detection**: the current "frame 0 defines the grid" stays global; a new check re-detects (and re-aligns) the grid per segment when later frames drift noticeably, so Perfect Pixel does not slowly wander on long clips;
- The pixelization order becomes fixed: stabilise → quantise → key, avoiding per-frame jitter.

**F5 Quality assessment & advice**
- Objective metrics: flicker index, subject jitter amplitude, loop error, inter-frame colour drift, transparency-edge stability;
- UI report: say *which* metric fails and *what to change* (more frames / higher playback speed / another extraction mode / retry).

**Definition of done**
- Looping GIFs are seamless with no visible brightness step; two runs of the same clip with default settings are **frame-for-frame identical** (deterministic);
- The metrics panel correctly identifies the kind of degradation; extraction stays consistent with the pixelization grid (no per-frame wobble).

### Phase G: Skeleton + Vector Sequence-Frame Generation

**Goal**: generate animation frames **without any video API** by rigging a single pixel source image with bones and
deforming a mesh (cut-out / shadow-puppet style 2D skeletal animation) — while **keeping the pixel look** instead of
dissolving into smooth interpolation.

**G1 Rig editor** (a new canvas mode)
- Bones: joint tree (position / rotation / scale / parenting / optional IK) drawn directly on the pixel canvas with a bone overlay;
- Mesh & weights: automatically triangulate/quadrangulate the subject and compute weights (distance falloff + alpha-edge constraint + denser sampling near joints), with manual weight brushes (add / subtract / smooth);
- Parting: split the subject into parts (torso / arms / legs / weapon) by colour family or painted masks, each rigged and ordered independently (prevents occlusion mix-ups).

**G2 Pixel-faithful deformation**
- Deformation: linear blend skinning (LBS) with thin-plate spline (TPS) as an alternative; **integer displacement + nearest-neighbour resampling**, never bilinear half-transparent edges;
- Colour constraints: snap back to the original palette after deformation (quantise + colour-family mapping) so colour counts never grow and gradients never break;
- Joint handling: local resampling / local rotation / pixel-level patching at joints (optional automatic hole filling plus manual frame touch-up);
- Anti-jitter: quantise sub-pixel motion into integer steps and suppress phase jitter (no "breathing").

**G3 Motion & baking**
- Preset motion library: walk / run / idle / jump / slash / hit / death, keyframes + interpolation curves (ease in-out), parameterised by period, amplitude, phase and mirroring;
- Timeline baking: bake to the target FPS/frame count → reuse the existing pixelize / key / export chain (GIF / APNG / PNG / sprite sheets / multi-action sheets);
- Skin swapping: the same skeleton plus motion applies to different characters (same size/structure) for batch asset production.

**G4 Combining with the AI pipeline**
- AI video extraction (Phase F) as **motion reference**: estimate joint trajectories from the video → drive the skeleton → bake clean frames locally (stable structure, no AI jitter);
- The reverse: baked skeleton frames become i2i references for AI restyling, then re-pixelized.

**G5 Export & interop**
- Self-describing rig + motion JSON (versionable, diffable), plus Spine / DragonBones compatible JSON export for engine reuse;
- Preview images (skeleton + keyframe grid) into tile packs / project files.

**Definition of done**
- One 32×32 character → 8 walk frames: no interpolation contamination (no translucent edges), colour count unchanged, edges hard;
- The same motion applied to a second character of the same spec produces frames directly; baking is deterministic and reproducible.

**Risks & fallback**: joint tearing and sub-pixel jitter in pixel-space deformation are the hard parts — the fallback is
"split parts + integer pixel displacement + manual frame touch-up" (cruder but absolutely pixel-safe), with the automatic
path as an accelerator.

### Phase H: Style Plugins (LoRA-like Style Control)

**Goal**: pull "style" out of scattered prompts into **installable, composable, shareable** plugins that take effect
uniformly across every generation path.

**H1 Plugin format (declarative, no code execution)**
- A folder or zip: `style.json` (`id / name / version / author / tags / targets / license`) + positive prompt fragments + negative words + quality words + reference image(s) + optional palette (`.gpl`) + preview image;
- Validation & version compatibility: field allow-list plus a version number, with readable failure reasons; plugins are **data only, never scripts** (security boundary).

**H2 Layered effect (automatic degradation per provider capability)**
1. **Prompt layer** (every provider): assemble style fragments + negative + quality words, with weight syntax;
2. **Reference layer**: feed plugin reference images into the existing i2i paths (Solo / sprite / tileset);
3. **Palette layer**: quantise / map colour families to the plugin palette after generation (pixel-first, reusing the palette chain);
4. **Native LoRA layer**: for self-hosted backends (ComfyUI / SD WebUI / compatible APIs) pass `lora_name` / `lora_weight` / `model` / `vae` / `sampler` through the existing **custom request-body templates**; ship ComfyUI workflow examples.

**H3 Combination & weights**
- Multiple style plugins stack, each with a 0–150 % weight; conflict detection (mutually exclusive tags, palette clashes, duplicated negatives) with clear warnings;
- Presets: save "plugin set + weights + parameters" as a named style preset and switch in one click.

**H4 Management UI & distribution**
- "Settings → Styles" and the main toolbar: local plugin list (import zip / open folder / enable-disable / preview thumbnail / delete) with the active set and weights visible;
- Export a plugin from inside the project (bundle the current prompts + reference images + palette); on import show author, licence and preview;
- A single `StyleContext` threads through text-to-image, sprite sheets and tilesets so one plugin looks the same in all three.

**Definition of done**
- After importing a pixel-art plugin, Solo / sprite / tileset outputs share one consistent style;
- On providers without LoRA support the plugin degrades automatically to "prompt + reference + palette" and still shows a clear style effect;
- A plugin exports in one click and reproduces on another machine (with preview and licence info).

### Phase I: Standalone Pixel Canvas Productivity

**Goal**: make the standalone pixel canvas able to "get a day's work done on its own" — full character / prop / icon
production without switching to IDE mode.

**I1 Layers & selection** (core editor upgrade)
- Real layer stack: multiple layers + groups + blend modes (normal / multiply / overlay / screen / …) + opacity + lock/hide + drag reordering + a layers panel;
- Stronger selection: magic wand (tolerance / contiguous / invert), select-by-colour, add/subtract/intersect, selection transforms (move / rotate / flip / scale / skew), floating selection;
- Stronger history: named snapshots, a history panel (jump back), operation coalescing (one drag = one undo step).

**I2 Drawing throughput tools**
- **Stamps & patterns**: multi-cell brushes (drop a 2×2 / 3×3 pattern at once), pattern fill (use tiles from a pack as stamps), random-variant brushes (scatter trees/flowers/rocks with density and region control);
- Pixel-font text: built-in 5×7 / 8×8 bitmap fonts (Latin + CJK) with outline / shadow / spacing / alignment;
- Dithering & gradients: Bayer / noise dither fills, two-colour gradients, gradients within a colour family;
- Brush upgrades: custom brushes saved from a selection, a brush preset panel, optional pressure/speed mapping.

**I3 In-canvas animation workbench**
- Multi-frame management: timeline with add/duplicate/reverse/shift frames, frame tags, loop range; adjustable onion-skin strength and frame counts;
- Variable-speed playback preview plus **batch frame shifting** (bob / breathe / rotate in one click, sharing the timeline with Phase G's skeletal baking);
- Direct export to GIF / APNG / PNG sequence / sprite sheet (with grid layout and metadata).

**I4 Reference & measurement**
- Reference layer: pinned underneath at partial opacity, grid-aligned, scale-aligned (for tracing);
- Pixel ruler / coordinate picking / distance measurement / angle guides; eyedropper sample history (last N colours).

**I5 Interop with packs / maps / IDE**
- Any canvas region → save as a pack asset (auto-named with metadata); "grab a region" from the map preview into the canvas and write it back (merges with the A3 to-do);
- Stronger two-way sync with IDE and sprite modes (preserve layer structure on sync, choose frame-sequence direction).

**I6 Efficiency & automation**
- Command palette (`Ctrl+Shift+P` to search and run any command), full shortcut customisation with conflict detection;
- **Macro recording**: record a sequence of operations into a replayable action (e.g. "outline + shadow + export at 4 sizes") and bind it to a shortcut or toolbar button;
- Batch export: multiple sizes / formats / naming rules in one go, with savable export presets.

**Definition of done**
- The canvas alone completes "32×32 character → 8 walk frames → sprite sheet + GIF export";
- Layers, selection and undo stay responsive on a 128×128 canvas; macro recording replays reliably and produces the multi-size outputs.


## 6. Milestones

| Milestone | Scope | Status |
|-----------|-------|--------|
| **M1 (v0.2)** | A1 tile sets + A2 tile-map MVP + packaging | ✅ Released |
| **M2 (v0.3)** | 47/16/dual-grid algorithm work, building 16-tile family, prop pipeline, tile-pack import/export, boundary percolation blending, Perlin big world, preview-without-generating, screenshot gallery | ✅ Released |
| **M3** | Tiled `.tmx/.tsx` import/export, map layer stack + rectangle/fill tools, A3 polish round-trip, A4 viewport/async export | Next release |
| **M4** | B1 result caching/parallelism + B2 multi-candidate & prompt templates + C2 inline sprite editing | After M3 |
| **M5 (v1.0)** | Stabilization, 100 % i18n, D1 auto-update, community, **v1.0** | ✅ Released (2026-09-18) |
| **M6 (v1.1)** | Relay API one-click adapt (auth / endpoint probing / field paths / cURL import) + IDE and canvas layout rebuild + first-frame/frame-sequence fix; **I canvas productivity** (layer stack / selection / stamps / macros / command palette) and **F extraction refinement** still pending | 🚧 Partially shipped (v1.1.0, 2026-09-20) |
| **M7 (v1.2)** | **E strict isometric (rhombus) tilemaps**: geometry & mask layer, ramps/stairs families, isometric preview & editor, isometric export (Tiled/Godot) | Planned |
| **M8 (v1.3)** | **G skeleton + vector sequence frames**: rig editor, pixel-faithful deformation, preset motion library & baking, complementary to AI extraction | Planned |
| **M9 (v2.0)** | **H style plugins (LoRA-like)**: plugin format & layered effect, combination weights, management UI, cross-pipeline consistency, plus D1–D5 polish | Planned (major) |

> Sequencing rationale: I/F deepen existing pipelines with contained blast radius and the most immediate day-to-day
> payoff, so they go first; E is a new engine that still reuses the existing tile algorithms; G is the heaviest **new
> engine** (it needs a new rig/deformation data model plus pixel-fidelity constraints); H is a cross-cutting capability
> layer, best done once the animation and projection systems have settled, so the generation path and the rendering
> path are never churning at the same time.

## 7. Quick Wins (highest ROI first)

**Original list (phases A–D, still valid)**

1. Packaging (onefile / drop pycache / size) — benefits every release;
2. Solo result caching — the most direct time/money saver for users;
3. Tile-set generation (A1) — fully reuses the sprite pipeline: low dev cost, big feature win;
4. In-editor animation preview + onion-skin strength — daily editor UX (**folded into I3**);
5. Multi-candidate generation — biggest perceived quality gain.

**New list (phases E–I, by payoff ÷ cost)**

6. **F2/F4 stability and grid-drift detection** — decides whether "AI video → sequence frames" is actually usable; top priority;
7. **I1 layer stack + I2 pattern/stamp brushes** — turns the pixel canvas from "capable" into "pleasant";
8. **I6 macro recording + batch export** — the key to mass-producing assets;
9. **E1/E2 isometric geometry and mask layer** — opens the isometric game market (farming / tactics / RPG) while reusing existing algorithms;
10. **H1–H3 style plugin format with layered effect** — land the "prompt + reference + palette" trio first (no provider support needed), native LoRA later;
11. **G1/G2 rigging and pixel-faithful deformation** — a long-term investment: validate value with the "split parts + integer displacement" path before adding automatic skinning.

## 8. How to Track

- Create GitHub **Milestones (M1–M9)** and **Labels**: `tiles` / `map-editor` / `isometric` / `solo-quality` /
  `video-frames` / `skeleton-anim` / `style-plugin` / `performance` / `pixel-editor` / `sprite` / `i18n` /
  `packaging` / `docs` / `good-first-issue`;
- Every PR links to an Issue; milestones are decomposed from this document;
- Quality bar: any change must pass `pytest` (currently **600** tests) without regressions, CI must be green on all
  four platform/Python combinations (Py3.11/3.13 × Win/Linux), and pixel-related changes must attach evidence of
  "shared-edge pixel identity / no interpolation contamination" (screenshot or test).

---

*Last updated: 2026-09-20 (alongside v1.1.0: one-click relay API adapt, IDE three-column rebuild with
first-frame/frame-sequence fix, standalone canvas layout polish; Phases E–I unchanged, M6 marked partially shipped)*
