# PHASE 3 — SCREEN-SPACE ACTIVE DRAW

Scope: make `FULL_RESIDENT` render only the screen-useful point set, with
`GPU_RESIDENT` kept as a cache state and `ACTIVE_DRAWN` as a screen-space /
frame-budget state. Baseline carried in: Phase 1+2 (stable arena, incremental
per-tile upload, `GPU_RESIDENT` != `ACTIVE_DRAWN`, 732 passed / 11 failed / 6
collection errors).

Everything below is either measured on this machine or explicitly marked
`NOT MEASURED`. Nothing is inferred from the mission text.

---

## SELECTOR
```
implementation            : naksha_lod_gate.ScreenSpaceLOD.select  (REUSED, not forked)
representation type       : PREBUILT per-node multi-resolution representatives
                            + 1 dataset overview
hierarchy reused          : YES
GPU mapping
  stable ranges           : YES
```

### [LOD REPRESENTATION AUDIT] — 27M cache, Fit view, real selection

Produced by `phase3_lod_audit.py` from the live index + live resident set.

```
selector input            : (index.nodes, camera, visible_rows) - the gate reads ONLY .nodes
selector output type      : list[dict], keys = node_id, lod, points, proj_px, px_err,
                            centre_dist, row
visible rows in           : 230
selector items out        : 230
selected node identity    : index node_id (230 distinct)
selected LOD identity     : (node_id, lod) -> nodes['lod_block'] -> BLOCK_ENTRY
                            -> .nakshapc block header -> resident key
selected points are       : 35 FULL LEAF / 195 PREBUILT REPRESENTATIVE
selected representation GPU-resident : YES (230/230)
stable gpu_first/gpu_count available : YES (230/230)
1:1 to a ResidentTile     : YES (key=(node_id, lod), count == lod_point_count)
directory agrees (block_id + point_count) : 230/230
selected node boxes overlapping in XY     : 7554 (of 26335 pairs)
sum(projected px) / viewport px           : 64.537
stored world spacing / sqrt(area/N)       : p50=2.508 min=0.263 max=21.915
selected points total     : 1,726,892

sample:
  node=21 lod=0 block=42  pts= 37,104 FULL LEAF        slot_first=24865797 count=37104
  node=20 lod=0 block=40  pts= 40,199 FULL LEAF        slot_first=24325844 count=40199
  node=17 lod=0 block=34  pts= 36,111 FULL LEAF        slot_first=25122687 count=36111
  node=19 lod=0 block=38  pts= 70,232 FULL LEAF        slot_first=18401331 count=70232
  node=18 lod=0 block=36  pts= 69,427 FULL LEAF        slot_first=18681786 count=69427
  node=16 lod=0 block=32  pts= 39,095 FULL LEAF        slot_first=24485626 count=39095
```

Index structure behind those numbers (measured directly, no manager needed):

```
nodes                     : 393   (all level 0, all CHILDLESS - a flat cell set,
                                   NOT a tree; first_child is -1 everywhere)
blocks                    : 1339  (393 lod0 + 392 lod1 + 320 lod2 + 233 lod3 + 1 lod4)
node width  min/med/max   : 1.04 / 90.90 / 999.99 m
sum(node areas) / dataset area : 30.4
overlapping pairs among all 393 nodes : 13072 of 77028
node 0 overlaps           : 347 of the other 392 nodes
```

**Conclusion the audit forces** (Section 1 asks for this before any reduction is
claimed): the mapping from a selector item to a stable GPU range is **proven and
1:1** — but `sum(node areas) = 30.4x the dataset area` means the node boxes are
per-node **DATA extents, not a disjoint tiling**, so "one representative per
visible node" is **not** a coverage proof and does **not** bound overdraw. At Fit
the selected set's projected boxes sum to **64.5x the viewport**. See DEFECT 1.

`Section 2` is satisfied on evidence, not on assertion: a coherent world-space
`lod_spacing` exists for every selected representation and tracks
`sqrt(area/N)` (p50 ratio 2.5), which a prefix-truncated block cannot produce.

---

## FULL_RESIDENT — 27M
```
resident blocks           : 1339
resident points           : 28,860,837
```
(`full_resident_total_points` remains the SOURCE count, 26,960,750, as the
coverage contract; the extra 1,900,087 points are the pinned coarse pyramid.)

