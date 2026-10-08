# BILLION-POINT SCALABILITY — PHASE 1 + PHASE 2 + PHASE 3 REPORT

Scope of this session: **Phase 1 (GPU_RESIDENT != ACTIVE_DRAWN) and Phase 2
(remove whole-buffer repack/reupload; implement stable block GPU allocation)**,
plus the measurement harnesses needed to prove them.

This is explicitly NOT a 1-billion-point acceptance. Phases 3-17 are untouched
and are listed as such in §6. Nothing below is extrapolated.

---

## 1. WHAT WAS BROKEN (measured, with line references)

`gui/naksha_cache/stream_manager.py`

1. **`_reconcile` repacked every resident tile on every pass.** The tail of the
   method assigned `v.first = off; off += v.count` over every `GPU_RESIDENT`
   tile, in priority order. `v.first` is the tile's address in the GPU buffer, so
   **every residency change renumbered every tile**.

2. **`_flush_gpu` then re-sent the whole buffer.** The `structural` branch did
   `xyz_p = [self.resident[k].xyz for k in keys]` ->
   `np.concatenate(xyz_p)` -> `adapter.upload_resident(xyz, ...)`. That is
   O(sum of all resident points) per residency change - one new 64K block cost a
   re-upload of every resident point.

3. **No separate ACTIVE_DRAWN.** `_build_ranges` already filtered what it
   submitted, but the result was never recorded, so residency and the frame's
   submission set had no independent identity to inspect or test.

4. **Attributes and normals were concatenated in one global stream**
   (`_upload_missing_attributes`, `_upload_normals`), which only works if all
   tiles live in one contiguous buffer - the thing (1) and (2) existed to
   maintain.

The native side did not need changing: `nkv_upload_point_tile` /
`nkv_reserve_point_capacity` and the `GpuArena` allocator already existed but
**`GpuArena` was never imported or used by the stream manager**. The arena
protocol was dead code.

---

## 2. WHAT CHANGED

### 2.1 Stable slots (Parts 11/12/14)

- `GpuArena` is now imported and owned by the manager (`self.arena`), created
  lazily by `_arena_ensure()` **only when `adapter.supports_arena()` is true**.
- Pool capacity is **derived from the measured GPU budget**, never hardcoded:
  `capacity = gpu_budget * ARENA_RESERVE_FRACTION (0.55) / 24 B per point`.
  The 45% left over is for attributes, normals, the surface, staging and the
  swapchain (Parts 20/21).
- The pool is **never regrown**: admission is already byte-budgeted, so growing
  it mid-session would invalidate every live slot.
- `ResidentTile` gained `gpu_first` / `gpu_attrs` / `arena_slot_valid`.
  `arena_slot_valid` exists because the legacy path packs offsets into the same
  `first` field - without an explicit ownership flag a packed offset could be
  handed back to the allocator and corrupt its free list.
- `_reconcile` **no longer repacks when an arena exists.**

### 2.2 Incremental upload (Part 14)

`_flush_gpu` now takes the arena path when one exists:

| | whole-buffer path | stable-slot path |
|---|---|---|
| what moves when a block arrives | every resident point | that block only |
| `np.concatenate` | once per structural change | never |
| `upload_resident` | every structural change | never |
| `existing_points_reuploaded` | n/a | **0 by construction** |

New telemetry: `new_blocks_uploaded`, `new_points_uploaded`,
`existing_points_reuploaded`, `whole_buffer_rebuilds`, `arena_used`,
`arena_free_runs`, `arena_epoch`.

### 2.3 ACTIVE_DRAWN (Part 10)

`_build_ranges` is now the single writer of `active_draw_keys` /
`active_draw_points`. A LOD change edits draw ranges only; it cannot evict,
decompress, concatenate or re-upload. `active_draw_report()` exposes
resident-vs-active, and `_record_arena_telemetry` publishes it per frame.

### 2.4 Per-tile normals and attributes (Parts 13/53/54)

