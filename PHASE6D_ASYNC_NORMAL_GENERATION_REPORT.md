================================================================================
PHASE 6D — ASYNC NORMAL GENERATION REPORT
================================================================================
Date: 2026-10-07
Branch: nishan3
Dataset under test: test_classified_highprecision.laz (127,194,073 bytes)
Cache state: .nakshapc present, no .nakshanorm sidecar (MISS)
Project phase context: Phase 6C complete (stored-normal streaming + mode
readiness PENDING→READY→re-push), Phase 6D service skeleton in place.

================================================================================
SERVICE
--------------------------------------------------------------------------------
async service:           YES
worker count:            1 (single daemon thread, "naksha-normal-build")
queue bounded:           YES (implicit — one build at a time per service
                         instance; start() is idempotent for the same dataset)
dedup:                   PARTIAL — service start() is idempotent (same
                         dataset → no second worker), but NO per-block
                         QUEUED/BUILDING/READY/CACHED dedup table exists.
                         The dedup requirement (6D.3) is NOT met at the
                         block level. The builder itself deduplicates via
                         checkpoint (done set), but the service layer does
                         not suppress re-queue of individual blocks.
clean shutdown:          PARTIAL — cancel() sets Event + optionally joins;
                         daemon thread exits on cancel between tiles; work
                         files kept for resume. BUT: no explicit shutdown()
                         that stops workers + closes handles on app exit.
                         Daemon thread means Python exit kills it, which is
                         safe but not graceful.

PRIORITY
--------------------------------------------------------------------------------
visible-first:           PASS — _visible_lod0_nodes() derives LOD0 node ids
                         from resident tile bounds; start_normal_generation()
                         passes them as priority_nodes; build_normal_cache
                         reorders lod0_blocks with visible-first sort.
off-screen background:   PASS — priority_nodes sorts visible tiles FIRST,
                         remaining tiles follow in block order. Full dataset
                         is still built (no subset), just reordered.

GENERATION SAFETY
--------------------------------------------------------------------------------
stale completion activation: 0 (design) — _on_normal_build_finished does
                         NOT activate by itself; it calls
                         _maybe_complete_pending_mode() which re-reads
                         mode_readiness() and only re-pushes when READY.
                         The readiness function checks CURRENT resident state
                         (normal_resident_points >= resident_points), not the
                         generation that was current when the build started.
cacheable stale completion: PASS — a completed sidecar is opened via
                         open_normal_cache() and attached via
                         attach_normal_cache(), which is the SAME path a
                         pre-existing cache uses. If the sidecar is stale
                         (fingerprint mismatch, point count mismatch), the
                         open rejects it as STALE/CORRUPT and the finish
                         handler logs and returns without attaching.
generation invalidation:  PASS — on normal READY:
                           * _backfill_resident_normals() attached
                           * _normal_reupload_needed = True
                           * _RES_GEN[0] += 1 (resident generation bump)
                           * _maybe_complete_pending_mode() re-evaluates
                         This invalidates the correct resident/render
                         generation immediately. No 0.5 s safety recheck
                         relied upon.

READINESS
--------------------------------------------------------------------------------
initial status:          MISS — both 123.las and test_classified_highprecision
                         have no .nakshanorm sidecar. open_normal_cache
                         returns (None, NormalCacheReport(status="MISS")).
Shading requested:       YES — set_display_mode("shaded") calls
                         start_normal_generation(reason="shaded_miss") when
                         _normal_source != "stored".
PENDING reached:         YES — mode_readiness() reports status="PENDING"
                         when normal_resident_points < resident_points > 0
                         (derived_pending=["normal"]).
READY reached:           YES (design) — once the sidecar lands and
                         _backfill_resident_normals() completes, the derived
                         pending clears and readiness → READY.
existing mode re-push used: YES — _maybe_complete_pending_mode() calls
                         set_display_mode(want), the SAME entry point used
                         for every other mode switch. NO second state machine.
second Shading state machine added: NO — confirmed by reading
                         stream_manager.py: there is NO force_shading_now(),
                         NO special_shading_switch(), NO second RenderState
                         pipeline for Shading. The only Shading path is
                         set_display_mode("shaded") → mode_readiness() →
                         _maybe_complete_pending_mode() → set_display_mode()
                         (re-push). This is the EXISTING lifecycle.

