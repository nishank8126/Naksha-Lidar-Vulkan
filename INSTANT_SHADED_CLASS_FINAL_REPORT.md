# Instant Shaded Class — Render-Ready Stream Requirement & Final Report

**Scope:** decide which render-ready streams NAKSHA's runtime cache must carry for an
*instant* Shaded-Class mode (points / splats / surfels vs tile-TIN), and whether a
**NORMAL** stream is required — before any ZSTD-1 codec is wired into the production
NKPC writer.

**Status:** Steps 1–2 complete with real, reproducible measurements. Steps 3–15
(splat/surfel prototypes, GPU lighting, 100-cycle mode-switch, pan/zoom/FIT x32
benchmarks, normal-generation cost, tile-dirty checks) require an **interactive
GPU/GUI session** and cannot execute in this non-interactive environment — they are
listed as the remaining programme, not as completed work. **No NKPC writer change, no
MANDI_56 rebuild, and no 1.14 B-point build were performed** (none were authorised).

---

## 1. Executive answer (the prescribed questions)

| Question | Answer |
|---|---|
| **Does the cache need a NORMAL stream?** | **YES — for any architecture that must shade without runtime triangulation.** Store `oct16x2` normals and render the surface as shaded points/splats: 0 ms render-time triangulation vs ~85 ms/150 K recompute, and ~6× cheaper than storing TIN topology. |
| **If YES, which encoding?** | **Octahedral `oct16x2` = 4.00 B/pt raw, 3.95 B/pt under ZSTD-1** (near-lossless, 0.001° mean). |
| **NKPC NORMAL raw bytes** | **4.00 B/pt** → **107.8 MB @ 26.96 M pts** |
| **NKPC NORMAL ZSTD-1 bytes** | **3.95 B/pt** → **106.5 MB @ 26.96 M pts** (ratio **1.01** — normals are near-incompressible) |
| **NAKSHASURF stream?** | **NO as a new per-point stream.** Surfel radius/colour are derivable at upload; storing them costs **+9 B/pt** (oct16x2 normal 4 + f32 radius 4 + class 1) ≈ **+10.3 GB @ 1.14 B pts** for no fidelity the base stream cannot already supply. |
| **Recommended architecture** | **Hybrid "stored-normal splats + resident positions/class"** (§5): base streams positions + class + SID; added `oct16x2` NORMAL stream gives TIN-quality shading with zero runtime triangulation. TIN topology is rebuilt only for editing. |

**Why NORMAL, and why now (the crux):**
1. **Fidelity** — the probe (§3) shows points-only PCA normals **do not** reproduce the
   TIN vertex normals on real classified LiDAR (mean direction error **31.8°**;
   **15.7°** flat vs **56.5°** steep). Deriving normals from geometry is not good enough.
2. **Cost** — deriving normals requires Delaunay topology, which is 92–95 % of the cost
   and takes **~85 ms / 150 K tile, 1.9 s / 3 M** (§3b). Recomputing per load is not
   "instant".
3. **Therefore** — store the normals (`oct16x2`, 4 B/pt) once, upload them, and shade
   points/splats directly. This is both **faithful** and **instant**.

---

## 2. Stream inventory actually consumed by Shaded Class (Step 1 trace)

Traced end-to-end so the cache contract is grounded in the real render path:

- `gui/shading_display.py` — builds the surface: `_do_triangulate` (scipy Delaunay on
  XY), `_compute_face_normals` (cross-product + *partial* hemisphere fix), and
  `_compute_vertex_normals` (area-weighted accumulation, normalised).
- `gui/render_backend.py` — passes a native GPU class colour LUT via
  `nkv_set_class_color_lut` and uploads positions; shading is applied per-vertex.
- Native Vulkan pipeline — `native/naksha_vulkan/src/SurfaceRenderer.cpp`,
  `.../naksha_vulkan_c_api.cpp`, shaders `surface.vert` / `surface.frag`.

**Streams required to shade a tile exactly as today:**
1. **POSITIONS** (XYZ) — mandatory (the only stream every mode needs).
2. **CLASS** — mandatory (drives both the class LUT and shading colour).
3. **SID** — already present (segment ids; compression honesty fixed earlier).
4. **NORMAL** — `oct16x2`, the enabler of instant shading (§3b): it lets the surface be
   shaded from positions alone with **zero** render-time triangulation. Store it.
5. **RGB16** — untested; **absent from both test datasets**, so it cannot be a hard
   dependency of instant Shaded Class (see §6).

---

## 3. Step 2 probe results — `shaded_normal_probe.py` (repeatable)

