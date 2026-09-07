# SNT / ortho TIFF / LAS-LAZ audit

Audited 2026-09-05, working-tree HEAD `7f95d48`. Scope: the reported loading-order failures, multi-TIFF display, raster refinement, scene ownership, import/clear/shutdown lifetimes, and local native crash evidence. This is a targeted deep audit, not a claim that every feature in the repository was verified.

**Update (same day, repair pass):** findings #1-#6 below are now fixed in the working tree (`gui/gis/gis_layers.py`, `gui/gis/raster_lod.py`, `gui/gis/raster_properties.py`, `gui/vector_export.py`, `gui/app_window.py`). Findings #7-#9 are unchanged/unfixed - see "Status" lines added to each. `audit/test_raster_audit.py` now asserts the corrected behavior (it originally asserted the defects as evidence); all 7 cases pass, alongside the 24 pre-existing targeted tests and the rest of `tests/` (308 passed; 7 pre-existing unrelated failures in shading/cross-section/wheel-filter tests were confirmed present before this repair pass too, via `git stash`).

## Outcome

There were real native crashes and several independent raster defects. A required SNT -> TIFF -> LAS sequence was never a sound fix. Some failures concerned the Qt layer panel, others concerned which raster is drawn and whether full-resolution pixels are fetched. The screenshot alone cannot distinguish a coarse overview from native source resolution or an overlapping lower-quality raster.

## Native crash evidence

Windows Application Event 1000 records for `dist/Naksha/NakshaAI-LiDAR.exe`:

- 2026-09-05 16:10:26: `Qt6Core.dll` 6.10.1.0, exception `0xc0000005`, offset `0x1dc4`.
- 2026-09-05 16:11:20: same process, exception `0xc0000005`, unknown module.
- 2026-09-04 16:34:28: `pyside6.abi3.dll`, exception `0xc0000005`.

The latest entries in `C:/ProgramData/NakshaTech/NakshaAI-LiDAR/crash_logs/faulthandler.log` show:

`dropEvent -> _import_one -> _import_raster_layer -> register_gis_layer -> refresh -> _current_entry -> _entry_by_item -> item.data(...)`

The raster executor thread is idle in these entries; this is not evidence that a background raster read crashed. Earlier entries also show rendering and SNT restoration stacks, but the append-only fault log does not timestamp individual entries, and historical line numbers differ from current source. Do not attribute every historical crash to one cause. The packaged executable was involved; equivalence of its bundled code with current source was not established.

## Findings, in repair order

### 1. Critical: unsafe GIS tree rebuild lifecycle at the recorded crash location

References: `gui/gis/gis_layers.py:1414`, `:1486`, `:1494`, `:1603`, `:1612`, `:1623`.

`refresh()` reads the current native tree item, then blocks both widget and model signals while clearing/recreating all items. Model reset notifications are suppressed. Separately, `rowsMoved` and `layoutChanged` synchronously call a handler that clears/rebuilds the tree while the originating model operation is still on its call stack. There is no refresh/reorder reentrancy guard or exception-safe restoration of blocked signal state.

**Evidence level:** native crash location confirmed; the specific dangling-pointer mechanism remains unproven. The small reset experiment confirmed suppressed model-reset notifications but Qt still cleared its current selection. It did NOT reproduce an access violation. Blocking signals alone must not be presented as the proven root cause.

**Repair:** keep model structural signals enabled; suppress application handlers with a scoped rebuild flag/widget signal blocker; defer/coalesce reorder refresh until after the model operation returns; restore state in `finally`; retain selection by layer ID. Capture a native dump if the crash persists. A Python try/except around `item.data()` cannot recover from a native access violation.

**Status: fixed.** `refresh()` now guards against reentrancy with `panel._rebuilding`, clears the current item before rebuilding instead of blocking the model's reset signals, and restores `blockSignals` state in `finally`. `rowsMoved`/`layoutChanged` now post the rebuild through a zero-delay `QTimer` (`_queue_reorder`) instead of calling `_reorder_from_list()` synchronously from inside Qt's move/layout callback, so items are never deleted while still on that call stack.

### 2. High: overlapping TIFF order depends on import-time elevation and image extent

References: `gui/vector_export.py:3710`, `gui/gis/gis_layers.py:601`, `gui/gis/raster_lod.py:163`, `:228`.