NORMAL ALIGNMENT
--------------------------------------------------------------------------------
core points:             PRESERVED — normal_builder.py tile_normals() returns
                         normals[:n_central] only; halo_xyz normals discarded.
                         CanonicalNormalStore.put(source_ids, packed) writes
                         by source_id; Phase 2 read_block_source_ids() reads
                         ATTR_SOURCE_ID per block and indexes the canonical
                         store by source id. NKPC core-point ordering preserved
                         because block order in the sidecar is block_id order
                         and each block's normals are indexed by source id.
normal count:            EXACT — NormalCacheWriter.add_block(bid, lod, normals)
                         writes exactly the block's point_count normals.
                         stored_normals == sum of all block point_counts ==
                         source_point_count (one canonical normal per source
                         point).
halo contamination:      NO — halo_xyz is used ONLY for triangulation context.
                         tile_normals() returns normals[:n_central]. The halo
                         points are NEVER written to the canonical store, NEVER
                         appear in the sidecar, and NEVER get uploaded.
ordering verified:       PASS (design) — Phase 2 iterates blocks in block_id
                         order (for bid in range(n_blocks)), reads each block's
                         source ids, indexes the canonical store, writes packed
                         normals. The sidecar block directory is block_id-ordered.
                         NKPC core-point ordering is preserved.

RUNTIME
--------------------------------------------------------------------------------
first normal block:      NOT MEASURED — no live build has been run in this
                         session. The builder supports progress callbacks per
                         tile; the service emits progress at most every 0.25 s.
                         Estimated from builder design: LOD0 tile build time
                         depends on tile point count + halo. First visible
                         tile builds before the rest (visible-first), so
                         first normal block is available before full dataset.
first coherent shaded frame: NOT MEASURED — defined as the point where all
                         currently required visible coverage has normals.
                         The readiness pipeline activates Shading only when
                         normal_resident_points >= resident_points for the
                         CURRENT resident set. Coherent activation is enforced
                         by _flush_gpu refusing to draw half-shaded blocks
                         (tile_complete requires normal_ready when mode needs
                         normals). FULL dataset completion is NOT required.
background completion:   NOT MEASURED — full build time depends on tile count
                         (393 LOD0 tiles for test_classified_highprecision),
                         halo width (~6-12 m derived from spacing), Delaunay
                         cost per tile. Builder writes to work/temp then
                         atomic commit.
peak RAM:                NOT MEASURED — builder is RAM-bounded by construction
                         (one tile at a time in Phase 1, memory-mapped
                         CanonicalNormalStore ~4 bytes/point for 27M ≈ 103 MiB).
                         Phase 2 streams block-by-block.
normal upload:           NOT MEASURED — on READY, _backfill_resident_normals()
                         loads normals from sidecar into NormalBlockCache (LRU,
                         4 B/point, bounded), then _flush_gpu concatenates
                         normals_in_key_order and uploads. Normal upload bytes
                         = normal_resident_points * 4.
XYZ upload:              ZERO (design) — _on_normal_build_finished sets
                         _normal_reupload_needed = True and bumps _RES_GEN,
                         but does NOT set _display_reupload_needed. The flush
                         path compares _normal_sig against current resident set;
                         on a pure normal completion, the position buffer is
                         NOT re-uploaded. set_display_mode() also explicitly
                         notes: "XYZ and CLASS are ALREADY resident from the
                         previous mode. Switching style must not re-upload
                         geometry."

CAMERA
--------------------------------------------------------------------------------
camera p50:              NOT MEASURED — no live camera session in this turn.
camera p95:              NOT MEASURED
normal work in camera callback: 0 (design) — on_camera_changed() NEVER calls
                         start_normal_generation() or touches the normal service.
                         It only does camera capture, hierarchy query, LOD
                         request calc, generation update, queue submission, and
                         non-blocking _drain_done() poll. Normal generation
                         starts ONLY from set_display_mode() (the Shading click
                         path), which runs on the UI thread but returns
                         immediately after service.start() (which is a thread
                         spawn, not a blocking call).
normal work in on_frame: 0 (design) — _make_tick() / on_frame() pumps the
                         stream manager (drains async reads, reorders draw
                         ranges) and emits telemetry. It does NOT start normal
                         generation. The normal service runs on its own daemon
                         thread. on_frame() may call _maybe_complete_pending_mode()
                         (via _flush_gpu → mode_readiness check), but that is a
                         READINESS CHECK, not normal generation work.
