# MicroStation / TerraScan Ground Classification Parity Audit

**Project:** NakshaAI-LiDAR  
**Audit date:** 2026-07-28  
**Status:** Analysis only — no classifier code changes are authorized by this report  
**Primary comparison target:** TerraScan Ground routine running in MicroStation  

## 1. Executive conclusion

The Ground dialog being reproduced by Naksha is a **TerraScan** classification
routine hosted inside MicroStation. It is not a generic MicroStation algorithm.
Bentley Descartes Ground Extraction is a separate algorithm with different
parameters and a different final classification stage.

Naksha currently implements the same broad family of algorithm—local-low seed
selection followed by progressive Delaunay TIN densification—but it is **not yet
numerically or behaviorally equivalent to TerraScan**. Matching the visible
parameter values will therefore not produce matching classified points.

The most important findings are:

1. Naksha adds every candidate that passes the thresholds during an iteration.
   Classical progressive TIN densification normally controls candidate insertion
   per triangle or by a rating/order. Different insertion order changes the next
   TIN and all subsequent decisions.
2. Naksha's optional Class-0 density holdout deliberately changes a percentage
   of accepted ground points after classification. With this enabled, exact
   TerraScan output parity is impossible.
3. Naksha's **Use distance as rating** control does not use a stored point
   distance/vegetation/echo/deviation attribute as TerraScan does. It currently
   reuses perpendicular distance to the active triangle plane, which has a
   different meaning.
4. TerraScan's current **Follow surface trend** behavior is missing.
5. Naksha's Low Points preprocessing algorithm is not equivalent to TerraScan's
   Low Points routine.
6. Naksha rebuilds its production TIN from at most 400,000 ground points using
   order-based stride sampling. This changes the surface on large data and can
   make the result dependent on input point order.
7. Naksha's Auto-adaptive profile and Rolling/Hilly/Mountain tables are useful
   heuristics, but they are not TerraScan features and cannot be used in a strict
   same-settings parity test.

**Decision:** do not continue tuning terrain presets as the primary fix. First
build a controlled TerraScan-vs-Naksha comparison harness, correct the algorithmic
differences, and only then calibrate terrain profiles.

## 2. Product boundary: what is actually being matched

| System | Ground method | Main controls | Suitable data |
|---|---|---|---|
| TerraScan Ground in MicroStation | Iterative progressive TIN densification | Max building size, terrain angle, iteration angle/distance, small-triangle controls, surface trend, optional stored-distance rating | Airborne LiDAR and mainly natural terrain |
| TerraScan Hard Surface | Iterative dominant median/planar surface model | Plane tolerance, maximum slope, iteration angle, minimum detail/triangle | Mobile LiDAR, paved roads and mainly hard surfaces |
| Bentley Descartes Ground Extraction | Seed grid → iterative TIN refinement → final distance-to-TIN classification | Largest infrastructure size, terrain variation, max triangle edge, classification threshold | Bentley reality models and point clouds |
| Naksha current Ground | Vectorized progressive Delaunay TIN approximation | TerraScan-like controls plus Naksha profiles and Class-0 holdout | General point clouds |

The current Naksha dialog most closely matches **TerraScan Ground**, so that is
the correct initial parity target. Descartes output must not be used as a
TerraScan gold result; the final Descartes threshold stage alone makes the two
classifiers materially different.

## 3. Documented TerraScan Ground behavior

According to the current TerraScan guide:

1. One or more Low Points passes should normally be run before Ground because
   Ground is sensitive to low error observations.
2. Local-low seed points are selected using the Max building size search area.
   The assumption is that an area of that size contains at least one real
   ground return.
3. A sparse initial TIN is constructed from those confident ground seeds.
4. The TIN is progressively molded upward. A candidate is evaluated against its
   containing triangle using:
   - perpendicular distance to the triangle plane;
   - angle formed by the point, its plane projection, and the closest triangle
     vertex;
   - maximum permitted terrain slope;
   - optional small-triangle, surface-trend, point-attribute rating, and
     upward-only rules.
5. Accepted points alter the TIN, so candidate selection and insertion order are
   part of the behavior—not just an implementation detail.
6. Automatic output must still be quality-controlled, especially for pits,
   peaks, bridges and missing ground.