Each TIFF plane uses `scene_z - 0.001 * image_extent`. Layer ordering only sets an additional `rank * 0.001` actor Z position. Different extents or scene elevations produce base differences far larger than the ordering offset. With depth testing enabled in the raster renderer, a nominal bottom layer can cover the nominal top layer. Importing a TIFF before versus after LiDAR can change its inferred base height. LOD restores the original per-file Z, preserving the discrepancy.

**Reproduced:** a top-listed plane at -10 plus its ordering offset remains behind a bottom-listed plane at -1. This can make an overlapping coarse image appear instead of the desired detailed TIFF; actual screenshot attribution needs the original layer set.

**Repair:** use a common raster plane elevation and authoritative ordering within the dedicated raster renderer, consistently in import, crop upload, and overview restoration. Validate overlapping rasters of different extents in every import permutation.

**Status: fixed.** `import_geotiff_as_texture()` now bakes every TIFF plane at Z=0 instead of `scene_z - 0.001 * image_extent`. `_apply_order()` additionally re-zeroes any plane's Origin/Point1/Point2 Z on every reorder (covering rasters imported before this fix, or by any other path), so the only depth signal left is `rank * _RASTER_Z_STEP` from panel order. Covered by `audit/test_raster_audit.py::test_layer_order_offset_overrides_import_plane_heights`.

### 3. High: one temporary read error can leave an overview indefinitely

References: `gui/gis/raster_lod.py:330`, `:351`, `:428`.

Failed requests are cached by actor/window/size/style with no expiration. Repeated refresh attempts for the same view skip that file forever until the request changes or `kick()` clears the cache. The GUI only gets a console message. A temporary I/O error can therefore look like permanently blurry imagery even after the source becomes readable.

**Reproduced:** five subsequent refresh attempts produce no second read after an injected temporary failure.

**Repair:** bounded retry with backoff and visible per-layer failure status, while preserving round-robin fairness.

**Status: fixed (retry added; no new GUI status indicator).** Failed requests now record an attempt count and a `retry_at` monotonic deadline (`2s, 4s, 8s, 16s, 30s cap`) per actor instead of a permanent skip; `start()` only skips while still inside that window, and the failure handler also (re)arms `_raster_lod_timer` for the backoff so a retry happens even without further camera/window activity. Console logging still reports failures; no per-layer status badge in the GIS panel was added (would be a follow-up UI change). Covered by `audit/test_raster_audit.py::test_transient_read_failure_retries_after_backoff`.

### 4. High: raster work remains active during window teardown

References: `gui/gis/raster_lod.py:293`, `:338`; `gui/app_window.py:13021`, `:13612`.

The loader closes on application quit or widget destruction, but the window shutdown path finalizes VTK earlier. `start()` and `finish()` do not check `_shutdown_in_progress`. The raster timer is not explicitly stopped before render-window finalization. Late delivery can therefore attempt VTK changes during shutdown.

**Reproduced:** a completed read calls `_apply_texture` with `_shutdown_in_progress=True`. No native shutdown crash was intentionally triggered, and this does not explain the recorded import crash by itself.

**Repair:** close the raster loader and detach its callbacks before VTK teardown; reject new/completed work after shutdown begins.

**Status: fixed.** `_Loader.start()` and `.finish()` now check `app._shutdown_in_progress` first and call `self.close()` (stops the poll timer, removes camera/render-window observers, shuts the executor down without waiting) instead of touching VTK. `AppWindow.closeEvent()` also now explicitly closes `_raster_lod_loader` right after the point where the close is committed (past the classification-worker abort check, before any VTK finalization) rather than relying solely on `aboutToQuit`/`destroyed`, which fire too late relative to `RenderWindow.Finalize()`. Covered by `audit/test_raster_audit.py::test_shutdown_flag_prevents_finished_gpu_upload`.

### 5. Medium: style changes discard the metadata needed to restore full coverage

References: `gui/gis/raster_properties.py:210`; `gui/gis/raster_lod.py:163`, `:402`.

A style change removes `_last_window` while leaving cropped geometry and texture in place. Overview restoration requires that metadata. Panning before the replacement read finishes leaves the old cropped quad stranded; if the replacement fails or the view is unsupported, the missing coverage can persist.

**Reproduced:** style change followed by a pan leaves geometry at X=40..60 while the viewport is X=70..90; `_restore_overview()` returns false.