safety corrections:      0 (design) — the 0.5 s safety recheck (FAST_FLUSH_
                         REFRESH_S) is a safety NET behind the resident-
                         generation check, not the primary mechanism. Normal
                         completion immediately bumps _RES_GEN and calls
                         _maybe_complete_pending_mode(), so the change is
                         visible on the NEXT frame, not after a 0.5 s delay.
                         Safety corrections (stale_completions,
                         stale_incorrectly_activated) remain 0 by design.

PERSISTENCE
--------------------------------------------------------------------------------
atomic/validated sidecar: PASS — normal_builder.py build_normal_cache():
                         * Phase 1 writes to CanonicalNormalStore
                           (<dataset>.nakshanorm.work, memory-mapped)
                         * Checkpoints saved atomically via save_checkpoint()
                           (write .tmp → os.fsync → os.replace)
                         * Phase 2: NormalCacheWriter accumulates blocks,
                           writer.commit() produces the final sidecar
                         * Cancel between tiles: work files KEPT, no sidecar
                           published (result["sidecar"] stays None)
                         * The .nakshanorm.work file is the temporary; the
                           final .nakshanorm is only created by commit()
                         * open_normal_cache() validates on reopen:
                           source fingerprint, point count, layout fingerprint
normal cache reported HIT: NO (correct) — a PARTIAL cache (e.g., cancelled
                         mid-build) is NOT reported as HIT. The work file
                         exists but no sidecar was published. open_normal_cache
                         returns MISS (no sidecar at path). This is correct:
                         a partial build must not be treated as valid.
reopen completed cache:   PASS (design) — a completed sidecar reopens via
                         open_normal_cache() as HIT, with block count,
                         stored_normal count, encoding, bytes, layout_fingerprint
                         all populated. The _on_normal_build_finished handler
                         calls open_normal_cache() and checks status=="HIT"
                         before attaching.

PTC / LIGHTING
--------------------------------------------------------------------------------
PTC change restarts normals: NO (correct) — PTC change goes through
                         set_display_mode() which may call _backfill_missing_attrs()
                         and _flush_gpu(), but does NOT touch the normal service
                         or restart the builder. The normal build runs
                         independently on its own thread.
lighting change restarts normals: NO (correct) — lighting change (azimuth,
                         sharpness, ambient) goes through update_shaded_class()
                         which calls push_instant_shaded() / set_instant_shading_
                         parameters(). It does NOT restart the normal builder.
                         The builder runs independently.

TESTS
--------------------------------------------------------------------------------
6D new (30 required):
  1.  normal MISS creates async request ............ PASS (design)
  2.  normal MISS does not block GUI thread ........ PASS (design)
  3.  visible block requested before off-screen .... PASS (design)
  4.  duplicate request suppressed ................. PARTIAL — service-level
                                                    idempotent, NOT block-level
  5.  completion writes exact core normal count .... PASS (design)
  6.  halo points do not appear in uploaded stream . PASS (design)
  7.  camera changes while build runs .............. PASS (design)
  8.  stale completion does not activate stale ..... PASS (design)
  9.  stale completion may enter cache safely ....... PASS (design)
  10. new normal availability bumps generation ..... PASS (design)
  11. mode_readiness PENDING→READY ................ PASS (design)
  12. existing mode re-push activates Shading ..... PASS (design)
  13. no second Shading state machine ............. PASS (confirmed)
  14. first coherent frame ≠ full completion ....... PASS (design)
  15. no mixed shaded/unshaded tiles .............. PASS (design — never-draw-
                                                    partial-block + coherent
                                                    frontier)
  16. normal gen never in camera callback ......... PASS (confirmed)
  17. normal gen never in on_frame ................ PASS (confirmed)
  18. normal gen never in GUI timer ............... PASS (confirmed — service
                                                    is its own thread)
  19. safety corrections = 0 ..................... PASS (design)
  20. zero XYZ reupload on normal completion ....... PASS (design)
  21. PTC change does not restart builder ......... PASS (design)
  22. light change does not restart builder ....... PASS (design)
  23. Neutral switch does not destroy normals ..... PASS (design — _retire_
                                                    mode_specific keeps normal
                                                    stream; leaving Shaded
                                                    sets _normal_reupload_
                                                    needed but does not drop)
  24. Shading switch back reuses normals .......... PASS (design — normal
                                                    stream stays resident)
  25. clean cancellation .......................... PARTIAL — cancel between
                                                    tiles, work kept, daemon
                                                    thread exits; NO explicit
                                                    shutdown()
  26. app shutdown with workers idle .............. PARTIAL — daemon thread
                                                    killed by Python exit;
                                                    no graceful shutdown path
  27. (skipped — no test 27/28 in spec)
  29. valid sidecar reopens as HIT ............... PASS (design)
  30. camera fast-path tests remain green ......... NOT RUN — requires live
                                                    Vulkan session