Attribute and normal streams are written **at the tile's own slot**, in the same
native call as the positions, so "normal i is point i" is structural rather than
a second ordering step that can drift. A late attribute arrival is an
attribute-only write (`xyz=None`) that leaves the position buffer untouched.
`HeadlessTileRenderer` and `VulkanTileRendererAdapter` now both count the
per-tile normal upload, so "normal blocks streamed on demand" stays measurable.

### 2.5 One kill switch

`NAKSHA_GPU_ARENA=0` previously only affected the Vulkan adapter; the headless
twin ignored it, so an "arena off" A/B run silently still used the arena. Both
adapters now honour it, which is what makes the A/B in §4 meaningful.

### 2.6 Fragmentation policy (Parts 12/43/46)

A tile that cannot be placed (pool full or fragmented) triggers
`_arena_evict_for`, which demotes the **lowest-priority tiles that are not in
ACTIVE_DRAWN**, then retries. If it still fails, the tile is marked `EVICTABLE`
and stays out of the draw set rather than advertising a slot the GPU never
received. Slots are released only on true eviction (tile leaves `self.resident`
or is demoted) - draw retirement and GPU eviction remain separate.

---

## 3. TEST EVIDENCE

New file: `tests/test_stable_gpu_arena.py` — **21 tests, all passing.**
Uses the STRICT `HeadlessTileRenderer` arena (rejects out-of-range writes,
overlaps and short attribute streams), so an allocator bug that the real GPU
would turn into silent corruption fails loudly.

Highlights:

| test | Part | what it pins |
|---|---|---|
| `test_one_new_block_does_not_reupload_existing_blocks` | 98 #5 | 1 arrival ⇒ 1 upload; both survivors keep their slot |
| `test_residency_change_never_concatenates` | 98 #5/6 | `np.concatenate` is patched to raise; the flush must not reach it |
| `test_active_draw_is_a_subset_and_a_lod_change_uploads_nothing` | 98 #3/#6 | LOD change = draw ranges only, 0 uploads, 0 slot movement |
| `test_camera_only_frame_uploads_nothing_at_all` | 98 #6 | 20 camera frames: 0 position, 0 attribute, 0 tile uploads |
| `test_eviction_returns_only_that_slot` | 11/12 | true eviction frees one slot; survivors do not move |
| `test_a_full_pool_evicts_low_value_tiles_and_never_the_active_one` | 43/46 | overflow never moves or blanks the ACTIVE tile |
| `test_draw_ranges_are_skipped_for_a_tile_with_no_slot` | 43 | no slot ⇒ not submitted |
| `test_hysteresis_removes_lod_oscillation_on_a_jittering_camera` | 98 #23 | measured: 494 → 3 transitions over 5×200 jittered frames |
| `test_pool_capacity_derives_from_the_gpu_budget` | 82 | capacity scales with the card, never uses 100% |

**A note on the hysteresis tests.** My first attempt asserted a behaviour I had
*assumed* (that a marginal nudge would be held). It failed, and the reason is
that the selector walks coarsest-first and keeps the first candidate that fits,
so a monotonic sweep is unaffected by hysteresis. I replaced the assumption with
a measurement against the real selector, including the jitter scenario in which
the effect is dramatic and reproducible.

**Whole suite:** `732 passed, 11 failed, 6 collection errors`.
The 11 failures and 6 errors are byte-identical to the documented pre-existing
baseline (GDAL/`osgeo` and `plugins.naksha_converter` are absent in this
environment). **delta: +21 passing tests, +0 failures, +0 errors.**

---

## 4. REAL-DATA MEASUREMENT (the headline result)

`accept_incremental_upload.py` (new) runs the real cache
`test_classified_highprecision.laz` (26,960,750 points, 394 blocks) through the
real `_reconcile` / `_flush_gpu` path, twice: once with stable slots, once with
the arena disabled so the whole-buffer repack is restored.

