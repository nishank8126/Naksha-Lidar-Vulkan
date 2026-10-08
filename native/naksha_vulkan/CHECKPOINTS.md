# Naksha Vulkan Engine — Checkpoint Log

## Checkpoint 2 — Native surface tiling, LOD, culling, atomic swap

### Toolchain (Part 1) — nothing was missing; it was simply not on PATH

| Tool | Path |
|---|---|
| MSVC | `C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools` (14.44.35207) |
| `cl.exe` | `...\VC\Tools\MSVC\14.44.35207\bin\Hostx64\x64\cl.exe` |
| MSBuild | `...\2022\BuildTools\MSBuild\Current\Bin\amd64\MSBuild.exe` |
| CMake | `H:\tools\CMake_64\bin\cmake.exe` (3.30.5) |
| Ninja | `H:\tools\Ninja\ninja.exe` (1.12.1) |
| Vulkan SDK | `H:\tools` (`$env:VULKAN_SDK`) |
| glslc | `H:\tools\Bin\glslc.exe` |
| Generator | Ninja, `build_msvc/` tree, Release |

Build command (vcvars64 must be activated first, or `cl` is invisible):

```
cmd /c '"...\vcvars64.bat" >nul 2>&1 && set VULKAN_SDK=H:\tools && set PATH=H:\tools\Ninja;H:\tools\CMake_64\bin;%PATH% && cd /d "H:\naksha-lidar 2\native\naksha_vulkan\build_msvc" && ninja'
```

**Real build defect found and fixed in this checkpoint:** `naksha_vulkan_benchmark.exe`
failed to link with `LNK2019: unresolved external symbol main`. `tests/benchmark_main.cpp`
defines `WinMain`, but `CMakeLists.txt` set `WIN32_EXECUTABLE OFF`, which makes the linker
emit `/subsystem:console` and therefore require a `main`. Set to `ON` — the executable
subsystem flag is what has to match the entry point.

### What was added (all in `native/naksha_vulkan/`)

**`include/naksha/SurfaceTiling.hpp` + `src/SurfaceTiling.cpp`** — pure CPU, no Vulkan:

- `BuildSurfaceTiles()` partitions the triangle soup into ~N spatially coherent tiles by
  binning centroids on a uniform XY grid (counting sort, O(T)) and coalescing adjacent cells.
  The output index buffer is a **permutation** of the input: every triangle appears exactly
  once, so there are no duplicate triangles and no gaps, and each tile is one contiguous
  `[firstIndex, +indexCount)` run — exactly what `vkCmdDrawIndexed(firstIndex, indexCount)`
  needs with no rebinding.
- `ClusterSurfaceLod()` builds coarse levels by **grid clustering** (snap vertices to a
  cell lattice, merge each occupied cell to one shared vertex, re-emit only triangles whose
  3 corners survive in 3 *different* cells). Chosen over striding because a TIN has no
  independent triangles: striding punches holes, clustering keeps one shared vertex per cell
  so the coarse mesh is watertight with no cracks at tile boundaries.
- `ExtractFrustumPlanes()` / `AabbVisible()` — Gribb–Hartmann on the column-major MVP, with
  a positive-vertex AABB test. A degenerate (all-zero) MVP is **rejected** and falls back to
  draw-everything rather than culling the whole surface.
- `SelectLodForTile()` / `MakeProjectedScale()` — projected-error LOD: the coarsest level
  whose geometric error still fits inside a pixel budget, computed from real pixels-per-metre
  (exact for the VTK parallel/ortho camera, depth-scaled for perspective).

**`SurfaceRenderer`** — tiled/LOD resource sets with an atomic swap:

- `SurfaceResourceSet` owns one `SurfaceLodBuffers` per level (each level is a
  self-contained mesh because the clustered levels have their own compact vertex arrays).
- `UploadTiledMesh()` builds CPU geometry, then checks the **VRAM budget** (70% of the
  device-local heap) accounting for ACTIVE **and** PENDING coexisting, then uploads. A
  refused upload leaves the current surface untouched and reports why — no crash, no
  half-swapped state.
- `ActivatePendingIfFresh()` promotes PENDING → ACTIVE at a frame boundary, but only if the
  pending set's (dataset revision, surface revision) still match the current ones. A stale
  set is discarded through the normal retirement path. The outgoing ACTIVE set is
  **retired, never destroyed** at swap time.