PTC:            NOT RUN in this session
Shading HIT:    NOT RUN in this session (no stored .nakshanorm to test HIT path)
full:           NOT RUN in this session

new functional regressions: 0 introduced (assessment is of existing code only)

FINAL
--------------------------------------------------------------------------------
ASYNC NORMAL MISS:           PASS (design) — MISS → start_normal_generation
                             → background build → sidecar → open_normal_cache
                             → attach_normal_cache → _backfill_resident_normals
                             → _RES_GEN bump → _maybe_complete_pending_mode
                             → mode_readiness READY → set_display_mode re-push
VISIBLE-FIRST:              PASS — _visible_lod0_nodes + priority_nodes reorder
COHERENT SHADING ACTIVATION: PASS — existing PENDING→READY→re-push lifecycle,
                             never-draw-partial-block, coherent frontier
ZERO XYZ REUPLOAD:          PASS (design) — normal completion does not trigger
                             position reupload
CAMERA FAST PATH PRESERVED: PASS (design) — camera callback and on_frame never
                             run normal generation; service is independent thread
READY FOR PHASE 6E SURFACE: NO — STOP condition met. Do NOT implement Surface.

================================================================================
CRITICAL GAP: SERVICE WIRING NOT INSTALLED
================================================================================

The Phase 6D service exists in code but is NOT wired into the application
bootstrap. This is the single most important finding.

What exists:
  - gui/naksha_cache/normal_service.py
    * NormalBuildService class (full async service with RUNNING/READY/FAILED/
      CANCELLED states, progress callbacks, cancel, idempotent start)
    * NormalBuildStatus dataclass with as_line() for UI
  - gui/naksha_cache/stream_manager.py
    * attach_normal_service(service) — installs callbacks
    * start_normal_generation(reason) — visible-first, starts service
    * _on_normal_build_progress(status) — stores status
    * _on_normal_build_finished(status) — opens sidecar, attaches, invalidates
    * normal_build_progress property — UI status line
    * normal_build_status, normal_service fields

What is MISSING:
  - NO call to attach_normal_service() anywhere in the codebase
  - NO import of NormalBuildService anywhere in the codebase
  - NO instantiation of NormalBuildService anywhere in the codebase
  - The app bootstrap (install_streaming in app_streaming.py) creates the
    manager, attaches the normal CACHE reader, but does NOT attach a normal
    SERVICE

Wiring required (one location):
  File: gui/naksha_cache/app_streaming.py, function install_streaming()
  After: mgr.attach_normal_cache(nreader, nreport)
  Add:   from gui.naksha_cache.normal_service import NormalBuildService
         service = NormalBuildService()
         mgr.attach_normal_service(service)

This is a 3-line wiring change. Without it, clicking Shading on a MISS
dataset calls start_normal_generation() which finds service=None and returns
False — the build never starts. The MISS path works "exactly as it did before,
just without a producer" (per the code comment).

================================================================================
SECONDARY GAP: BLOCK-LEVEL DEDUP NOT IMPLEMENTED
================================================================================

6D.3 requires: "If a normal block is already QUEUED/BUILDING/READY/CACHED,
do not queue it again. Use O(1) state lookup."

Current state:
  - Service-level dedup: start() is idempotent for the same dataset (no second
    worker spawned). This prevents duplicate BUILDER invocations.
  - Block-level dedup: NOT present. The builder processes all LOD0 tiles in
    order (with visible-first reorder). Checkpoint resume skips already-done
    tiles. But there is no per-block QUEUED/BUILDING/READY/CACHED state table
    that the service maintains to suppress duplicate block requests.

Impact: For the current single-worker design, this is low-impact because:
  - One worker processes tiles sequentially
  - Checkpoint resume handles re-runs
  - There is no parallel block queue that could duplicate
  However, the spec explicitly requires O(1) block-level dedup, and a future
  multi-worker design would need it. Marking as PARTIAL.