**Repair:** retain coverage independently from style freshness, or restore full geometry/overview before invalidating the crop. Update styled overview content as well as detailed content.

**Status: fixed.** `apply_raster_style()` now pops only `meta["_last_style"]` (forcing the next LOD request to re-render the same crop window with the new style) and leaves `meta["_last_window"]` in place, so the crop geometry/coverage metadata survives a style change. If the user pans away before the restyled crop finishes, `_restore_overview()` still has what it needs and correctly falls back to the full-file preview. Covered by `audit/test_raster_audit.py::test_style_change_keeps_crop_coverage_until_replacement`.

### 6. Medium: refinement has unsupported-view and missing-notification gaps

References: `gui/gis/raster_lod.py:54`, `:373`, `:402`; `gui/vector_export.py:3511`.

- A rolled top view, even at one degree, is rejected. Perspective/tilted views also have no native-detail path, while eligible RGB imports start from at most four million overview pixels.
- Resizing the viewport does not schedule refinement. Enlarging a window can magnify the previous texture until another qualifying camera event occurs.
- `ensure_installed()` returns forever after initial installation, even if the active camera changes. It never rebinds its watcher to a replacement camera.

**Reproduced:** all three behaviors. Active-camera replacement was tested as a lifecycle gap; no confirmed current load path replacing the main camera was identified. Rolled/perspective limitations are explicit in the implementation, but silently falling back to coarse imagery is user-visible.

**Repair:** subscribe to viewport changes and camera replacement; define a full-detail policy for supported map orientations and an explicit fallback for unsupported views.