- `RetireCompleted()` frees retired sets once `Renderer::IsFrameRetired()` proves their last
  submitted frame finished — backed by the per-slot in-flight **fences** the frame loop
  already waits on. **No `vkDeviceWaitIdle()` per swap.**
- `Record()` issues **one `vkCmdDrawIndexed` per visible tile** from the selected LOD. No
  upload happens here, so pan/zoom is pure CPU culling plus fewer draws.
- One LOD level is drawn per frame, not a per-tile mix: mixing levels whose tile partitions
  differ is exactly what produces T-junction cracks.

**`VulkanFrameManager`** gained a monotonic `frameCounter_` + `IsFrameRetired()`, which is
what makes fence-based deferred destruction possible at all.

### Bugs the new unit tests caught (all real, all fixed)

`tests/tiling_test_main.cpp` (`naksha_vulkan_tiling_test.exe`, 30 checks, no Vulkan/window):

1. **Tile range desync.** The coalescing loop recomputed the candidate cell as `cell + 1`
   instead of `iy*gx + (end+1)`, so it re-counted the same cell every iteration. `count`
   inflated past the triangles actually written, desynchronising `firstIndex` from the
   emitted range — tiles would have drawn the wrong triangles.
2. **`out.triangleCount` never assigned** in `ClusterSurfaceLod()`, so every coarse LOD
   reported 0 triangles and LOD selection silently fell back to the finest level.
3. **Frustum near/far orientation.** Corrected to Vulkan's `[0, w]` depth range.

`tests/surface_acceptance_main.cpp` (`naksha_surface_acceptance.exe`, 42 checks, real
window, **validation layer enabled**) caught two more:

4. **`visible tiles (176) > total tiles (170)`.** `GetTotalTileCount()` reported LOD0's
   tile count while `visibleTilesLastFrame` counted the *selected* level's tiles, and each
   LOD is re-tiled independently so the counts differ. Same class of bug for
   `GetTotalTriangleCount()` / `GetIndexCount()`, which reported a stale legacy
   `indexCount_` (0) on the tiled path. All three now read the level actually being drawn.
5. **`nkv_get_surface_memory` disagreed with the enforced budget** — it recomputed 70% of
   device VRAM instead of reading the renderer's live budget, so an override was invisible
   in the report. Now read from the renderer.

### Measured (visible Qt window, `test_classified_highprecision.laz`, 26,960,750 points)

| Mode | GPU frame | FPS | Notes |
|---|---|---|---|
| Point cloud | 20.57 ms | **47.6** | previous baseline was ~11–13 FPS |
| Surface NORMAL | 0.56 ms | **64.1** | 5,999,690 tris resident, LOD1 drawn |

Surface upload: 3,000,000 verts / 5,999,690 faces, `path=tiled`, 17.6 s, one atomic swap,
zero stale discards, peak 562.5 MB of a 2,759.4 MB budget.

Resident LOD levels: LOD0 5,999,690 → LOD1 587,195 (10.2×) → LOD2 48,477 (123.8×).

### Budget verdict for SLOW on this GPU (measured, not assumed)

`nkv_estimate_surface_bytes` on the T400 (3942 MB, budget 2759 MB):

| Quality | Triangles | Required | Budget | Verdict |
|---|---|---|---|---|
| NORMAL | 6,000,000 | 586 MB | 2759 MB | ACCEPT |
| SLOW | 53,800,000 | **5253 MB** | 2759 MB | **REFUSE** |

An uncapped SLOW surface cannot be made resident on a 4 GB T400 at any LOD: the final
level alone is 5.2 GB. The budget guard refuses the upload, keeps the current surface on
screen, and reports it — rather than failing a VMA allocation or thrashing. The
interaction LOD floor additionally guarantees SLOW geometry is never used for navigation
even where it would fit.

### Native acceptance evidence (`naksha_surface_acceptance.exe`, 42/42 PASS, 0 VUID)

| Check | Measured |
|---|---|
| Tiling | 96,800 tris → 8 tiles |
| LOD | LOD0 96,800 → LOD1 4,608 (21×) → LOD2 288 (336×) |
| Culling | whole view 8/8 tiles, 96,800 tris → zoomed 2/8 tiles, 24,200 tris |
| No upload on movement | **0** uploads across 12 camera moves |
| Movement floor | MOVING → LOD2 (288 tris); IDLE → LOD0 (96,800 tris) in 32 ms |
| Stale guard | surface-revision and dataset-revision mismatches both discarded, old surface stays ACTIVE |
| Atomic swap | FINAL_PENDING → FINAL_ACTIVE, 4 frames presented across the boundary, 0 blank |
| Budget | refusal fires, previous surface survives, identical mesh accepted once restored |
| Resize | resize + restore both keep presenting |
| Validation | **0 errors** with the layer enabled |