```
                         stable-slot arena     whole-buffer repack
position points written        27,560,102            ~121,000,000
whole-buffer rebuilds                   0                       6
existing points re-uploaded             0                     n/a
15 camera-only frames: +0 pts          +0                      +0
resident slots that moved               0                       0
slots disjoint                       True                    True
ACTIVE vs RESIDENT     26,960,750 pts / 393 blk  vs  27,260,426 pts / 394 blk
arena capacity                 28,835,840 points (from a 1200 MB budget)
```

**≈4.4× fewer position bytes moved** for the same scenario, and the arena total
is O(points that actually became resident) while the repack total is O(resident
points summed over every change). All 11 checks PASS, including
`ACTIVE_DRAWN is a strict subset of GPU_RESIDENT`.

Other harnesses on the same real cache:

- `proof_overview_retire.py` — **HOLE cells = 0** at every stage (integer, zoom
  in/out, Fit). The never-blank / coverage-atomic invariant survives the change.
- `accept_stored_normal_shaded.py` — **11/11 PASS**, on both paths.
- `accept_lod_density_budget.py` — 8/9 PASS. The one FAIL
  (`illegal persistent overlap == 0`) is **pre-existing**: it reproduces
  byte-identically with the arena disabled.
- `accept_refine_trace.py` — crashes on a **pre-existing harness bug**:
  it monkeypatches `_retire_superseded` as `def retire():` while the production
  call site is `self._retire_superseded(specs=specs)`. Not caused by this work.
- `accept_live_frame_tick.py` — 8/9 PASS; the FAIL
  (`live budget uses real viewport pixels`, `target_points == 0`) is
  **pre-existing** (A/B identical).

---

## 4b. PHASE 3 — IN `FULL_RESIDENT`, THE FRAME IS BUILT FROM THE SCREEN

### 4b.1 The defect

`ScreenSpaceLOD` already existed and was already used — but only on the
streaming frontier. In `FULL_RESIDENT`, `_build_ranges` had no screen-space path
at all: it drew every `GPU_RESIDENT` LOD0 tile. So the one mode where every
candidate LOD is *already hot* — the mode where a screen-space selection is free
— was the mode that drew the whole residency.

### 4b.2 The integration (Part 5: reuse the gate, do not fork it)

| piece | role |
|---|---|
| `_ResidentIndexView` + `_resident_lod_nodes()` | `idx.nodes` with every **non-resident** LOD's `lod_point_count` zeroed. `ScreenSpaceLOD` already treats a zero count as "this LOD does not exist", so masking is the entire integration: no gate math, ordering, budget pass or never-blank floor is reimplemented. Cached on the resident key set — a camera move cannot invalidate it. |
| `_screen_space_active_keys(interaction, camera, viewport)` | pushes the one `ScreenDensityBudget` into the gate, asks it for the frame, then runs a **coverage check**. If any visible node got no resident representative it returns `None` and nothing changes. The new path can therefore only ever fall back to the old behaviour — it cannot introduce a hole (Part 43). |
| `_build_ranges(..., screen_space=, camera=, viewport=)` | the gate's answer becomes `ACTIVE_DRAWN`. |
| `_flush_gpu` | passes the screen-space flag, and dedups repeated draw-range pushes on the sealed/settled signature only. Streaming still re-asserts. |

### 4b.3 The resident coarse pyramid (Parts 16/47/68) — and why it is now allowed

`FULL_RESIDENT` preloaded **only LOD0**. At a Fit view there was therefore no
coarse representation on the GPU *to* choose: the gate's never-blank floor had no
alternative but LOD0 for every visible node, which is why the first run measured
a **1.0×** reduction — draw-the-screen and draw-the-residency were identical.

`_coarse_specs()` now also requests every block with `lod > 0`. Those tiles are
`pinned`, and pinned tiles are excluded from `_retire_superseded`'s coverage union
and from both eviction loops.

This was previously **tried and rejected**, and the code says so: the
docstring in `_keep_overview_for_interaction` records that preloading the coarse
blocks was abandoned because re-laying the packed buffer **moved the LOD0 offsets
and misaligned index-bound class/intensity**, and made idle *worse*
(28.5M points over 1338 calls, 9.8 s → 206 s). Stable per-tile slots (Phase 2)
remove that obstacle: a coarse block gets its own slot and no other tile's bytes
move. Retro-fitting the pyramid without Phase 2 would have reintroduced exactly
that bug.

