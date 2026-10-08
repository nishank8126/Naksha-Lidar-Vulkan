# ROOT CAUSE — visible point-cloud regions disappear during pan/zoom

Scope: the cache **build** path only. No change was made to `stream_manager`,
`ScreenSpaceLOD`, event coalescing or the frame-budget controller.

---

## 1. Three defects, all in cache construction

### DEFECT A — `format.morton2d` did not compute a Morton code

```python
code |= (ix >> np.uint64(b)) << np.uint64(2 * b)      # shifts the WHOLE value
code |= (iy >> np.uint64(b)) << np.uint64(2 * b + 1)
```

`ix >> b` is not "bit b of ix". The loop ORs overlapping ranges together, so a
64×64 grid collapsed to **144 distinct codes with 3952 collisions**. Cells far
apart on the map received the same key.

Measured before the fix:

```
morton2d: mismatches 52/64 vs textbook interleave, distinct codes 144/4096
```

after the fix (`& np.uint64(1)` per step):

```
morton2d: mismatches 0/4096, distinct codes 4096/4096
```

### DEFECT B — `builder.shard_of` kept the LEAST significant bits

```python
side = (1 << n_shards_bits) - 1
return code & np.uint64(side)          # low bits = fine position, not a prefix
```

A shard id must be a Morton **prefix** (coarse high bits). Masking the low bits
selects a periodic lattice instead. On a uniform 1000 m test field, before/after:

| | shard 0 point count | shard 0 X span | shard 0 Y span |
|---|---|---|---|
| before (`code & 63`) | 122 | 15.6 m | **523.8 m** |
| after (`code >> 6`) | 3235 | 126.9 m | 127.0 m |

### DEFECT C — `spatialize` wrote the WRONG RECORDS into each shard

```python
order = np.argsort(shard, kind="stable")     # permutation
ss = shard[order]
...
for a, b in zip(starts, ends):               # a,b are SORTED boundaries
    sw.append(int(ss[a]), payload[a * SHARD_ITEMSIZE : b * SHARD_ITEMSIZE], ...)
```

`payload` is `rec.tobytes()` in **ORIGINAL FILE ORDER**, so the slice
`[a*I : b*I]` is a contiguous run of file order — not the points belonging to
that shard. Every shard file therefore contained points that did not belong to
its bucket, and PASS 2 subdivided file-order runs.

**Defect C is the direct cause of the visual bug.** It is independent of A and B:
even a perfect spatial key would have been defeated by it.

---

## 2. What the resulting index actually looked like

`diag_leaf_shape.py`, real `123.las` cache (the dataset in the bug report —
2,958,460 points, the "2.95M" in the report):

| property | value |
|---|---|
| nodes | 51 |
| leaf width min / p50 / max | — / — / **150.0 m** (survey is 150×150 m) |
| leaves wider than 30 m | 53 of 73 (rebuild intermediate) |
| sum(leaf XY area) / dataset area | **4.93×** |
| overlapping leaf pairs | many |

`test_classified_highprecision.laz` (27M, still on the old builder):

| property | value |
|---|---|
| nodes | 393 |
| leaf width min / p50 / max | 1.04 / 90.90 / **999.99 m** |
| sum(leaf XY area) / dataset area | **30.39×** |
| overlapping node pairs | **26,144 of 76,576** |
| X-slice coverage depth | 43–102 leaves covering every slice |
| visible nodes FIT → x32 (real viewport) | 146 → 40 (never culled) |

A "leaf" that spans the whole survey is not a tile. Screen-space LOD projects
those bounds, so `proj_px`, points-per-pixel, the screen error and the budget
share are all computed from a footprint that is not the real one — and a leaf's
coarse LOD is a sparse scatter over the entire survey rather than a local
sample, which is precisely why large source-covered regions rendered almost
empty while other rectangles stayed dense.

---

## 3. The fix

| file | change |
|---|---|
| `gui/naksha_cache/format.py` | `morton2d` interleaves one bit per step (`& 1`) |
| `gui/naksha_cache/builder.py` | `shard_of` takes the Morton **prefix** (`code >> n_shards_bits`) |
| `gui/naksha_cache/builder.py` | `spatialize` slices `rec[order[a:b]]`, not the flat payload |
| `tests/test_morton_spatial_key.py` | 9 regression tests |
| `rebuild_123.py` | rebuild of the user's dataset (preserves `.nakshaedit`) |
| `accept_coverage_occupancy.py` | source-supported occupancy acceptance |

Nothing in the rendering path changed.

---

## 4. Measured result — leaf geometry after rebuild (`123.las`)

| metric | before | after |
|---|---|---|
| nodes | 51 | 67 |
| leaf width min / p50 / max | — / — / **150.0 m** | 3.9 / 19.0 / **19.0 m** |
| sum(leaf area) / dataset | **4.93×** | **0.864×** |
| overlapping leaf pairs | >0 | **0 of 4422** |
| build time | — | 9.2 s (pass1 2.8 s, pass2 3.1 s) |
| `.nakshapc` | 86,479,672 B | 94,232,270 B |
| `.nakshaidx` | 50,512 B | 65,728 B |