### 3.1 Official parameter meaning and terrain guidance

| Parameter | TerraScan meaning | Official guidance |
|---|---|---|
| Max building size | Search area used to find initial local-low seeds | Close to the edge length of the largest building |
| Terrain angle | Steepest slope permitted in the ground surface | 88–90° when man-made objects are present; for natural terrain, estimated maximum terrain slope +10–15° |
| Iteration angle | Primary density/eagerness control | Near 4° for flat terrain; near 10° for mountainous terrain |
| Iteration distance | Maximum point-to-triangle-plane distance | Normally 0.5–1.5 m; smaller values reject low objects more strongly |
| Reduce iteration angle | Reduces acceptance eagerness when all triangle edges are short | Controls density and memory |
| Stop triangulation | Stops processing inside sufficiently small triangles | Controls density and memory |
| Follow surface trend | Adapts selection to surrounding terrain | Usually improves natural terrain; may hurt photogrammetric data with smooth/inaccurate edges |
| Use distance as rating | Uses a precomputed stored distance value representing vegetation index, echo length or deviation | Positive values make ground acceptance less likely; Weight controls influence |
| Add only upward points | Adds only points above the initial/current surface | Protects against low observations |

There is no official universal Flat/Rolling/Hilly/Mountain parameter table. Only
the ranges and rules above are documented. A single "stick-on" parameter set
cannot maximize accuracy across natural mountains, buildings, dense vegetation,
bridges, cliffs and paved mobile scans.

## 4. Audit of the current Naksha implementation

The main implementation is in `gui/lidar_classification_tools.py`.

### 4.1 Parity matrix

| Behavior | Naksha status | Audit finding |
|---|---|---|
| Source, target and current-ground classes | Partial match | Supported. Active project class maps must be identical during comparison. |
| Fence-limited classification | Partial/unknown | Supported, but Naksha clips both candidate and current-ground influence to the fence. TerraScan's exact use of loaded ground outside the fence must be measured. |
| Local-low grid seeds | Approximate match | One minimum per bounds-anchored square cell. TerraScan grid origin, boundary and tie behavior are undocumented. |
| Existing ground as protected TIN influence | Broad match | Existing ground seeds are included and not reclassified. |
| Delaunay TIN | Broad match | SciPy/Qhull triangulation is used; diagonal/tie handling may differ from TerraScan. |
| Terrain-angle gate | Approximate match | Triangle slope is rejected above the threshold. TerraScan internal steep-terrain handling is not fully documented. |
| Iteration distance | Geometric match | Absolute perpendicular point-to-plane distance is tested. |
| Iteration angle | Geometric match | Uses the closest triangle vertex to the point's plane projection. |
| Candidate insertion policy | **Major mismatch** | Naksha adds all passing candidates in a batch before rebuilding. This can over-densify and changes later TIN decisions. |
| Reduce angle for small triangles | Approximate | Naksha applies a linear scale based on maximum edge length. TerraScan's exact reduction curve is not published. |
| Stop on small triangles | Broad match | Maximum triangle edge is used as the stop condition. |
| Follow surface trend | **Missing** | Current TerraScan behavior has no Naksha equivalent. |
| Use distance as rating | **Incorrect semantics** | Naksha weights geometric point-to-plane distance; TerraScan weights a stored vegetation/echo/deviation-derived attribute. |
| Add only upward points | Broad match | Signed plane distance is used to reject downward candidates. |
| TIN density control | **Non-parity postprocess** | Class-0 holdout changes already accepted ground. TerraScan's triangle density controls do not randomly or deterministically reclassify a ratio into Class 0. |
| Large-cloud processing | **Surface-changing approximation** | At more than 400,000 TIN points, Naksha uses stride subsampling rather than a spatially equivalent full/chunked TIN. |
| Neighbor block context | **Missing workflow parity** | TerraScan loads neighbor points around blocks for correct boundary classification. Naksha has no equivalent ground-classification halo. |
| Low Points preprocessing | **Major mismatch** | Naksha compares candidates against an existing ground-class reference; TerraScan compares a point/group against surrounding source points and is designed to run before ground exists. |
| Hard-surface workflow | **Missing** | TerraScan recommends a different routine for paved/mobile data and polygons when natural and paved areas coexist. |
| Stored point attributes | **Insufficient** | The loader retains XYZ, RGB, intensity and classification, but not the point attribute needed for true distance rating, nor the complete echo/deviation workflow. |