**Status: resize and camera-replacement gaps fixed; rolled/perspective limitation left as-is by design.** `_prepare_frame()` (already running once per rendered frame off the render window's `StartEvent`) now also compares the current `GetSize()` against the last-seen size and, on a change, invalidates coverage and reschedules a refresh exactly like a camera-modified event - a pure resize with no camera motion now triggers refinement. It also detects when `renderer.GetActiveCamera()` no longer matches the camera the LOD watcher is attached to (`_rebind_camera_if_replaced()`), moves its `ModifiedEvent` observer to the new camera, and forces an immediate refresh check. Neither addition calls the (comparatively expensive) `_visible_world_bounds()` coverage computation outside the existing dirty-frame gate, preserving the once-per-dirty-frame throttling the pre-existing `test_camera_burst_checks_coverage_once_per_frame` test guards. The rolled/perspective-view restriction and the four-megapixel starting overview are unchanged - those are explicit, intentional limits of the windowed-read approach (no single rectangular "visible extent" to key a re-read off), not a lifecycle bug; a visible "reduced detail" indicator for those views would be a separate UX addition. Covered by `audit/test_raster_audit.py::test_resize_schedules_refinement` and `::test_camera_replacement_rebinds_watcher`.

### 7. High risk from static review: LiDAR import is not a single protected transaction

References: `gui/app_window.py:6119`, `:6465`, `:6502`, `:6513`, `:6534`, `:6825`; `gui/file_loader_worker.py:24`; `gui/grid_label_system.py:8325`.

Normal import, grid import, and TIFF import have separate lifecycle rules. Normal import stores a parentless QThread in one `_file_loader_worker` slot without an entry guard or result-generation identity. Its custom `finished(dict)` signal is emitted before the QThread run method returns. Finalization pumps Qt events repeatedly and then clears the shared worker slot. Overlapping/reentrant requests can overwrite shared progress/options/data, and old results are not rejected. The child loading overlay mitigates ordinary clicks but is not a transaction guard against all callbacks or programmatic requests. Window close does not explicitly cancel/wait for this worker.

**Evidence level:** source-level lifetime/reentrancy risk; no native worker crash reproduced. Do not conflate this with the confirmed GIS-panel crash.

**Repair:** one shared scene-import transaction across normal/grid/drop paths, generation-tagged results, retained workers until actual thread completion, and cancellation/teardown handling. Defer raster GPU updates during scene mutation, then resume refinement.

**Status: not fixed - deferred.** This is a cross-cutting relifetime change touching `app_window.py`'s three separate import paths plus `file_loader_worker.py` and `grid_label_system.py`; it was not reproduced as a crash (see Evidence level above) and carries meaningfully higher regression risk than #1-#6, which directly explain the reported symptoms (the crash and the blurry/overlapping TIFF). Recommend tackling as a separate, deliberately scoped change with its own test pass rather than folding into this repair.

### 8. Medium: raster memory limits are per request, not an aggregate residency limit

References: `gui/vector_export.py:3374`, `:3416`; `gui/gis/raster_lod.py:217`, `:376`.

Import budgeting always grants at least 16 MiB of headroom even when the nominal pool is exhausted. Detail reads independently permit up to eight million RGB pixels per layer and retain their full-file previews. There is no aggregate LOD eviction policy. Many layers can exceed the intended raster allocation alongside point-cloud buffers.

**Evidence level:** static review, not measured GPU exhaustion. Eight million RGB pixels are about 22.9 MiB of raw pixel storage per detail image, before previews, temporary copies, and implementation-specific GPU overhead.

**Repair:** enforce an aggregate raster cache budget across previews and detailed windows; evict inactive detail first and measure actual scene memory under repeated grid switches.

**Status: not fixed - deferred.** Static-review-only finding; needs measured GPU memory profiling under repeated grid switching before designing an eviction policy, which is out of scope for this repair pass.

### 9. Medium: CRS changes and raster geometry are not order-independent

References: `gui/app_window.py:6370` and `:6590` vicinity; `gui/vector_export.py:3644`; `gui/gis/raster_lod.py:90`.

Normal LiDAR loading can clear/replace the canvas CRS when no SNT is present even though TIFF layers survive. Existing raster coordinates are not rebuilt. The TIFF importer transforms only bounding-box corners for a differing CRS, not image pixels, and LOD maps source pixels linearly into those bounds. Rotated affine source rasters are also marked eligible without validating the transform. These assumptions are invalid for general reprojection/rotation.

**Evidence level:** static review; no original mixed-CRS dataset tested. Primarily a placement/pixel-correspondence defect, not proof of a crash or the screenshot's blur.

**Repair:** define one scene CRS across retained layers; warp image windows into it rather than stretching transformed bounds; handle source affine rotation explicitly.

**Status: not fixed - deferred.** Needs an actual mixed-CRS dataset to validate against; a reprojection/rotation fix attempted without one risks being unverifiable and higher-risk than the crash/blur cluster this pass targeted.

## Verification and deliverables

- Pre-existing targeted tests: **24 passed**, unchanged (raster LOD, real three-TIFF import with offscreen rendering, renderer composition, buffered grid loading, SNT coordinate alignment).
- `audit/test_raster_audit.py`: **7 tests**, rewritten from defect-evidence checks into regression tests for the fixes to #2, #3, #4, #5, #6 - all pass against the repaired code.
- `audit/reproduce_layer_tree_reset.py`: reset-notification experiment; unchanged, still does not reproduce a native crash (kept as documentation of what was and wasn't isolated, not as a regression test).
- Full `tests/` suite: **308 passed**, 7 pre-existing failures (shading/cross-section/border-logic/wheel-filter) confirmed unrelated and present before this repair pass via `git stash` on the same commit.
- Findings #1-#6 fixed in `gui/gis/gis_layers.py`, `gui/gis/raster_lod.py`, `gui/gis/raster_properties.py`, `gui/vector_export.py`, `gui/app_window.py`. Findings #7-#9 deliberately deferred - see their Status lines for why. Pre-existing deleted ECW plugin files were left untouched.

Commands:

```powershell
.\chiru\Scripts\python.exe -m pytest tests/ audit/test_raster_audit.py -q --ignore=tests/test_display_mode_window_lifecycle.py
.\chiru\Scripts\python.exe audit/reproduce_layer_tree_reset.py
```

(`test_display_mode_window_lifecycle.py` fails to import on this tree for an unrelated, pre-existing reason - a `gui.display_mode` symbol it expects no longer exists there.)

Remaining validation: reproduce the packaged-app crash with native dump capture to confirm the fix against the actual `0xc0000005` fault, not just the Python-level reentrancy reproduction; exercise all six SNT/TIFF/LAS order permutations using representative source data and the actual GUI, including selected tree rows, drag reorder, overlapping TIFFs, temporary read failures, resize, repeated grid switches, and closing during reads. The original screenshot's exact source files were not identified or replayed. Findings #7-#9 remain open - see their Status lines. No claim is made that the application is fully crash-free, only that the reproduced/located defects behind the reported symptoms are repaired and covered by tests.