`sum(area)/dataset = 0.864×` with **zero** overlapping pairs is a disjoint
tiling covering 86% of the dataset box — the remainder being genuinely empty
ground. That is the property every downstream culling and coverage decision
assumes.

---

## 5. Measured result — occupancy acceptance

`accept_coverage_occupancy.py <dataset>`: builds the source occupancy grid from
every leaf's LOD0 at 10 m, runs the production `ScreenDensityBudget` +
`ScreenSpaceLOD.select` with the bug report's real 1400×731 viewport, reads only
the tiles the selector returns, and rasterises what would reach the screen.

"Starved" = a source-covered cell holding **< 5% of its own source points** —
the numeric form of "that region looks empty".

### `123.las`, rebuilt with the fix

```
  zoom state    vis   sel_pts    ppp  LOD dist             exp  got miss starved  gap
  FIT  MOVING    28   353,483  0.345  L0:3 L1:25            84   84    0       6     4
  FIT  IDLE      28 1,007,553  0.985  L0:19 L1:9            84   84    0       4     4
  x2   MOVING    15   346,559  0.339  L0:4 L1:11            32   32    0       2     2
  x2   IDLE      15   956,669  0.935  L0:15                 32   32    0       0     0
  x4   MOVING     9   339,056  0.331  L0:4 L1:5              8    8    0       0     0
  x8   MOVING     1    94,452  0.092  L0:1                   2    2    0       0     0
  x16  MOVING     1    94,452  0.092  L0:1                   1    1    0       0     0

  largest connected EMPTY source region   : 0 cells
  largest connected STARVED source region : 4 cells (400 m²)
```

### `test_classified_highprecision.laz`, still on the old builder

```
  FIT  MOVING   146   355,770  0.348  L0:3 L1:48 L2:33 L3:62  2747 2742    5    2080  1939
  FIT  IDLE     146 1,022,998  1.000  L0:20 ... L3:60         2747 2744    3    1609  1533
  x2   MOVING    86   356,821  0.349  ...                     1092 1092    0     569   435
  x16  MOVING    40   356,078  0.348  ...                        18   18    0       0     0

  largest connected EMPTY source region   : 2 cells (200 m²)
  largest connected STARVED source region : 1,939 cells (193,900 m²)
```

**193,900 m² (≈19.4 ha) of continuously connected, source-covered ground
receiving under 5% of its own points in one FIT/MOVING frame.** That is the
reported screenshot, in numbers.

`miss` (cells with *zero* drawn points) was small on the broken cache because
the giant leaf bounds nominally cover everything — which is exactly why a
binary coverage check passed while the screen looked empty. The starved metric
is the one that matches the symptom.

**Honest caveat:** the two tables are different datasets. This is not a
controlled A/B; the `123.las` rebuild is the controlled before/after (4.93× →
0.864×, 150 m → 19 m leaves, 51 → 67 nodes).

---

## 6. Behaviour now correct on `123.las`

* **Visibility responds to zoom**: 28 → 15 → 9 → 1 visible nodes (was flat).
* **LOD refines with zoom**: FIT MOVING `L0:3 L1:25` → x2 IDLE `L0:15`.
* **Density tracks the budget**: MOVING ppp 0.345/0.339/0.331 against the 0.35
  target; IDLE 0.985/0.935 against the 1.0 target.
* **Zero empty cells in every frame of the ladder.**

---

## 7. Verification

| check | result |
|---|---|
| `py_compile` on edited files | OK |
| `pytest tests/ -q --continue-on-collection-errors` | **748 passed, 11 failed, 6 errors**, `PYTEST_EXIT=1` |
| new tests | `tests/test_morton_spatial_key.py` — **9 passed** |
| baseline failures | identical 11 (border_logic_routing, cross_section_line_tap, crs ×3, main_wheel_filter, shading_crisp_budget, shading_edit_planner ×2, shading_scene_ownership, view_fields_image) |
| collection errors | identical 6 (`osgeo`/GDAL, `naksha_converter`) |
| new regressions | **0** (748 = 739 baseline + 9 new) |

Baseline before this work was 739 passed / 11 failed / 6 errors.

---

## 8. Not done — stated plainly

1. **The 27M cache and `1234.laz` cache are still built with the broken code.**
   They must be rebuilt to benefit. The 27M rebuild additionally invalidates
   `test_classified_highprecision.laz.nakshanorm`, because `source_fingerprint`
   covers the committed NKIDX — the normal sidecar must be rebuilt too. Not done
   in this turn; it is the next required step.
2. **No GUI run after the rebuild.** The visual symptom is not yet confirmed
   fixed on screen. `diagnostics/live_pixels.py` is the pixel-level harness.
3. The **LOD ladder cliff** is untouched: coarse rungs jump by a large factor
   (measured earlier as 174,043 → 3,881 points between adjacent rungs), so a
   node still has no usable intermediate representation. That is a cache-build
   property and now lives in the same file as this fix.
4. `accept_lod_density_budget.py` still has the **`zoom shifts detail toward
   finer LODs`** failure introduced by the earlier `ScreenSpaceLOD` share pass
   — a separate, runtime defect, still open.
5. Nothing was staged, committed or pushed.