### OVERVIEW MOVING
```
selected blocks           : 230
selected points           : 683,657
active blocks             : 230
submitted points          : 683,657
draw ratio                : 0.0237      (submitted / resident)
selected_to_submitted     : 1.000
actual_points_per_pixel   : 0.420
```

### MEDIUM ZOOM (x2 / x4, IDLE)
```
selected points           : 1,665,594   / 1,636,662
submitted points          : 1,665,594   / 1,636,662
actual_points_per_pixel   : 1.023       / 1.005
```

### CLOSE ZOOM (x8 / x16, IDLE)
```
selected points           : 1,619,433   / 1,621,303
submitted points          : 1,619,433   / 1,621,303
actual_points_per_pixel   : 0.995       / 0.996
selector target           : 1,628,160   (= 1920 x 848 x 1.0)
```

### IDLE
```
selected points           : 1,726,892
submitted points          : 1,726,892
draw ratio                : 0.0598
actual_points_per_pixel   : 1.061
resident set during all of the above : UNCHANGED (28,860,837 points, every row)
```

Anti-fraud check that names the defect, same camera, Fit view:

```
draw-the-residency (old rule) : 393 blocks / 26,960,750 points
draw-the-screen    (Part 16)  : 230 blocks /  1,726,892 points
reduction                     : 15.61x
ACTIVE_DRAWN LOD mix at Fit   : L0:35 L1:42 L2:46 L3:107
```
The mixed LOD mix is the proof that the resident coarse pyramid is being
**selected**, not merely retained.

---

## DRAW RANGE PATH (Section 7)
```
Python owner              : NakshaStreamManager._build_ranges
adapter call              : VulkanTileRendererAdapter.set_draw_ranges(ranges)
                            -> AppRenderBackend.set_point_draw_ranges(firsts, counts)
C ABI call                : nkv_set_point_draw_ranges(handle, const uint32_t* firsts,
                            const uint32_t* counts, uint32_t rangeCount)
                            (native/naksha_vulkan/src/naksha_vulkan_c_api.cpp:1568)
native renderer function  : PointCloudRenderer::SetDrawRanges  (header, inline)
                            -> clears + rebuilds std::vector<pair<uint32_t,uint32_t>>
                               drawRanges_. No Vulkan call. No buffer touched.
draw                      : PointCloudRenderer::Record (PointCloudRenderer.cpp:728)
                            and RecordSplat (:829) issue
                            vkCmdDraw(cmd, r.second, 1, r.first, 0) per non-empty range.
number of ranges          : 230 (27M Fit), 34-66 (123.las production ticks)
number of submitted points: 1,726,892 (27M Fit), 416,363-429,692 MOVING (123.las)

effect of changing ranges : COMMAND RE-RECORD ONLY
                            - no GPU buffer copy
                            - no point upload
                            - no per-point CPU work
```
Verified at source, not from the header comment. `arenaMode_` + empty range list
draws nothing (deliberate: a full-buffer draw would rasterise unwritten gaps
between tiles).

---

## CAMERA
```
pan XYZ upload bytes      : 0
zoom XYZ upload bytes     : 0
attribute upload bytes    : 0
whole buffer rebuilds     : 0
camera-only frames        : 30 frames -> 0 positions uploaded
selection cost (settled)  : 3.8-4.3 ms/frame
selection cost (changing) : 11.0-11.7 ms/frame   [see LIMITATIONS 6]
selection cache           : 119-133 hits vs 49 real derivations
```

## RESIDENCY
```
resident set changed during pure camera test : NO
   123.las, real GUI, 9 camera steps: resident_by_state={'GPU_RESIDENT': 170}
   at EVERY step. fallback_parents=104 constant. frame_errors=0.
   stale=170, stale_incorrect=0.
draw retirement != eviction                  : YES
   draw-set changes never touch a buffer (see DRAW RANGE PATH); eviction is a
   separate arena free.
```

## LOD
```
screen-space selection    : YES
points-per-pixel budget   : YES  (0.35 moving / 1.0 idle, viewport-derived)
frame-budget integrated   : PARTIAL - see LIMITATIONS 4
hysteresis                : YES  (0.0 -> 494 transitions, 0.15/0.35 -> 3 over
                                 200 jittered frames x 5 trials)
fallback coverage         : PASS
HOLE cells                : 0    (headless detector, every zoom level)
                            0    (real GUI, 123.las, pixel level, every step:
                                  render_missing=0, largest_rendered_gap_m=0.0)
                            18-23 (real GUI, 27M, pixel level, every step) - the
                                  headless detector says 0 here and the pixels
                                  disagree. See the 27M GUI section.
```

