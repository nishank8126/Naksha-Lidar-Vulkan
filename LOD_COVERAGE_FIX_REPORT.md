# LOD COVERAGE / POINT LOSS FIX

Measured on the real 123.las cache (`diagnostics/data/123.las`, 170 blocks) at the
user's viewport (1400x731) plus the existing harnesses. Everything is a
measurement or is marked NOT MEASURED.

---

## ROOT CAUSE
```
selection            : DEFECTIVE - budget allocation was a PATCHWORK (FIXED)
activation           : no evidence of a defect (see SELECTOR OWNERSHIP)
handoff              : NOT THE CAUSE of the reported symptom
representative quality: DEFECTIVE - the LOD ladder between LOD0 and LOD1 is
                       45x, so a node that cannot afford LOD0 lands on a
                       representation that is invisible. NOT FIXED (cache build)
arena ranges         : no defect found (strict arena tests all pass)
camera generations   : no evidence of a defect
```

### exact proven defect

`ScreenSpaceLOD.select`'s budget pass kept the **highest-priority node at its
ideal, FINEST LOD** until the budget ran out, then collapsed **every remaining
visible node to the coarsest level it could afford**. Measured, one frame:

```
BEFORE (123.las, x4, MOVING, budget 358,190)
  LOD distribution     : L0:6 L3:1
  STARVED node=31  lod=0 pts= 69,640 proj=29,037,114px ppp=0.00240
  STARVED node=42  lod=3 pts=    296 proj= 7,838,955px ppp=0.00004
  nodes below 0.02 ppp : 4 of 7 (57.1%),  starved screen area 91.6%
```
**296 points in one cell and 69,640 in the next, in the same frame.** A few
hundred points spread over a cell-sized footprint IS an empty region on screen,
and WHICH cells were dense changed with the camera. That is the reported symptom
exactly: a few dense tile-like rectangles surviving inside large sparse/empty
source-covered regions, moving during pan and zoom.

Two further defects, both measured and both still open:

1. **The LOD ladder is too coarse between LOD0 and LOD1.** node 42:
   LOD0 = 174,043 points, LOD1 = **3,881** points - a **45x** jump. There is no
   intermediate representation, so a node either gets its full LOD0 (dense) or
   something invisible. This is a CACHE-BUILD property, not a runtime one.
2. **Node bounds are wrong or inflated for many nodes.** `proj_px` reaches
   **116,148,458 px for a single node** while the whole viewport is
   1,023,400 px - i.e. one node projecting to **113x the viewport**. From the
   index directly: node width min/median/max = 1.04 / 90.90 / **999.99 m** among
   393 "leaf" cells, and `sum(node areas) = 30.4x` the dataset area. The gate
   therefore mis-projects nodes, mis-computes `ppm` and the screen error, and the
   per-area budget share is computed from a footprint that is not the real one.

---

## SELECTOR OWNERSHIP
```
selector output keys       : list[dict], one per visible node; keys = node_id,
                             lod, points, proj_px, px_err, centre_dist, row
selector point count       : before the earlier fix the density counters were
                             never fed on this path
selected_points telemetry  : WAS a TELEMETRY-ONLY defect
active_draw_keys writer    : NakshaStreamManager._build_ranges (single writer)

active draw source         : SELECTOR
   proof: the user's own log shows active_blocks ~= 50 with
   submitted_points ~= 360K at a 358,190 target. The only other source,
   _interaction_lod_keys, returns the single coarsest resident block
   (~299,676 pts, 1 draw call) - it cannot produce 50 blocks. 50 blocks at a
   target-matching point count is only reachable through the gate's budget pass.

draw range source          : _build_ranges -> stable arena slots -> native
                             vkCmdDraw per range (command re-record only)
PASS / FAIL                : PASS (the selector owns rendering)
```
```
selected_points before     : 0     (every frame, while active selection existed)
selected_points after      : 1,349,244   (27M, real GUI, IDLE)
                             623,752     (27M, real GUI, MOVING)
                             1,349,244 was verified in the GUI log, not only in a
                             unit test.
```
`selected_points=0` was telemetry only. It is fixed. The visual defect is a
separate, real defect in the budget pass.

---

## MOVING  (123.las, 1400x731, budget 358,190)
```
target points              : 358,190
selected points            : 358,181   (1.000x target)   FIT, after fix
submitted points           : 358,181
LOD distribution           : L0:15 L1:32 L2:7            FIT, after fix
per-node ppp               : min 0.0012  median 0.0259  max 0.5662
nodes below 0.02 ppp       : 22 of 54 (40.7%)
expected cells             : NOT MEASURED
selected missing           : NOT MEASURED
active missing             : NOT MEASURED
PASS / FAIL                : FAIL - the point count is on target and the
                             patchwork is much better, but 40.7% of visible node
                             area is still below the visible-density threshold,
                             so "0.35 PPP spatially distributed over ALL visible
                             source coverage" is NOT yet achieved.
```