Run:
```
venv\Scripts\python.exe shaded_normal_probe.py --blocks 8
venv\Scripts\python.exe shaded_normal_probe.py --src "H:\TESTING CONTIUES\FUNIVIA\NT219\MANDI_56.laz" --blocks 8
```
The probe imports the **exact** runtime normal functions, so the reference normals
cannot drift. Reference = area-weighted vertex normal from `_compute_vertex_normals`.
Errors are sign-aligned (intrinsic plane direction) unless labelled "hemi".

### Q1 — Can points-only PCA normals replace TIN vertex normals?

`test_classified_highprecision` (REAL), 8 LOD-0 leaf blocks, 729,369 pts, spacing 0.37 m:

| method | P50 | P95 | max | mean |
|---|---|---|---|---|
| PCA k=6 | 24.58 | 83.76 | 90.00 | 32.82 |
| PCA k=10 | 22.86 | 83.33 | 90.00 | 31.91 |
| PCA k=16 | 23.05 | 82.98 | 90.00 | 31.79 |
| PCA k=24 | 24.13 | 82.81 | 90.00 | 32.07 |
| PCA radius = 2x spacing | 38.46 | 85.01 | 89.99 | 39.52 |

**Split by terrain flatness** (majority incident faces `|nz|>0.30`):

| subset | P50 | P95 | max | mean |
|---|---|---|---|---|
| PCA k=16 **FLAT** | 6.03 | 63.32 | 90.00 | **15.67** |
| PCA k=16 **STEEP** | 59.97 | 86.72 | 90.00 | **56.49** |

`MANDI_56` reproduces the same story (FLAT k=10 ~ 15.3 mean vs STEEP ~ 57;
overall ~ 35.6 mean).

**Reading:** the 31-36 overall figure is **entirely a steep-face phenomenon**.
On flat terrain PCA lands at ~15 mean (P50 ~ 6) but with a heavy rough tail; on the
~40 % near-vertical faces all estimators disagree ~57. This is expected:
`_compute_face_normals` flips only faces with `nz < -0.3`, so `|nz|<=0.3` faces keep a
near-random orientation and their area-weighted sum is ill-conditioned.

**Conclusion for Q1:** points-only PCA normals are **not** a drop-in replacement for
TIN vertex normals on structure-rich classified LiDAR. Terrain-only data would pass a
"<= ~2 mean / P95 <= 8" bar; classified LiDAR does not.

### Q2 — Encoding error vs the production vertex normal

| encoding | B/pt | P50 | P95 | max | mean |
|---|---|---|---|---|---|
| f32x3 | 12.00 | 0.000 | 0.000 | 0.000 | 0.000 |
| snorm16x3 | 6.00 | 0.001 | 0.001 | 0.002 | 0.001 |
| **oct16x2** | **4.00** | **0.001** | **0.002** | **0.004** | **0.001** |
| oct8x2 | 2.00 | 0.272 | 0.572 | 0.945 | 0.292 |

`oct16x2` is effectively lossless (0.001 mean) at 1/3 the size of f32x3. `oct8x2`
halves it again at a still-acceptable ~0.3 mean / 0.95 max - a viable size hero if the
size budget is tight.

### Q3 — Normal stream size (raw + compressed)

REAL, 729,369 pts (extrapolate linearly):

| encoding | B/pt | NONE MB | LZ4 MB | ZSTD-1 MB | ZSTD-3 MB | Z1 ratio | Z1 B/pt |
|---|---|---|---|---|---|---|---|
| f32x3 | 12.00 | 8.35 | 8.38 | 7.73 | 7.73 | 1.08 | 11.11 |
| snorm16x3 | 6.00 | 4.17 | 4.19 | 4.12 | 4.13 | 1.01 | 5.93 |
| **oct16x2** | **4.00** | **2.78** | **2.79** | **2.75** | **2.75** | **1.01** | **3.95** |
| oct8x2 | 2.00 | 1.39 | 1.40 | 1.28 | 1.28 | 1.09 | 1.84 |

**Key insight:** unit normals are already a high-entropy 2-DOF quantity. LZ4 and ZSTD
buy **almost nothing** (ratio 1.00-1.09). **The encoding *is* the compression.** The
NKPC writer should store the NORMAL stream as `oct16x2` and expect the per-block
fallback to select `NONE` for most normal blocks (no point paying a codec frame for a
~1 % gain).

**Extrapolated to the REAL cache (26.96 M pts):**

| stream | raw | ZSTD-1 |
|---|---|---|
| NORMAL oct16x2 | **107.8 MB** | **106.5 MB** |
| NORMAL oct8x2 | 53.9 MB | 49.6 MB |
| NORMAL snorm16x3 | 161.8 MB | 159.9 MB |