---

## GUI (Sections 28-30) — REQUIRED, AND RUN

`diagnostics/live_pixels.py`: real `NakshaApp`, `NAKSHA_RENDER_BACKEND=vulkan`,
`NAKSHA_VULKAN_MAIN_VIEWPORT=1`, 1400x900 window, real Qt-injected wheel/drag
events, per-10 m-cell pixel coverage read back from the rendered frame.

```
production Vulkan viewport tested : YES
GPU (measured, not assumed)       : NVIDIA T400 4GB, api 1.3.277, vram 3942 MB
                                    (the Part 82 "very limited working set" case)
dataset                           : diagnostics/data/123.las
                                    (59.2 MB source, 88.9 MB .nakshapc, 170 blocks)
viewport                          : 1400x841
```

Pixel-level parity, every step (`rendered/testable`, `render_missing`):

```
0_INITIAL_FIT      src_vis=117  selected=117 active=117 submitted=None  rendered=117/117  missing=0
1_ZOOM_IN          src_vis=109  selected=109 active=109 submitted=109   rendered= 85/85   missing=0
2_PAN_AFTER_ZOOM   src_vis=108  selected=108 active=108 submitted=108   rendered= 78/78   missing=0
3_PAN_RIGHT        src_vis= 81  selected= 81 active= 81 submitted= 81   rendered= 57/57   missing=0
4_PAN_LEFT         src_vis=101  selected=101 active=101 submitted=101   rendered= 71/71   missing=0
5_PAN_UP           src_vis=102  selected=102 active=102 submitted=102   rendered= 80/80   missing=0
6_PAN_DOWN         src_vis=109  selected=109 active=109 submitted=109   rendered= 91/91   missing=0
7_ZOOM_IN_2        src_vis= 91  selected= 91 active= 91 submitted= 91   rendered= 91/91   missing=0
8_ZOOM_OUT         src_vis=109  selected=109 active=109 submitted=109   rendered= 91/91   missing=0
9_FINAL_FIT        src_vis=200  selected=200 active=200 submitted=200   rendered=200/200  missing=0
```
`selection_missing = activation_missing = draw_missing = render_missing = 0` at
every one of the 10 steps. `largest_rendered_gap_m = 0.0`.

The budget landing on target in production (real GUI, real renderer):

```
IDLE    target=1,177,400  submitted=1,181,389..1,196,894  actual_ppp=1.003..1.017
        (1400x841 x 1.0 = 1,177,400 exactly)
MOVING  target=  412,090  submitted=  416,363..429,692    actual_ppp=0.354..0.365
        (1400x841 x 0.35 =   412,090 exactly)
LOD mix at every tick is MIXED, e.g. LOD0/1/2/3 = (31,10,14,11)
resolved by 34-66 active blocks out of 170 resident
```

```
frame_ms p50 / p95        : NOT MEASURED
moving FPS                : NOT MEASURED
input-to-present p95      : NOT MEASURED
```
Reason, stated rather than papered over: this GPU/driver reports
`[VulkanContext] WARNING: GPU timestamps NOT available`, so
`nkv_get_gpu_frame_time_ms` returns -1.0, and the harness records no CPU frame
time either (`grep frame_ms|fps` over the whole run = 0 matches). No frame-time
number exists for this machine, so none is reported.