## IDLE  (123.las, 1400x731, budget 1,023,400)
```
target points              : 1,023,400
selected points            : 1,022,463   (0.999x target)   FIT, after fix
submitted points           : 1,022,463
LOD distribution           : L0:24 L1:29 L2:1            FIT, after fix
per-node ppp               : median 0.0484
nodes below 0.02 ppp       : 11 of 54 (20.4%), starved screen area 24.6%
expected cells             : NOT MEASURED
active missing             : NOT MEASURED
PASS / FAIL                : FAIL for the same reason as MOVING.
```

## PAN
```
largest missing connected region before : NOT MEASURED (no connected-region
                                          metric existed; the new diagnostic
                                          measures starved AREA, not connectivity)
largest missing connected region after  : NOT MEASURED
black/sparse gaps                       : 91.6% of visible node area starved
                                          before the fix at x4 MOVING
                                          54.1% at FIT MOVING after the fix
PASS / FAIL                             : FAIL - improved, not resolved
```

## ZOOM
```
parent/child gap frames    : NOT MEASURED
PASS / FAIL                : FAIL - the LOD0 -> LOD1 ladder gap (45x on node 42)
                             means zoom-in cannot produce an intermediate, and
                             zoom-out from LOD0 to LOD1 loses 45x of detail in
                             one step. This is the cache-build defect.
```

## ARENA
```
range overlap              : strict arena tests 28/28 PASS; no overlap in the
                             arena itself
wrong tile range           : none found
stale range                : none found
ILLEGAL persistent overlap : 15/15/16/16/16 across FIT..x16
                             (was 19/22/24/24/24 before the fix - IMPROVED,
                             still not zero)
PASS / FAIL                : FAIL on the pre-existing illegal-overlap check;
                             PASS on arena integrity
```

## CAMERA EVENTS
```
raw callbacks            : NOT MEASURED this turn (the earlier 123.las GUI log
                           shows camera_modified / main_camera_pan storms at
                           millisecond spacing, with camera_generation climbing
                           1 -> 108 over 9 steps)
unique signatures        : NOT MEASURED
selector executions      : 49 real derivations + 119-133 cache hits (27M headless)
frontier commits         : NOT MEASURED
```

## PERFORMANCE
```
MOVING submitted              : 358,181   (123.las headless, 1.000x target)
IDLE submitted                : 1,022,463 (123.las headless, 0.999x target)
XYZ upload during camera      : 0
whole buffer rebuild          : 0
```
No performance regression: the change is inside the gate's budget pass and adds
no uploads, no allocations and no whole-buffer work.

## GUI VISUAL
```
user-style missing-region reproduction : NOT FIXED / NOT RE-VERIFIED
```
The fix is not re-verified in the real GUI. The last GUI runs predate it. The
headless diagnostic that reproduces the user's pattern
(`diag_frontier_uniformity.py`) shows the patchwork is much reduced but not gone.

## TESTS
```
passed                    : 739
baseline failures         : 11
new failures (pytest)     : 0
new failure (acceptance harness) : 1
   accept_lod_density_budget.py : "zoom shifts detail toward finer LODs"
   (asserts LOD-weighted mean at the closest zoom < the mean at Fit)
```
This is a REAL new failure introduced by the allocation change and it is NOT
papered over. The illegal-overlap number improved (19-24 -> 15-16) and the
296-point node became 3,881 points, but the LOD-weighted-mean gradient the
harness measures no longer holds. `accept_stored_normal_shaded.py` still PASSES.

Reverting the allocation change is a one-file, one-hunk revert in
`naksha_lod_gate.py` (`select`, the second pass) if the user prefers the previous
behaviour over the improvement while this is resolved.

## FINAL
```
LOW-DENSITY MOVING MODE   : YES  (358,181 at a 358,190 target, 0.35 PPP)
FULL SPATIAL COVERAGE     : NO   (40.7% of visible node area below the visible
                                  density threshold while MOVING)
ATOMIC FRONTIER HANDOFF   : NOT IMPLEMENTED (no coverage-proof-gated
                                  parent/child commit exists)
NO POINT-LOSS DURING PAN  : NO   (improved, not proven)
NO POINT-LOSS DURING ZOOM : NO   (LOD0 -> LOD1 is a 45x detail cliff)
PHASE PASS                : NO
```

## WHAT MUST HAPPEN NEXT, IN ORDER
1. **Fix the LOD ladder** so LOD1 is not 45x coarser than LOD0 (for node 42:
   174,043 -> 3,881). Without an intermediate representation, no budget policy
   can keep a node visible at medium zoom. This is a cache-build change.
2. **Fix node bounds** in the index (one node projects to 113x the viewport).
   Until then `proj_px`, `ppm`, the screen error and the area-proportional share
   are all computed from a footprint that is not the real one.
3. Implement the coverage-first frontier with a coverage-proof-gated
   parent/child commit (the atomic handoff), so completeness never depends on the
   budget.
4. Re-verify in the real GUI with a pixel/connected-region coverage metric and
   re-run `accept_lod_density_budget.py` until both the overlap and the
   zoom-gradient checks pass.