### 4.2 Code evidence

- `classify_low_points` starts at
  `gui/lidar_classification_tools.py:2409`. It requires or synthesizes a
  ground reference, unlike TerraScan Low Points.
- `_subsample_ground_tin_points` at line 2755 caps the active TIN at 400,000
  vertices and selects by array stride.
- the Naksha terrain presets begin at line 2765 and the 90th-percentile
  adaptive estimator begins at line 2789.
- `classify_ground_ptd` begins at line 2850.
- local-low seed cells are implemented at line 2892.
- all passing candidates are collected and added together around lines
  3031–3037.
- the current distance-rating equation is at lines 3026–3028.
- the Class-0 post-classification reassignment occurs at line 3056 and is
  enabled by default in the dialog around line 4397.
- Auto-adaptive is selected in the dialog around line 4529.
- `gui/data_loader.py:797–806` returns XYZ, RGB, intensity and classification,
  but no TerraScan-style stored distance rating channel.

## 5. Why identical visible values still produce different output

Progressive TIN classification is path-dependent:

1. initial seed selection defines the first surface;
2. the triangulation implementation determines triangle topology;
3. candidate rating/order determines which points enter next;
4. those accepted points change the next surface;
5. small changes compound over later iterations.

Consequently, setting both products to `terrain=88`, `angle=6`,
`distance=1.4` is necessary for a parity test but not sufficient for parity.
Seed locations, point ordering, candidate selection, triangle ties, boundary
context and optional feature semantics must also match.

The exact proprietary TerraScan tie-breaking, trend logic and reduction/rating
functions are not fully specified in the public guide. Bit-for-bit cloning
cannot be promised from documentation alone. It requires black-box differential
testing against a licensed TerraScan reference.

## 6. Fidelity versus ground-truth accuracy

Two targets must be measured separately:

### Target A — TerraScan fidelity

For the same input points, initial classes, fence and parameter values, Naksha
should classify nearly the same point IDs as the selected TerraScan version.
This tests product parity.

### Target B — survey accuracy

Both products should be compared with independently verified bare-earth ground
truth. TerraScan is a strong reference implementation, but it is not ground
truth and its guide explicitly expects manual correction.

A change may improve true terrain accuracy while reducing exact TerraScan
agreement, or vice versa. Release criteria must report both rather than treating
them as the same number.

## 7. Required reference package before implementation

Obtain the following from one controlled MicroStation/TerraScan run:

1. MicroStation edition/build and TerraScan edition/build.
2. Original unclassified LAS/LAZ used by both applications.
3. TerraScan-classified LAS/LAZ without subsequent manual edits.
4. Exact Ground dialog screenshot or exported `.MAC` action.
5. Low Points macro/settings and its intermediate classified output.
6. Point-class definition table.
7. Fence geometry, processing units and coordinate system.
8. Whether neighbor blocks were loaded and the neighbor distance.
9. Whether Follow surface trend, Add only upward, reduction/stop controls, or
   distance rating were enabled.
10. If distance rating was enabled, the source and values of that attribute.
11. A surveyed/manual truth subset for accuracy measurement.

If the reference was actually produced by Bentley Descartes Ground Extraction,
collect its Largest Infrastructure Size, Terrain Variation, Max Triangle Edge
and final Threshold instead. That requires a different Naksha implementation.

## 8. Proposed differential test dataset

The benchmark should include fixed, separately fenced zones:

1. flat open bare earth;
2. urban flat terrain with large roofs;
3. rolling natural terrain;
4. steep natural mountain slopes;
5. cliffs, retaining walls or quarry faces;
6. dense low vegetation and forest;
7. bridges and elevated road decks;
8. paved/mobile-scanner road data;
9. voids and block/fence boundaries;
10. known low noise and isolated error points.

Every output must preserve a stable point identity. Prefer original point record
index when file order is unchanged; otherwise use a collision-safe key built
from scaled integer XYZ plus GPS time, return number and point source ID.

## 9. Metrics and acceptance gates