Base cache is 13.21 B/pt => base + oct16x2 = **17.16 B/pt**, still under the 18 B/pt
target; base + oct8x2 = **15.05 B/pt**. So a NORMAL stream is affordable **only** in
the compact encodings; f32x3/snorm16x3 would blow the budget.


## 3b. TIN-derivation cost (Step 3 cost gate) — `shaded_tin_cost_probe.py`

Measures the real cost of turning resident POSITIONS into a shaded surface, using the
production numba functions (REAL dataset, world coords, 3,068,802-point leaf pool):

| points | tris | Delaunay | face normals | vertex normals | **total** | ms / 1k pt |
|---|---|---|---|---|---|---|
| 150,000 | 299,980 | **78.9 ms** | 2.0 ms | 3.8 ms | **84.7 ms** | 0.564 |
| 1,000,000 | 1,999,941 | **555.7 ms** | 8.6 ms | 33.1 ms | **597.5 ms** | 0.598 |
| 3,000,000 | 5,999,842 | **1803.1 ms** | 20.6 ms | 80.0 ms | **1903.7 ms** | 0.635 |

**Reading — the decisive architectural fact:**

1. **scipy Delaunay is 92–95 % of the derivation cost.** The numba normal math is cheap
   (6 ms / 150 K, 54 ms / 3 M); building the topology is what hurts.
2. A 150 K tile costs **~85 ms** to derive — far too slow for the render thread, and a
   visible hitch even async per tile at higher sizes (1.9 s @ 3 M).
3. Therefore, for an *instant* renderer, **runtime triangulation must be avoided**.

**Consequence for the NORMAL stream (this flips the framing):** generating normals is
cheap *once topology exists*, but topology is the expensive part. So the compact way to
get TIN-quality shading with **zero** render-time triangulation is to **store the normals
(oct16x2, 4 B/pt) and render the surface as shaded points/splats** — no TIN indices
needed at display time. Storing the TIN topology instead would cost ~2N tris × 6 indices
× 4 B ≈ **~24 B/pt** (uncompressed), i.e. ~6× the normals.

| way to shade a tile | render-time cost | storage |
|---|---|---|
| recompute Delaunay + normals each load | ~85 ms / 150 K tile | 0 |
| store TIN indices, upload | 0 (just upload) | ~24 B/pt |
| **store oct16x2 normals, render splats/points** | **0 (just upload)** | **4 B/pt** |

The NORMAL stream is therefore not a luxury — it is the **enabler of instant shading**.


---

## 4. NAKSHASURF verdict — do we need a dedicated surfel stream?

A surfel needs position + normal + radius (+ class/colour). Compared with the base
stream + an `oct16x2` NORMAL stream, the *only* extra fields are radius and
per-surfel colour:

| field | bytes/pt | @ 1.14 B pts |
|---|---|---|
| oct16x2 normal | 4 | 4.56 GB |
| f32 radius | 4 | 4.56 GB |
| class/colour | 1 | 1.14 GB |
| **new over base+normal** | **~9** | **~10.3 GB** |

Radius is derivable at upload from tile density / local spacing (already the basis of
the adaptive splat radius work), and class already exists. **Recommendation: NO new
NAKSHASURF stream.** Keep surfel/splat radius + size as a *runtime* parameter, not a
stored stream — this keeps the format at positions + class + SID + NORMAL, which is what
every mode can share.

---

## 5. Recommended architecture

The brief's A/B/C/D options are not stored in the repo, so they are restated here in
functional terms and a recommendation is made:

- **A — Points/splats only, no normals.** Smallest cache (13.2 B/pt); shading derived
  from geometry per tile. **Rejected**: geometry-derived normals cost ~85 ms/150 K
  (Delaunay-bound, §3b) and are not faithful to the TIN look (§3). Not "instant".
- **B — Surfel.** Needs per-surfel radius/colour => **largest cache** (§4).
  **Rejected**: +~10 GB @ 1.14 B pts for marginal fidelity.
- **C — Tile-TIN streamed.** Reproduces the current look exactly, but must store or
  rebuild topology: ~24 B/pt to store TIN indices, or ~85 ms/150 K to recompute.
  **Rejected** on cost.
- **D — Stored-normal splats + resident positions/class (RECOMMENDED).** Base streams =
  **POSITIONS + CLASS + SID** (resident, ZSTD-1, ~13.2 B/pt). Add a **NORMAL stream,
  `oct16x2` (4 B/pt)**, so the surface shades as points/splats with **zero** runtime
  triangulation. TIN topology is rebuilt lazily *only* for editing (tile-dirty). Budget:
  **17.16 B/pt** (`oct16x2`) or **15.05 B/pt** (`oct8x2`) — both under 18 B/pt.