```
visual holes              : NONE DETECTED (pixel-level cell coverage above)
visual flicker            : NOT MEASURED (needs frame-to-frame capture)
camera jumps              : NONE - [CAMERA CONSUMER PARITY] passed at every step
                            (vtk_scale == main_camera == rig == native), and
                            [CAMERA SANITY] scale_preserved=True on every pan
teardown segfault         : the harness printed "DONE", wrote
                            diagnostics/stage0_p3a.json, and THEN segfaulted
                            during interpreter/Vulkan teardown. All measurements
                            completed before the crash. Not attributable to the
                            Phase 3 selection (pure-Python draw-set logic, does
                            not run at teardown) but NOT proven innocent either.
run trustworthiness       : the harness's own guard printed
                            [EXTERNAL INPUT DETECTED] wheel events seen=5..7
                            injected=1..3. The per-step delta matches injection
                            exactly (a constant +4 offset from before the watch
                            was installed), but the project's own check says NOT
                            trustworthy, so this is NOT claimed as a clean GUI pass.
### 27M DATASET IN THE REAL GUI (the Section 28 requirement)

`diagnostics/live_pixels_27m.py` (same harness, `DATA` repointed). Steps 0-3 of 9
had completed at report time; the run was still advancing.

```
GPU budget probed on the real device : safe_gpu_budget=1,591,392,337 bytes (1.59 GB)
bytes_per_point=15  estimated_gpu_bytes=444,852,375 (445 MB)
[FULL_RESIDENT] COMPLETE - 28,561,161 points resident in 1338 blocks in 11.6 s
[FULL_RESIDENT] lod0_blocks=393 points=26,960,750 coarse_blocks=946
                pyramid_points=28,860,837
resident_by_state                    : {'GPU_RESIDENT': 1339} at every step
frame_errors=0  stale_incorrect=0
```

Budget landing on target in production, 27M (1400x841 = 1,177,400 px):
```
IDLE    target=1,177,400  selected=1,349,244..1,374,328  submitted=1,349,244..1,374,328
        active_blocks=337..393  actual_ppp=1.146..1.167  draw_ratio=0.0472..0.0481
MOVING  target=  412,090  selected=  623,752          submitted=  623,752
        active_blocks=393      actual_ppp=0.530        draw_ratio=0.0218
LOD mix at every tick is MIXED, e.g. LOD0/1/2/3 = (31,43,87,232) - heavily coarse
```
`selected_points` is now populated in the GUI (1,349,244) instead of 0, which is
the DEFECT 2 fix confirmed in production rather than only in a unit test.

**Measured discrepancy - the headless hole detector is OPTIMISTIC at 27M:**
```
0_INITIAL_FIT     selected=10000 active=10000 submitted=None  rendered= 9978/10000  missing=22
1_ZOOM_IN         selected= 9600 active= 9600 submitted= 9600  rendered= 9377/ 9400  missing=23
2_PAN_AFTER_ZOOM  selected= 9600 active= 9600 submitted= 9600  rendered= 9377/ 9400  missing=23
3_PAN_RIGHT       selected= 7488 active= 7488 submitted= 7488  rendered= 7314/ 7332  missing=18
4_PAN_LEFT        selected= 9600 active= 9600 submitted= 9600  rendered= 9376/ 9400  missing=24
5_PAN_UP          selected= 6400 active= 6400 submitted= 6400  rendered= 6290/ 6300  missing=10
```
Every one of these steps reports **0 holes** from the headless detector and
`selection_missing = activation_missing = draw_missing = 0`, yet real pixels show
**10-24 unrendered visible source cells per step (approximately 0.16-0.25%)**.
The same harness on 123.las rendered **every** testable cell (`missing=0` on all
10 steps), so this is dataset-scale dependent, not a harness artifact. It is
**NOT** claimed as a clean 27M visual pass. Root cause not yet established
(candidates: cells whose points fall below the point-sprite raster threshold at
this zoom, dataset-edge cells, or a genuine draw-range gap).
```

---

## DISPLAY MODES (Section 32)
```
Neutral                   : TESTED (real GUI, 123.las all 10 steps + 27M steps 0-3)
Class                     : NOT TESTED in the GUI this phase
Intensity                 : NOT TESTED in the GUI this phase; intensity math UNTOUCHED
Elevation                 : NOT TESTED in the GUI this phase
Depth                     : NOT TESTED in the GUI this phase
XYZ upload on camera transition : ZERO   (measured, 30 frames + both harnesses)
XYZ upload on mode transition   : NOT RE-MEASURED this phase (Phase 1/2 measured it
                                  as attribute-only and did not regress)
```
`accept_stored_normal_shaded.py` remains **11/11 PASS** (normal cache hit, stored
normals, 4 B/point, LOD refines beyond overview, no blank frames, every drawn
block complete, normal/XYZ counts aligned) — that is the headless Shading
regression net, not a GUI mode test.

---

## DEFECTS FOUND BY THIS PHASE