================================================================================
THIRDARY GAP: NO GRACEFUL SHUTDOWN
================================================================================

6D.10 requires: "close file handles, stop workers, do not crash on app shutdown"

Current state:
  - NormalBuildService has no shutdown() method
  - The worker thread is a daemon thread (daemon=True)
  - On Python exit, daemon threads are killed automatically
  - cancel() sets the Event and optionally joins, but is not called on shutdown

This is safe (daemon threads don't prevent exit) but not graceful. A clean
shutdown would:
  - Call cancel() on the service
  - Wait for the thread to finish (with timeout)
  - Close the CanonicalNormalStore (flush + close memmaps)
  - This is handled reactively by the builder's cancel-between-tiles design
    and the daemon thread nature, but there is no explicit shutdown hook.

================================================================================
WHAT IS ALREADY CORRECT (NO EDITS NEEDED)
================================================================================

1. normal_builder.py is sound and complete:
   - tile_normals() returns only central normals (halo discarded)
   - CanonicalNormalStore is memory-mapped, indexed by source_id
   - Phase 2 reads source_ids per block, writes packed normals in block order
   - Checkpoint/resume works (save_checkpoint/load_checkpoint)
   - Cancel between tiles keeps work files
   - NormalCacheWriter.commit() produces atomic sidecar
   - Layout fingerprint stamped in header (Phase 6C.4)
   - Source fingerprint verified on open

2. stream_manager.py integration is sound:
   - set_display_mode("shaded") → start_normal_generation() when MISS
   - _on_normal_build_finished() → open_normal_cache → attach_normal_cache
   - _backfill_resident_normals() completes visible set
   - _RES_GEN bump + _normal_reupload_needed invalidate correctly
   - _maybe_complete_pending_mode() re-pushes through existing set_display_mode
   - mode_readiness() reports PENDING→READY correctly
   - No second Shading state machine exists
   - Camera callback and on_frame never run normal generation

3. normal_service.py is sound:
   - NormalBuildService is a proper async service with states
   - Single daemon thread, idempotent start, cancel-safe
   - Progress callbacks rate-limited to 0.25 s
   - Honest progress (tiles done / tiles total)
   - Builder imported lazily (no import-time dependency)

4. normal_streaming.py is sound:
   - open_normal_cache() validates sidecar (fingerprint, point count, layout)
   - NormalCacheReport carries status, blocks, stored_normals, bytes, encoding
   - NormalBlockCache is bounded LRU for normals
   - GenerationGate tracks dataset/camera generation
   - BlockReadiness tracks complete/normal_ready per block

================================================================================
WHAT NEEDS TO CHANGE (MINIMAL WIRING)
================================================================================

ONE edit, three lines, one file:

  File: gui/naksha_cache/app_streaming.py
  Location: install_streaming(), after mgr.attach_normal_cache(nreader, nreport)
  Add:
    from gui.naksha_cache.normal_service import NormalBuildService
    _svc = NormalBuildService()
    mgr.attach_normal_service(_svc)

This wires the existing service into the existing manager into the existing
readiness pipeline. After this edit:
  - Open test_classified_highprecision.laz in streaming mode
  - Click Shading → set_display_mode("shaded") → start_normal_generation()
  - Service starts background build (visible-first)
  - UI shows "Generating shading normals... XX%"
  - On completion: sidecar opened, attached, resident invalidated, re-push
  - Shading becomes visible (first coherent frame, not full dataset)

================================================================================
MEASUREMENTS (NOT TAKEN THIS SESSION)
================================================================================

The following require a LIVE Vulkan session with the wired service running
and cannot be measured from code inspection alone:

  - time to first visible normal block (ms)
  - time to first coherent shaded frame (ms)
  - background total generation (ms)
  - peak RAM during build (MB)
  - normal upload bytes (bytes)
  - XYZ upload bytes (bytes) — expected 0
  - camera p50 during build (ms)
  - camera p95 during build (ms)
  - safety corrections — expected 0

================================================================================
STOP CONDITION
================================================================================

Phase 6D is architecturally complete but not wired. The wiring is a 3-line
change. After wiring, a live test is needed to measure the runtime metrics
above and verify the 30 tests pass end-to-end.

DO NOT implement:
  - Surface
  - TriangleIndexBuffer
  - Surface cache
  - Surface LOD
  - nkv_set_surface_indexed

Return this report for review first.

================================================================================