For every terrain zone and for the complete file, calculate:

- exact class agreement percentage;
- TerraScan-ground precision, recall, F1 and intersection-over-union;
- over-classification: Naksha ground / TerraScan non-ground;
- under-classification: TerraScan ground / Naksha non-ground;
- disagreement counts by original class and height above the TerraScan TIN;
- 1 m and 5 m spatial heatmaps of disagreements;
- separate boundary-band metrics;
- seed-point agreement when TerraScan seeds can be exported;
- runtime, peak RAM and TIN vertex count.

Suggested engineering gates, to be finalized after the first baseline:

| Gate | Proposed target |
|---|---:|
| UI/parameter semantic parity | 100% |
| Overall binary ground/non-ground agreement | ≥98% |
| Ground F1 against TerraScan | ≥0.99 |
| Over-classification on buildings/vegetation | ≤0.5% of reference non-ground |
| Boundary disagreement | Report separately; no hidden exclusion |
| Determinism | Identical result on three repeated runs |
| Ground-truth vertical RMSE | No worse than TerraScan; target improvement reported separately |

Strict parity runs must disable Naksha Auto-adaptive thresholds and Class-0
holdout. The same fixed values must be passed to both products.

## 10. Recommended implementation sequence after approval

### Phase 1 — Build measurement before changing the classifier

- Create the point-identity comparison tool and zone reports.
- Record the current Naksha baseline.
- Add diagnostic export of initial seeds, each iteration's additions, triangle
  counts and rejection reasons.

### Phase 2 — Correct strict semantic mismatches

- Make a dedicated **TerraScan Compatibility** mode.
- Disable Class-0 holdout and automatic profiles in that mode.
- Correct Low Points to compare source point/groups with their surroundings.
- Remove or disable Use distance as rating until the proper stored attribute is
  loaded and computed.
- Implement Follow surface trend only after black-box tests define its effect.

### Phase 3 — Correct the PTD core

- Replace add-all batch insertion with a controlled per-triangle/rated candidate
  policy and test candidate ordering.
- Replace order-based TIN stride sampling with deterministic spatial tiling,
  halos and boundary reconciliation, or a scalable full-TIN strategy.
- Match neighbor/fence context and triangulation tie behavior as far as the
  reference outputs demonstrate.

### Phase 4 — Add data-specific workflows

- Implement Hard Surface classification for paved/mobile data.
- Preserve/load the additional LAS/TerraScan attributes required by rating
  workflows.
- Keep adaptive terrain profiles as a separate Naksha-assisted mode, calibrated
  against survey truth rather than presented as exact TerraScan values.

### Phase 5 — Calibrate and release

- Sweep documented parameter ranges on the benchmark zones.
- Publish separate parity and ground-truth accuracy reports.
- Lock regression fixtures and performance budgets for 32-million-point files.

## 11. Final recommendation

The next work item should be the **differential audit harness and gold reference
package**, not another threshold adjustment. Once one original file and its
TerraScan output are available, the first comparison will show whether seed
selection, iteration insertion, boundary context or preprocessing is the
dominant source of the observed disagreement.

No production classifier code should be changed until the TerraScan version and
gold dataset are fixed. Otherwise, changes may improve one screenshot while
reducing repeatability or accuracy in another terrain type.

## 12. Sources

- Terrasolid, **TerraScan Ground**, current guide dated 2026-06-25:  
  https://terrasolid.com/guides/tscan/crground.html
- Terrasolid, **TerraScan Low Points**:  
  https://terrasolid.com/guides/tscan/crlowpoints.html
- Terrasolid, **TerraScan Hard Surface**:  
  https://terrasolid.com/guides/tscan/crhardsurface.html
- Terrasolid, **Run a Macro on a Project / Neighbor Points**:  
  https://terrasolid.com/guides/tscan/marunonproject.html
- Bentley, **Descartes Ground Extraction**, article KB0012378:  
  https://bentleysystems.service-now.com/community?id=kb_article_view&sysparm_article=KB0012378
- Axelsson, P. (2000), **DEM Generation from Laser Scanner Data Using Adaptive
  TIN Models**, International Archives of Photogrammetry and Remote Sensing,
  Vol. XXXIII, Part B4, pp. 110–117.