### DEFECT 1 — the selector output is NOT a disjoint partition (measured, UNFIXED)
The audit in Section 1 is what exposed it. `sum(node areas) = 30.4x` the dataset
area; 7554 of 26335 selected node pairs overlap in XY; `sum(projected px) =
64.5x` the viewport at Fit. The dominant contributor is node 0, whose box is the
whole dataset and whose lod-4 overview therefore re-rasterises every pixel the
finer leaves already rasterise.

Neither available answer is right, which is why it is reported rather than
papered over:
* drawing the overview alongside the leaves = the 64.5x overdraw above;
* the old accidental `lod <= 0` filter = removing the overview as a side effect,
  which is 16.7x fewer points but silently discards 195 of 230 deliberately
  selected blocks.

The correct rule is draw-retirement of a coarse representation once the UNION of
finer selected representations provably covers it (Parts 44/45). That needs the
existing union-coverage machinery and a proven-coverage guard; a half-verified
version of it would drop coverage, which Section 36 explicitly forbids. **Top
item for Phase 4/5.**

### DEFECT 2 — `selected_points=0` on the screen-space path (FIXED, verified)
The density controller's counters were never fed on the FULL_RESIDENT
screen-space path, so every production `[GUI STREAM TICK]` line read
`selected_points=0` while `submitted_points` was correct — i.e. it reported "no
selection happened" when one had. Fixed in `_screen_space_active_keys` via
`density.note_candidates/note_selected`.

### DEFECT 3 — `active_draw_blocks` printed the RESIDENT count (FIXED, verified)
The new `[ACTIVE DRAW]` line initially reported `active_draw_blocks=1339` for a
230-block frame, because `_flush_gpu` hands `_record_draw_telemetry` the resident
key list. Now reads `active_draw_keys`. Verified: `active_draw_blocks=230`.

### DEFECT 4 — a hidden-state camera silently disabled the feature (FIXED earlier this phase)
`_screen_space_active_keys` read `self._last_camera`, which only `on_frame` sets.
Any caller holding a definite camera got the legacy answer while
`_screen_space_applies()` still returned True. Camera/viewport are now explicit
arguments and `last_screen_space_reason` / `screen_space_fallbacks` distinguish
"the gate answered" from "the gate was never asked".

### DEFECT 5 — the gate's answer was second-guessed by the legacy filter (FIXED earlier this phase)
`_build_ranges` re-filtered the gate's own answer by `lod <= 0`, discarding 195
of 230 blocks. `_screen_space_authoritative()` (default on;
`NAKSHA_SCREEN_SPACE_GATE_AUTHORITATIVE=0` restores the old behaviour for A/B)
makes the gate authoritative on the gate path only. Streaming is untouched.

---

## LIMITATIONS
```
1.  The 27M GUI run had completed 6 of 9 camera steps at report time (it was
    still advancing). Its key numbers are measured, not headless: 1339 resident
    blocks, IDLE ppp 1.123-1.167, MOVING ppp 0.469-0.530, draw_ratio
    0.0194-0.0481. Steps 6-9 are unreported.
2.  No frame time, no FPS, no input-to-present latency. GPU timestamps are
    unavailable on this T400/driver and no CPU frame time was recorded.
3.  Class / Intensity / Elevation / Depth were not exercised in the GUI.
4.  Frame-budget integration is PARTIAL: the viewport-derived density budget
    provably changes submitted_points (1,177,400 target -> ppp 1.003..1.017;
    412,090 target -> ppp 0.354..0.365), but nothing closes the loop against a
    MEASURED frame time, because there is no measurable frame time here.
5.  Section 5's `fallback_keys` is NOT a separately tracked set. On the
    gate path it is empty BY CONSTRUCTION - the gate's never-blank floor emits
    one representative for every visible node, so there is nothing to fall back
    to. Reported as 0, not implemented as a third set.
6.  A CHANGING camera costs 11.0-11.7 ms of pure-Python on_frame at 230 visible
    nodes (27M dataset, no GPU work in it). Ready/ready-selected is 1.000, so
    this is derivation cost, not fallback overhead.
7.  Positions are still 24 B/point (float64 world XYZ). Phase 9 untouched.
8.  GUI/visual flicker not measured; visual "no black holes" is proven by pixel
    coverage, not by eye.
9.  Teardown segfault in the GUI harness after all measurements completed.
11. THE HEADLESS HOLE DETECTOR IS OPTIMISTIC AT 27M SCALE. It reports 0 holes
    where the real pixels show 18-23 unrendered visible source cells per step
    (~0.25%). Every "0 holes" claim in this report is a HEADLESS claim unless it
    says GUI+pixel. This is the most important open item after DEFECT 1.
10. Only 1 of the 3 `[EXTERNAL INPUT DETECTED]`-flagged runs is discussable; the
    flag means the project's own harness does not certify the run.
```