### 4b.4 Two defects found *while verifying* Phase 3

Both were invisible to the existing tests, and both are the same class of
failure the mission's Part 87 exists to catch: **a feature reporting itself as
active while silently not running.**

1. **The camera was hidden state.** `_screen_space_active_keys` read
   `self._last_camera`, which only `on_frame` sets. Any caller holding a definite
   camera — the acceptance harness, and any future async seam — got the legacy
   "draw the residency" answer while `_screen_space_applies()` still returned
   `True`. The first A/B run measured **1.0×** for this reason and was reported
   as the feature being useless. Fixed by threading `camera`/`viewport` as
   arguments, and by recording `last_screen_space_reason` and
   `screen_space_fallbacks` so "the gate answered" and "the gate was never
   asked" are distinguishable without reading private state.
2. **The Phase-5 legacy rule was second-guessing the gate.** Even after the gate
   answered, `_build_ranges` still ran `fine = [k for k in keys if k[1] <= 0]`
   over the gate's *own* answer, silently discarding **195 of 230** selected
   blocks (recorded as `legacy_overrode=True`). The gate emits exactly one
   representative per visible node and already applies the interaction density
   budget, so re-filtering it can only delete coverage it chose deliberately.
   `_screen_space_authoritative()` (default **on**;
   `NAKSHA_SCREEN_SPACE_GATE_AUTHORITATIVE=0` restores the old behaviour for
   A/B) makes the gate's answer the submission **on the gate path only** —
   streaming is untouched, and `last_screen_space_diag` now carries
   `gate_blocks`/`gate_points` (proposal) separately from
   `submitted_blocks`/`submitted_points` (what was drawn).

### 4b.5 Measured — real data, real `on_frame` path

`test_classified_highprecision.laz`, **26,960,750** points, 1339 index blocks,
1920×848, strict headless adapter, no GPU.

| measurement | result |
|---|---|
| A/B at Fit, same camera — *draw the residency* | 393 blocks / **26,960,750** pts |
| A/B at Fit, same camera — *draw the screen* | 230 blocks / **1,726,892** pts |
| **reduction** | **15.6×** (was 1.0× before the pyramid) |
| ACTIVE_DRAWN LOD mix at Fit | `L0:35 L1:42 L2:46 L3:107` — the coarse pyramid is genuinely selected |
| idle points per zoom (Fit→×16) | 1,726,892 → 1,665,594 → 1,636,662 → 1,619,433 → 1,621,303 (target 1,628,160) |
| idle ppp per zoom | 1.061 → 1.023 → 1.005 → 0.995 → 0.996 |
| MOVING vs IDLE, same camera | **683,657** vs **1,726,892** → **0.396** |
| resident, every row | **constant** 28,860,837 pts — the camera path never re-reads or re-uploads it |
| 30 settled camera frames | **0** positions uploaded, **0** whole-buffer operations |
| coverage holes, every level | **0** |
| gate derived / fell back | **49** real derivations, **119–133** cache hits, **1** fallback (the pre-camera open) |
| settled frame vs changing frame | **3.8 ms** vs **11.7 ms** per `on_frame` (3.1×) |

Harness: `accept_screen_space_active.py` — **15/15 PASS**.

### 4b.6 The hot-path cost, and the one cache that removes most of it

The selection is a **pure function of the view, the resident LOD set, the
interaction state and the budget**. Despite that, it was being re-derived on
every flush — 5–11 ms at Fit, spent on the camera hot path, to produce a
byte-identical answer. `_active_screen_sig` existed for this and was **dead**
(nothing read it). It is now wired to the existing rounded `_camera_signature`,
so a settled view cannot miss a real change, and the BUDGET and ERROR TARGET are
part of the key — a `frame_budget` controller that lowers the budget with a still
camera must invalidate the cache, or the frame budget would silently stop working
until the user happened to pan.