**Decision: D** — `oct16x2` as the NORMAL encoding, `oct8x2` held as the size-pressure
fallback. This is the option that is simultaneously **faithful** (TIN-quality normals,
stored once) and **instant** (no render-thread triangulation).

---

## 6. Open items / remaining programme (require interactive GPU session)

1. Steps 3–5 — shaded-point pipeline reusing resident positions; splat/surfel
   prototypes with adaptive radius 0.5/1/1.5/2/3 px + GPU 256-entry class LUT.
2. Steps 6–7 — GPU lighting via uniforms only; **instant mode-switch 100-cycle**
   benchmark.
3. Steps 8–14 — pan/zoom + FIT x32 benchmarks; visual quality vs the 53.8 M-tri TIN;
   normal-generation cost at 150 K/1 M/3 M pts + async tile strategy;
   classification / XYZ-edit tile-dirty checks; tile-TIN and hybrid comparisons.
4. **RGB16 stream is untested** — absent from both REAL and MANDI_56. It must remain an
   optional stream, never a dependency of instant Shaded Class.
5. **Not done (not authorised):** NKPC per-stream writer, MANDI_56 26.96 M rebuild,
   1.14 B build, GPU streaming LRU/cancellation, interactive T400 acceptance.

---

## 7. Artifacts & incidental fix

- `shaded_normal_probe.py` — new, repeatable Step-2 harness (Q1/Q2/Q3), importing the
  production normal functions.
- `shaded_tin_cost_probe.py` — new, repeatable Step-3 cost gate (Delaunay vs normal
  derivation at 150 K/1 M/3 M), production numba functions.
- `gui/naksha_cache/codecs.py` — fixed a latent bug: `versions()` had been scrambled by
  an earlier edit (returned `None`, with its body orphaned inside `per_block_fallback`).
  It now returns `{"zstd": "1.5.7", "lz4": "present"}`. Regression-checked:
  `compression_bench.py --blocks 4` => **0 mismatches across 40 combinations**.

---

## 8. Post-approval implementation plan (mandatory intermediate: 26.96 M GUI)

R&D is not the deliverable — the deliverable is the selected renderer **visible in the
real Naksha application**. The sequence is strictly gated:

```
R&D  ->  SELECT WINNER (this report)  ->  *** STOP FOR APPROVAL ***
     ->  implement selected renderer in real Naksha
     ->  visible 26.96 M REAL GUI acceptance (INTERMEDIATE GATE)
     ->  optimize  ->  compressed NKPC integration  ->  MANDI_56  ->  1.14 B
```

**Do not jump from R&D to 1.14 B.** The 26.96 M REAL GUI implementation is the mandatory
intermediate acceptance gate.

### Target user workflow (must be the real viewport, not a prototype)

1. `cd "H:\naksha-lidar 2"` → `py main.py`
2. Open `test_classified_highprecision.laz` → visible in the normal Naksha viewport.
3. Select **Classification** → then **Shaded Class**.
4. The real Vulkan viewport switches to the new instant renderer; pan / zoom / fit /
   2D / 3D / orbit / class-visibility / palette / lighting / classify+edit all work.
5. Switching **Classification ↔ Shaded Class** visibly uses the new implementation.

Must **not** be left as a standalone benchmark, CLI demo, hidden test window, isolated
prototype, or unused module — it must wire into the existing Display Mode / Shaded Class
UI.

### DEV overlay (visible development mode)

An optional overlay/status output showing which renderer is active:

```
Renderer:  VULKAN SHADED SPLAT / TILE-TIN / HYBRID
Dataset:   26,960,750
Visible points:            ...
Visible triangles/surfels: ...
LOD:                       ...
FPS:                       ...
RAM:                       ...
VRAM:                      ...
Position uploads:          ...
```

### Legacy fallback (side-by-side comparison)

Retain the old Shaded Class behind a DEV switch — `Legacy Shaded` | `New Instant Shaded`
— for visual comparison. Do **not** remove the legacy path until parity and stability
are accepted.

**STOP (approval gate).** No NKPC writer integration, no MANDI_56 rebuild, and no 1.14 B
build has been performed. Implementation begins only on approval of architecture **D**.

### Final handoff deliverables (produced at the end of the implementation phase)

- COMMAND: `cd "H:\naksha-lidar 2"` then `py main.py`.
- Exact steps: Open `test_classified_highprecision.laz` → **Display Mode → Shaded Class**
  → expected: new instant Vulkan shading appears in the main viewport.
- How to enable **DEV telemetry** (renderer/points/tris/LOD/FPS/RAM/VRAM/uploads).
- How to **switch Legacy ↔ New** renderer for side-by-side comparison.
- Where **performance logs are saved**.