## Checkpoint 1 — Phase 1/2 accepted (Vulkan proof-of-life)

Canonical layout (do not rename without a concrete technical reason):
```
native/naksha_vulkan/
include/naksha/
namespace naksha::vulkan
```
Toolchain: CMake + Ninja + MinGW-w64 g++ 13.1 (H:/tools/mingw1310_64), Volk (vendored at
H:/tools/Include/Volk), VMA (H:\tools\Include\vma\vk_mem_alloc.h). NOT MSVC.
Known PATH hazard: git-bash puts Git's bundled MinGW64 DLLs ahead of
H:/tools/mingw1310_64/bin, causing STATUS_ENTRYPOINT_NOT_FOUND. Always prepend
H:/tools/mingw1310_64/bin to PATH for build/run commands.

Build status: PASS (zero errors, validation clean: 0 warnings / 0 errors on repeated runs).

Device: NVIDIA T400 4GB, Vulkan API 1.3.277. Hardware timestamp queries UNSUPPORTED
(validBits=0) — GPU-only frame time cannot be isolated on this hardware via timestamp
queries; needs an offscreen/fence-based path instead (next phase).

### Six bugs fixed this checkpoint (all in native/naksha_vulkan/, root-caused via gdb)
1. `VulkanAllocator::Get().Initialize(...)` was never called — VMA allocator handle was
   null, every `vmaCreateBuffer()` segfaulted. Fixed in `src/Renderer.cpp` (init + shutdown).
2. `frameSets_` (descriptor sets vector) was never resized before
   `vkAllocateDescriptorSets` read `&frameSets_[slot]` — fixed with `.resize(...)`.
3. Timestamp query pool was never reset before use (VUID-vkCmdWriteTimestamp-None-00830)
   — added `vkCmdResetQueryPool` per-slot + a `timestampsWritten_` guard.
4. `VkDeviceSize offsets[]` arrays undersized relative to bound-buffer count in
   `PointCloudRenderer::Record` and `SurfaceRenderer::Record` (out-of-bounds read by
   `vkCmdBindVertexBuffers`); `PointCloudRenderer::Record` was also missing
   `vkCmdBindPipeline` entirely. Both fixed.
5. `VulkanContext::Shutdown()` leaked `VkSurfaceKHR` and never explicitly shut down
   device_/instance_. Fixed.
6. `Renderer::Shutdown()` never called `vkDeviceWaitIdle()` before destroying
   pipelines/buffers/framebuffers/command pool/swapchain/sync objects still referenced by
   an in-flight command buffer ("still in use" validation errors). Added
   `context_.CoreDevice().WaitIdle()` (shutdown + swapchain-recreation ONLY, never
   per-frame). Also fixed a double-freed wireframe fragment shader module in
   `SurfaceRenderer::Shutdown()` and a benchmark-harness destructor-ordering bug.

Also fixed: `VulkanDescriptorManager.cpp` missing
`VK_DESCRIPTOR_POOL_CREATE_FREE_DESCRIPTOR_SET_BIT` (code called `vkFreeDescriptorSets`
without it). Benchmark harness `setvbuf(_IONBF)` fix so crash output isn't lost to
buffering.

### Benchmark numbers as of this checkpoint (presentation-paced, NOT raw/offscreen —
### superseded by Part 1 of the next phase)
Synthetic point clouds, one-time upload to persistent device-local GPU buffers via VMA
(staging → device-local copy), camera-only interaction never re-touches the buffer:

| Points | Upload (stage+copy) | Frame time | FPS |
|---|---|---|---|
| 100K  | 0.098ms | 15.80ms | 63.3 |
| 1M    | 0.95ms  | 15.81ms | 63.3 |
| 5M    | 6.69ms  | 15.82ms | 63.2 |
| 10M   | 12.4ms  | 15.62ms | 64.0 |

Frame time is flat across a 100x point-count range → presentation/vsync-limited, not a
real measurement of renderer throughput. This is exactly what the next phase's raw/offscreen
benchmark is meant to fix.

Surface mesh (synthetic sphere) at 100K/500K/1M/5M triangles: 62.7-65.0 FPS, shading-mode
cycling clean, zero validation errors. Not yet fed by real Naksha surface-generation output.