Measured across harness runs: **119–133 cache hits against 49 real derivations**
— ~70% of gate calls removed. The range is stated rather than a single figure
because the number of SETTLING frames that reach the selection varies slightly
with wall-clock timing; the 49 derivations are stable. A settled frame costs
**3.8–4.3 ms** vs **11.0–11.7 ms** for a changing one.

A nuance worth stating rather than hiding: the 40-frame settled loop added *zero*
cache hits, because a sealed camera-only frame never reaches the selection at all
— the seal already short-circuits earlier. The cache's wins come from SETTLING
frames that still flush. Both mechanisms are doing the same job at different
stages, and the harness now says which is which instead of conflating them.

### 4b.7 What Phase 3 does *not* prove

- Still headless. No GPU, no rendered frame, no FPS, no input-to-present latency.
- A *changing* frame still costs **11.7 ms** of pure-Python `on_frame` for 230
  visible nodes at Fit — that is a 16.67 ms budget, at 27M points, with no GPU
  work in it at all. It scales with *visible nodes*, not with total points (the
  right shape), but 11.7 ms of CPU per frame is not a 60 FPS hot path, and it must
  be profiled and cut before 100M is attempted. The cache removes the repeated
  cost; it does nothing for a genuinely moving camera.
- MOVING lands at **1.2×** its 569,856 target, not under it: the gate's
  never-blank floor keeps one representative per visible node. That is the gate's
  documented design, not a Phase 3 regression, but it means the interaction
  budget is a soft cap.
- The pinned pyramid spends VRAM on coarse blocks in `FULL_RESIDENT`
  (28,860,837 resident vs 26,960,750 source). On a 4 GB card that is a real cost
  that has not been measured against a real budget.

---

## 5. WHAT IS *NOT* PROVEN

Being explicit, because the mission's Part 95 forbids the alternative:

- **No live GUI run.** No frame was rendered, no pixel was read back, no window
  was created. Every number above is from headless runs of the decision pipeline.
- **No FPS, no frame time, no input-to-present latency.** Parts 85 and 99's
  CAMERA section are **NOT MEASURED**.
- **No 1B dataset.** The largest real dataset used here is 26.96M points. Gates
  B/C/D/E (100M/250M/500M/1B) are **NOT RUN** and are not claimed.
- The real-data harness legitimately selects `FULL_RESIDENT` for this 27M
  dataset. The stable-slot path was therefore proved against a resident set that
  *fits* VRAM. The behaviour under sustained eviction pressure at billion scale
  is unverified.
- 24 B/point for positions (float64 world XYZ) is the current cost. Part 36/37
  (block-local quantized coordinates) is untouched, so the working set is ~2×
  larger than a quantized representation would allow.

---

## 6. PHASE STATUS (Part 100)