---

## TESTS
```
previous                     : 732 passed / 11 failed / 6 collection errors
current passed               : 739
current failed               : 11
collection errors            : 6
new regressions              : NONE
new tests                    : +7 in tests/test_stable_gpu_arena.py (28 total)
```
The 11 failures were diffed by name against the documented baseline set and are
identical (border_logic_routing, cross_section_line_tap, crs_resolution_fallbacks
x3, main_wheel_filter, shading_crisp_budget, shading_edit_planner x2,
shading_scene_ownership, view_fields_image). The 6 collection errors remain
environmental (`osgeo`/GDAL, `naksha_converter`). `PYTEST_EXIT=1`, caused solely
by the pre-existing set.

Phase 3 tests added (all pass):
```
gate answers from the camera it is GIVEN, not hidden state
without a camera the fallback is recorded, not reported as a selection
the gate answer is not second-guessed by the legacy lod filter
the legacy filter still governs the non-screen-space path
screen-space authoritative env parsing
a settled camera reuses the selection, a camera move does not
the selection cache is invalidated when the density budget changes
```

Harnesses:
```
accept_screen_space_active.py        15/15 PASS, exit 0
phase3_lod_audit.py                  [LOD REPRESENTATION AUDIT] evidence
accept_incremental_upload.py         11/11 PASS (Phase 1/2 unchanged)
accept_stored_normal_shaded.py       11/11 PASS
accept_lod_density_budget.py         8 PASS / 1 FAIL (pre-existing, identical
                                     19/22/24/24/24 illegal overlap)
diagnostics/live_pixels.py           real GUI + Vulkan, 10/10 steps, 0 missing
```

`[ACTIVE DRAW]` (Section 25) is implemented and verified, off unless
`NAKSHA_ACTIVE_DRAW_TRACE` is set, rate-limited by `NAKSHA_ACTIVE_DRAW_EVERY`
(default 30). Measured output:
```
IDLE    resident 1339/28,860,837  budget 1,628,160  selector 230/1,726,892
        ready 230/1,726,892  fallback 0/0  active 230  submitted 1,726,892
        ppp 1.061  XYZ 0  attr 0  rebuilds 0  draw_ratio 0.0598  sel->sub 1.000
MOVING  resident 1339/28,860,837  budget   569,856  selector 230/  683,657
        ready 230/  683,657  fallback 0/0  active 230  submitted   683,657
        ppp 0.420  XYZ 0  attr 0  rebuilds 0  draw_ratio 0.0237  sel->sub 1.000
```

---

## FINAL
```
GPU_RESIDENT != ACTIVE_DRAWN           : YES
FULL_RESIDENT SCREEN-SPACE LOD         : YES
CAMERA-ONLY XYZ UPLOAD                 : ZERO
WHOLE-BUFFER REBUILD                   : ZERO
29M DRAW WORKLOAD SCREEN-BOUNDED       : YES (27M measured; draw_ratio 0.0237
                                         moving / 0.0598 idle, submitted within
                                         1.06x of the viewport-derived target)
PHASE 3                                : PASS against the architectural target,
                                         with the limitations above
READY FOR PHASE 4 EVENT COALESCING     : YES, but DEFECT 1 (selector output is
                                         not a disjoint partition -> 64.5x
                                         projected overdraw) should be fixed
                                         first, because it is a draw-set
                                         correctness issue, not an optimization.
```

The acceptance target of Section 31 is met on measurement, not on assertion:
resident point count no longer forces submitted point count — **28,860,837
resident, 1,726,892 idle, 683,657 moving**, with the resident set provably
unchanged and zero bytes uploaded while the camera moves.

**STOPPING AFTER PHASE 3** as instructed. No compressed-cache, prefetch, Surface
or billion-scale scheduler work was started.