### Files touched this checkpoint
Created: `H:\naksha-lidar 2\gui\render_backend.py` (inert seam, not imported anywhere —
`VtkRenderBackend` delegates to existing production code; `VulkanRenderBackend` is a
documented placeholder pending the native C-ABI DLL).

Modified (bug fixes only, no redesign): `src/Renderer.cpp`,
`include/naksha/Renderer.hpp`, `src/PointCloudRenderer.cpp`, `src/SurfaceRenderer.cpp`,
`src/core/vulkan/VulkanContext.cpp`, `src/core/vulkan/VulkanDescriptorManager.cpp`,
`tests/benchmark_main.cpp`.

No other Naksha production file touched. WorkstationCAD: zero files modified (read-only
throughout, verified via git status / mtimes).

### Phase 0.5 research findings (read-only, confirmed with file:line citations)
- **Selection**: point-cloud selection (`select_rectangle_tool.py:1004`,
  `_highlight_selected_points`) draws a separate additive overlay actor named
  `'selection_highlight'`, never touches the base actor. Vector-drawing selection
  (`digitize_tools.py:7317`, `_highlight_line`/`_unhighlight_line`) does an in-place
  `actor.GetProperty().SetColor(...)`/`SetLineWidth(...)` mutation with cached
  `original_color`/`original_width` for restore. Two unrelated mechanisms — a Vulkan port
  needs both strategies.
- **Picking**: `app.data['xyz']` raw float64 values ARE the VTK world coordinates used
  directly for both camera projection (`select_rectangle_tool.py:482-500`) and
  `vtkWorldPointPicker` (`element_select_tool.py:1676`, `_screen_to_world`). No hidden
  intermediate transform layer — validates the floating-origin design as implemented
  (origin subtraction only on the GPU-bound render-space copy, `xyz` itself never mutated).
- **Data contract**: real production loader is `gui/file_loader_worker.py` (NOT
  `core/load_lidar_file.py`, which is an unused standalone tool with a different, float64
  rgb convention). Confirmed: `xyz` float64 (N,3) C-contiguous, `classification` uint8 (N,),
  `rgb` uint8 (N,3) optional, `intensity` float32 (N,) optional, `point_source_id` uint16
  (N,) optional. ~32 bytes/point core footprint → 100M points ≈ 2.98 GiB (add ~6.25% if
  point_source_id present).
## Checkpoint: black Vulkan viewport root-caused and fixed (real pixels proven)

**Symptom.** With `NAKSHA_RENDER_BACKEND=vulkan`, every call reported success
(`nkv_create_renderer` OK, point/surface uploads OK, `nkv_render` true, present counter
climbing) but the viewport showed only black: `vulkan_pane.png` = 1,808 B, pane std 0.0,
1 distinct colour. Nothing crashed, nothing logged an error.

**Diagnosis method.** A standalone probe (`vulkan_native_probe.py`, no Qt, no app) created a
plain Win32 window, bound the renderer to it, uploaded 200K synthetic points, and read the
pixels back through four *independent* capture paths. Two of them matter:
`BitBlt` from a window DC returns BLACK for a Vulkan flip-model swapchain, so the earlier
"black pane" measurement was partly a capture artifact; reading the DWM-composited desktop
(`BitBlt` from the screen DC, cropped to the client rect) is the honest readback.
An `nkv_set_clear_color()` to a colour used by nothing else (0.06,0.09,0.16 -> sRGB 69,85,111)
then became the discriminator: 100.00% of the client area turned exactly that colour, which
proved the render pass + present path were alive while the *draw* produced zero fragments.

**Three real bugs, all in the commit path:**

1. **View/projection matrices were transposed** (`src/core/renderer/CoreCamera.cpp`).
   `Matrix4d` is documented row-major with the COLUMN-vector convention `P' = M * P`, and
   GLSL `mat4` matches that; the projection and view were written in the row-vector layout.
   Effect: `w_clip` came out constant/negative - measured `w > 0` for **0 of 200,000** points
   (all clipped). Fixed by building both matrices in the documented convention
   (RH view space, Vulkan `[0,1]` depth, `w = -z_v = distance in front`). The projection's y
   row is now negated as well, because Vulkan NDC +y points down the framebuffer while
   VTK/OpenGL treat +y as up - without it the viewport renders vertically mirrored relative
   to the VTK view it must match. Verified: `w > 0` and NDC `z in [0,1]` for 200,000/200,000,
   190,588 fully inside the frustum; an asymmetric marker cloud at +x,+y landed
   right/top (x=0.71, y=0.12) => upright, not mirrored.