| Phase | Status |
|---|---|
| 1 — GPU_RESIDENT != ACTIVE_DRAWN | **DONE** (explicit `active_draw_keys`, tested) |
| 2 — stable block allocation, no whole-buffer repack | **DONE** (arena, tested, 4.4× measured) |
| 3 — FULL_RESIDENT uses ScreenSpaceLOD | **DONE** — the gate is now the submission in `FULL_RESIDENT`; 15.6× at Fit, mixed LODs, 0 holes, 7 new tests. 15/15 on `accept_screen_space_active.py` |
| 4 — universal event coalescing | Pre-existing (`_camera_only_frame`, camera signature); `_note_camera` records only, selection runs at frame cadence. Not extended into a scheduler |
| 5 — adaptive frame-budget / PPP controller | `FrameBudgetController` and `ScreenDensityBudget` both drive the selection (the gate's budget is now viewport-derived, and `apply()` is what limits the submission). **Not yet closed-loop against measured frame time** — there is no GPU to measure it on |
| 6 — bounded async read/decode/upload scheduler | **NOT DONE** (executor is a plain 2-worker pool) |
| 7 — RAM hot cache + GPU eviction scoring | Eviction exists (byte budget + priority); no LRU-plus-relevance scoring |
| 8 — predictive pan/zoom prefetch | **NOT DONE** (no camera velocity model) |
| 9 — quantization / copy reduction | **NOT DONE** |
| 10-14 — scale gates 29M / 100M / 250M / 500M / 1B | 29M-equivalent only, headless, no GPU. **100M+ NOT RUN** |
| 15 — billion-scale native Shading | **NOT DONE** for billion scale; per-tile normal streaming is in place |
| 16 — `.nakshasurf` + billion-scale Surface | **NOT STARTED** (no `.nakshasurf` is written) |
| 17 — GPU-driven indirect rendering | **NOT STARTED** (correctly deferred) |

---

## 7. FILES TOUCHED

| file | change |
|---|---|
| `gui/naksha_cache/stream_manager.py` | `GpuArena` import; arena/slot state; `_arena_active/_ensure/_target_capacity/_alloc/_release_slot/_evict_for/_flush`; `_release_gpu_bytes`; `_record_arena_telemetry`; `active_draw_report`; `_reconcile` repack gated; `_flush_gpu` arena branch; `_build_ranges` records ACTIVE_DRAWN; per-tile normal accounting; `ResidentTile` slot fields |
| `gui/naksha_cache/stream_renderer_adapter.py` | real `NAKSHA_GPU_ARENA` kill switch on the headless twin; per-tile normal + resident accounting in the arena methods |
| `tests/test_stable_gpu_arena.py` | **new** — 28 tests (21 in Phase 1/2, 7 in Phase 3) |
| `accept_incremental_upload.py` | **new** — real-data A/B measurement |
| `accept_screen_space_active.py` | **new** — Phase 3 real-data acceptance (15 checks) |

Phase 3 additions to `stream_manager.py`: `_ResidentIndexView`; `_resident_lod_nodes`;
`_screen_space_applies`; `_screen_space_authoritative`; `_note_camera`;
`_screen_space_active_keys`; `_resident_pyramid_enabled`; `_coarse_specs`;
`ResidentTile.pinned`; screen-space state (`_last_camera`, `_last_vp`,
`_screen_nodes_cache`, `_last_draw_sig`, `last_screen_space_diag`,
`last_screen_space_reason`, `screen_space_fallbacks`, `_active_screen_keys`,
`screen_space_cached_hits`); `_build_ranges`
`screen_space`/`camera`/`viewport`; `_flush_gpu` pass-through + push dedup;
`_seal_camera_only` camera pass-through; `load_full_resident` coarse specs;
`_reconcile`/`_retire_superseded`/`_arena_evict_for` pinned handling;
`lod_budget_telemetry` now reports **DRAWN** (`active_points`/`active_blocks`)
separately from `resident_points`/`resident_blocks`.

`gui/naksha_cache/` is entirely untracked in git, so none of this is committed.

---

## 8. THE SHORT VERSION

The two foundational defects named in the mission audit are fixed and measured:

- **`GPU_RESIDENT` no longer means `DRAWN`** — ACTIVE_DRAWN is a separate,
  explicitly recorded set, and a LOD change provably uploads nothing.
- **`np.concatenate(all resident tiles) -> upload_resident(...)` is gone from the
  interaction path** — a tile gets a stable slot once and keeps it, so an arriving
  block costs one block.

Measured on real data: **≈4.4× fewer position bytes moved** for a zoom ramp, with
`whole_buffer_rebuilds == 0`, `existing_points_reuploaded == 0`, zero camera-only
uploads, and **zero coverage holes**.

Phase 3 makes the mode that holds everything *draw only the screen*: 15.6× fewer
points at Fit, mixed LOD levels selected out of a resident coarse pyramid, zero
holes, and zero camera-driven uploads.

That is necessary for 1B and sufficient for none of it. The largest real dataset
touched is still 27M points, and the selection itself costs 5-11 ms per frame in
pure Python - on the hot path, and the next thing to fix. Everything from Phase 6
onward - the async scheduler, the eviction policy, predictive prefetch,
quantization, and every gate above 29M - is still ahead, and the camera hot path
has not yet been timed against a real GPU.