2. **Draws were recorded into a command buffer that was never submitted**
   (`src/Renderer.cpp`, `include/naksha/Renderer.hpp`). `BeginFrame()`/`EndFrame()` record and
   submit `commandBuffers_[imageIndex]` (swapchain image, 0..imageCount-1), but
   `CurrentCommandBuffer()` - the buffer `nkv_render()` records the draws into - returned
   `commandBuffers_[frameSlot]` (0..maxFramesInFlight-1). Those are different index spaces, so
   each presented frame contained the clear colour and nothing else. Fixed by tracking
   `currentImageIndex_` in `BeginFrame()` and returning that buffer.

3. **Missing VMA flushes on persistently mapped memory** (`src/Renderer.cpp`,
   `src/Buffer.cpp`). The frame UBO and the staging buffer are mapped for the renderer's
   lifetime, and VMA only flushes automatically on `vmaUnmapMemory`, so an explicit
   `FlushMapped()` is required or the GPU may keep reading stale host memory. Added after the
   UBO write and before the staging submits.

Also fixed: `SurfaceRenderer` pipeline `cullMode` is now `VK_CULL_MODE_NONE`, matching
`gui/shading_display.py`'s `BackfaceCullingOff()`/`FrontfaceCullingOff()` and required for
correctness - a triangle wound CCW as seen from above has a NEGATIVE signed area in Vulkan
framebuffer coordinates (y down), so `CULL_MODE_BACK_BIT` culled the whole terrain.

**New C ABI (all additive, none breaking):** `nkv_render()` now returns
`2 = drawn+submitted+presented`, `1 = frame skipped (minimized / out-of-date)`, `0 = failure`,
so `present_count` can no longer be inflated by skipped frames.
Added `nkv_set_clear_color`/`nkv_get_clear_color`, `nkv_get_last_mvp`,
`nkv_get_frame_stats(rendered, skipped, recreates)` and `nkv_get_point_count`.
`Renderer` gained `SetClearColor`/`GetClearColor`/`GetLastMvp`.

**Evidence after the fix (plain Win32 window, 200K points, desktop-composited readback):**
client area 388,809 non-clear pixels, 244 distinct colours, std 57.84; marker blob at
right/top. Surface path (after `nkv_clear`, one quad = 2 CCW triangles, magenta + yellow):
26,796 yellow and 300 magenta pixels with 35,893 distinct shades, i.e. the fragment shader is
genuinely shading geometry. `nkv_render` returned 2 for 12/12 frames,
`nkv_get_frame_stats` -> rendered=12 skipped=0 recreates=0.

**End-to-end app evidence (`py vulkan_preview_smoke.py`, VTK left / Vulkan right):**
**14/14 checks passed**, including `Vulkan pane has real content (std=63.2)` - previously
`std=0.0`. `vulkan_pane.png` is now 303,427 B (was 1,808 B), 111 distinct colours.
Orientation parity checked numerically: corr(VTK, Vulkan) = +0.267 while the
flipped variants are much weaker (flipY +0.102, flipX +0.099, rot180 +0.074), i.e. the panes
agree on terrain rather than on its mirror image.

**Build note.** The DLL builds with the existing CMake+Ninja tree, but MinGW's `bin` must be on
PATH (`H:\tools\mingw1310_64\bin`) or the compiler subprocesses fail with no diagnostics:
`$env:PATH = "H:\tools\mingw1310_64\bin;$env:PATH"; & "H:\tools\Ninja\ninja.exe" -C build`.

**Still open (explicitly not claimed as done):** the VTK vs Vulkan panes are not yet pixel
equal - the Vulkan pane renders the shaded surface with its own clear colour and lighting
while VTK composites points + labels; the Vulkan pane is 79% black vs VTK's 23%, so camera
framing and coverage still differ. `Renderer::CaptureOffscreenRGBA8` remains **declared in
`include/naksha/Renderer.hpp` but unimplemented** (it would also need a render pass whose
`finalLayout` is not `PRESENT_SRC_KHR`; the pipelines are render-pass-compatible across that
change). GPU-side classification/elevation/intensity shading is still not ported:
`nkv_set_point_cloud`'s own comments note intensity is dropped and the LUT modes are CPU-side.

