/*
 * naksha_vulkan_c_api.h
 *
 * Narrow extern "C" ABI for the naksha::vulkan engine, built as a shared
 * library (naksha_vulkan.dll) so it can be driven from Python via ctypes
 * without pybind11 (none is available on this machine).
 *
 * Design rules (per integration task boundaries):
 *  - No Vulkan handle types cross this boundary (opaque uint64 handle only).
 *  - One process-wide renderer instance is enough for this phase (the app
 *    has a single main viewport); the API is still handle-shaped so a
 *    second instance could be added later without an ABI break.
 *  - Every function is safe to call in "not initialized" / "already failed"
 *    states and returns a bool/int status rather than throwing or crashing
 *    - this DLL must never be able to take the Python process down.
 *  - Point/surface uploads are bulk-only (one call per attribute), matching
 *    the existing PointCloudRenderer/SurfaceRenderer contracts (see
 *    include/naksha/PointCloudRenderer.hpp, SurfaceRenderer.hpp).
 */
#pragma once

#include <stdint.h>

#if defined(_WIN32)
  #define NKV_API extern "C" __declspec(dllexport)
#else
  #define NKV_API extern "C" __attribute__((visibility("default")))
#endif

typedef uint64_t NkvHandle; /* 0 == invalid/none */

/* Display modes for the point-cloud path. Mirrors the existing production
 * RGB / Classification / Intensity / Elevation modes in gui/display_mode.py.
 * This selector only changes which uniform/LUT the fragment shader reads -
 * never a re-upload of the position/attribute buffers. */
typedef enum NkvDisplayMode {
    NKV_DISPLAY_RGB = 0,
    NKV_DISPLAY_CLASSIFICATION = 1,
    NKV_DISPLAY_INTENSITY = 2,
    NKV_DISPLAY_ELEVATION = 3,
    /* Instant Shaded Class: the splat pass draws class colour + depth into an
     * offscreen target and a fullscreen pass lights it from depth-reconstructed
     * normals. Setting this mode is a pure state change - no mesh is built and
     * no buffer is re-uploaded. Distinct from the TIN/Surface path on purpose:
     * selecting it selects a renderer, not a dataset transform. */
    NKV_DISPLAY_SHADED_CLASS_INSTANT = 4,
    /* Neutral gray: the same legacy VTK neutral color the UI opens with. This is
     * a distinct native mode rather than silently aliasing to RGB, so the app and
     * backend cannot disagree about a display: they either both render gray or
     * they both render the actual RGB mode. */
    NKV_DISPLAY_NEUTRAL = 5,
    /* Depth: grayscale shading by distance from the EYE, matching the legacy VTK
     * depth mode (gui/depth_settings_dialog.py: depth_gamma=1.0,
     * depth_color_scheme='grayscale'). It reads NO stored attribute - depth is
     * computed from the resident positions and the live camera - so it costs no
     * attribute stream and no XYZ upload, in 2D and 3D alike.
     *
     * Previously this mode had no native value at all, so selecting it logged
     * "[DISPLAY MODE ERROR] unsupported mode='depth'" and left the PREVIOUS mode
     * on screen. It is a real mode here, not an alias for RGB: the colour comes
     * from a genuine view-space depth computation in point.vert. */
    NKV_DISPLAY_DEPTH = 6,
} NkvDisplayMode;

/* Shading mode for the surface / shaded-class paths. Two SEPARATE formulas
 * (see gui/surface_mode.py _compute_surface_face_colors and
 * gui/shading_display.py _compute_shading / _compute_face_shade) - kept as
 * distinct enum values so the fragment shader can branch on a uniform
 * rather than the two formulas being merged/approximated into one. */
typedef enum NkvShadeFormula {
    NKV_SHADE_PASSTHROUGH = 0,   /* colors already fully baked on the CPU side (current implementation) */
    NKV_SHADE_SURFACE_RAMP = 1,  /* formula (B): ambient additive lerp, non-flipped azimuth  (not yet ported to GLSL) */
    NKV_SHADE_CLASS_LUT = 2,     /* formula (A): ambient hard floor, compass-flipped azimuth (not yet ported to GLSL) */
    NKV_SHADE_CRISP_HYBRID = 3,  /* real port of gui/shading_display.py's crisp-hybrid formula, see
                                     nkv_set_shaded_class_surface / nkv_set_crisp_shading_parameters below */
} NkvShadeFormula;

/* ---- Availability / lifecycle ------------------------------------------------- */

/* Cheap, side-effect-free check: can this process load Vulkan at all
 * (loader present, at least one physical device)? Does NOT create a
 * renderer or a window surface. Safe to call speculatively before deciding
 * whether to attempt create_renderer(). Returns 0/1. */
NKV_API int nkv_is_available(void);

/* Creates the renderer bound to an existing native window (HWND on
 * Windows), passed as a raw pointer-sized integer so ctypes needs no
 * struct marshalling. width/height are the initial swapchain extent in
 * pixels. Returns a non-zero handle on success, 0 on failure (bad HWND, no
 * suitable device, shader/pipeline creation failure, etc.) - failure is
 * always non-fatal to the caller. */
NKV_API NkvHandle nkv_create_renderer(void* hwnd, uint32_t width, uint32_t height,
                                       int enable_validation);

/* Idempotent: safe to call more than once, and safe to call with handle==0
 * (invalid handle) - both are no-ops. Blocks on GPU idle internally before
 * releasing resources (matches Renderer::Shutdown's existing WaitIdle rule). */
NKV_API void nkv_destroy_renderer(NkvHandle handle);

/* ---- Point cloud (bulk upload; camera-only interaction must never re-upload) --- */

/* xyz: float64 world-space (N,3) C-contiguous, exactly app.data["xyz"].
 * rgb/classification/intensity are optional (pass NULL to skip that
 * attribute this call) uint8 (N,3) / uint8 (N,) / float32 (N,) respectively.
 * Returns 1 on success, 0 on failure (handle invalid, allocation failure). */
NKV_API int nkv_set_point_cloud(NkvHandle handle,
                                 const double* xyz, const uint8_t* rgb,
                                 const uint8_t* classification,
                                 const float* intensity,
                                 uint64_t count);

/* Push-constant/uniform only - never touches the uploaded buffers. */
NKV_API int nkv_set_display_mode(NkvHandle handle, int mode /* NkvDisplayMode */);
/* Legacy fixed pixel size (pins the adaptive band to one value). */
NKV_API int nkv_set_point_size(NkvHandle handle, float pixels);

/* Adaptive sizing + GPU-side shading parameters. point.vert derives each
 * point's screen footprint from footprint_m / view depth and clamps it to
 * [min_px, max_px]; the ramp calls give the shader the same normalisation
 * range gui/pointcloud_display.py uses for VTK. See
 * src/naksha_vulkan_c_api.cpp for the per-call notes. */
NKV_API int nkv_set_point_size_params(NkvHandle handle, float footprint_m, float min_px,
                                      float max_px, float class_intensity_mix);
NKV_API int nkv_set_point_elevation_range(NkvHandle handle, float lo, float hi, float gamma);
NKV_API int nkv_set_point_intensity_range(NkvHandle handle, float lo, float hi,
                                          float contrast, float gamma);
NKV_API int nkv_set_point_sprite_params(NkvHandle handle, float softness, float brightness);

/* DEPTH mode normalisation: the view-space distance range the depth ramp spans,
 * plus the legacy depth_gamma. Push-constant only - it never touches a buffer,
 * so changing the camera in Depth mode costs uniforms and a redraw.
 *
 * Depth is derived from the LIVE camera, so this must be re-pushed whenever the
 * camera changes while Depth is active; a stale range does not go black, it just
 * compresses the ramp into part of the table. */
NKV_API int nkv_set_point_depth_range(NkvHandle handle, float lo, float hi,
                                      float gamma);

/* 256*3 uint8 colour tables read by point.vert (classification palette,
 * elevation ramp, intensity ramp); NULL leaves a table unchanged. */
NKV_API int nkv_set_point_luts(NkvHandle handle,
                               const uint8_t* class_rgb,
                               const uint8_t* elevation_rgb,
                               const uint8_t* intensity_rgb);
NKV_API uint64_t nkv_get_point_intensity_upload_count(NkvHandle handle);

/* ---- Attribute-only stream upload (PHASE 1: Class / Intensity / Elevation) ----
 *
 * Uploads the CLASSIFICATION and/or INTENSITY vertex streams for the ALREADY
 * RESIDENT position buffer, and NOTHING else. This is what lets a display-mode
 * switch add a required attribute without re-uploading XYZ:
 *
 *     Neutral -> Class      needs the class stream, positions unchanged
 *     Neutral -> Intensity  needs the intensity stream, positions unchanged
 *
 * so `nkv_get_point_position_upload_count()` MUST NOT move across such a switch.
 * That is the measured form of "XYZ uploads during switches = 0".
 *
 * CONTRACT - both streams are indexed by the SAME point index as the position
 * buffer, so `count` MUST equal the resident point count exactly. A mismatch is
 * REFUSED (returns 0) rather than partially accepted, because a short stream
 * would silently bind the wrong attribute to every point past its end. Pass
 * NULL for a stream this call does not update; the other stream still uploads.
 *
 * Mirrors nkv_set_point_normals: a separate vertex stream that never duplicates
 * or re-uploads positions. */
NKV_API int nkv_set_point_attributes(NkvHandle handle,
                                     const uint8_t* classification,
                                     const float* intensity,
                                     uint64_t count);
/* Points covered by each resident attribute stream (0 = that stream was never
 * uploaded, which is how [INTENSITY TRACE] distinguishes "absent" from
 * "present but all zero"). */
NKV_API uint64_t nkv_get_point_classification_count(NkvHandle handle);
NKV_API uint64_t nkv_get_point_intensity_count(NkvHandle handle);

/* Class visibility table: 256 bytes, 1 = that class is shown. It lands in the
 * class palette's ALPHA, so on NKV_DISPLAY_SHADED_CLASS_INSTANT a hidden class
 * writes no pixel at all (no colour, no depth) and the lighting pass shows the
 * viewport background through it. 256 BYTES: a Display Mode checkbox change is
 * a uniform push, never a point re-upload. Pairs with
 * nkv_get_point_position_upload_count - visibility updates must move
 * nkv_get_class_visibility_update_count while the position count stays put. */
/* ---- Stored oct16x2 normals (Instant Shaded production normal source) ----
 *
 * `packed` is the caller's 4-bytes-per-point octahedral signed-normalized
 * int16 x2 buffer; `count` is the POINT count, so count*4 bytes are read. The
 * bytes are uploaded verbatim and decoded in the shader - they are never
 * expanded to float32x3 on the CPU.
 *
 * ORDERING CONTRACT: point i of this stream MUST be the normal of point i of
 * the position stream. That invariant is verified by
 * validate_stream_attribute_alignment.py over the real cache; a violation
 * shows up as normals that belong to the wrong surface.
 *
 * Returns 1 on success. This never duplicates or re-uploads positions. */
NKV_API int nkv_set_point_normals(NkvHandle handle, const uint8_t* packed,
                                   uint64_t point_count);
/* Coverage / resident bytes / upload count. normal_upload_count must NOT move
 * on a warm style switch when the normal stream is already resident. */
NKV_API uint64_t nkv_get_point_normal_count(NkvHandle handle);
NKV_API uint64_t nkv_get_point_normal_bytes(NkvHandle handle);
NKV_API uint64_t nkv_get_point_normal_upload_count(NkvHandle handle);

/* Normal source for the Instant Shaded lighting pass.
 *   0 (default) STORED oct16 normals - the production path.
 *   1           screen-space depth reconstruction - DEV fallback / A-B only.
 * Switching is one float of push constant: no upload, no rebuild. */
NKV_API int nkv_set_instant_normal_source(NkvHandle handle, int screen_space);

NKV_API int nkv_set_class_visibility(NkvHandle handle, const uint8_t* visible256);
NKV_API uint64_t nkv_get_class_visibility_update_count(NkvHandle handle);

/* Lighting for NKV_DISPLAY_SHADED_CLASS_INSTANT. azimuth_deg is the compass
 * azimuth and elevation_deg the degrees above the horizon; they are the exact
 * pair gui/shading_display.py::_compute_shading consumes, so the instant and
 * legacy paths converge on the same look from the same slider. ambient is the
 * 0..1 shadow floor. debug_stage: 0 final colour, 1 reconstructed normal,
 * 2 lighting factor, 3 class colour (0 in normal use). Push-constant only -
 * calling it every frame costs nothing and touches no buffer. */
NKV_API int nkv_set_instant_shading_parameters(NkvHandle handle,
                                               float azimuth_deg,
                                               float elevation_deg,
                                               float ambient,
                                               int debug_stage);

/* Reconstruction neighbourhood radius in texels (clamped to 1..8).
 *
 * This exists because screen-space depth-normal reconstruction on a 1-3 px
 * splat depth buffer is dominated by WHICH point won the depth test in each
 * individual pixel: at radius 1 the result is per-pixel noise, not a surface
 * normal, which reads as coloured confetti. A wider neighbourhood averages
 * across the same planar surface and is what makes roofs/roads coherent.
 *
 * Push-constant only; safe to call any time. Default 2. */
NKV_API int nkv_set_instant_normal_radius(NkvHandle handle, int radius_texels);

/* Frames recorded through the instant path (0 when it never ran), and whether
 * the pass actually built. Lets a test prove the mode really rendered without
 * reading a pixel back, and detect the legacy fallback when it did not. */
NKV_API uint64_t nkv_get_instant_shaded_frame_count(NkvHandle handle);
NKV_API int nkv_get_instant_shaded_available(NkvHandle handle);

/* Proof that the POSITION stream was re-uploaded. The classification/elevation/
 * intensity/palette paths are uniform-only by contract, so this counter must not
 * move when app.class_palette / class visibility / class_weight / display mode
 * change - that is the "no million-point re-upload" guarantee, measured instead
 * of assumed. */
NKV_API uint64_t nkv_get_point_position_upload_count(NkvHandle handle);

/* Point-cloud draw toggle (buffers stay resident; no upload, no destroy).
 * Shaded Classification / Surface show a mesh instead of the raw cloud. */
NKV_API int nkv_set_point_cloud_visible(NkvHandle handle, int visible);
NKV_API int nkv_get_point_cloud_visible(NkvHandle handle);

/* ---- Per-stage CPU frame breakdown (PHASE 1) ----------------------------
 * Returns the CPU cost of each stage of the last completed frame, in ms.
 * All are CPU-side wall time around the real driver call. There is
 * deliberately NO GPU field: GPU timestamps are optional in Vulkan and
 * unavailable on some devices. out must hold NKV_FRAME_STAGE_COUNT doubles.
 */
#define NKV_FRAME_STAGE_COUNT 9
NKV_API int nkv_get_frame_timings(NkvHandle handle, double* out);

/* ---- Draw / LUT telemetry ------------------------------------------------ */
NKV_API uint64_t nkv_get_lut_update_count(NkvHandle handle);
NKV_API uint64_t nkv_get_point_draw_call_count(NkvHandle handle);
NKV_API uint64_t nkv_get_surface_draw_call_count(NkvHandle handle);
NKV_API uint64_t nkv_get_surface_overlay_draw_call_count(NkvHandle handle);
NKV_API int nkv_get_max_point_size(NkvHandle handle, float* out);

/* Floating render origin: the vertex buffer holds (world - origin). Reading it
 * back is what proves a "points vanished" report is not a precision/origin
 * problem. */
NKV_API int nkv_get_render_origin(NkvHandle handle, double* out3);

/* ---- ORTHOGRAPHIC (VTK parallel) camera ---------------------------------
 * The real projection for the Naksha main view. Camera state is centre +
 * parallel scale + aspect + near/far; there is no eye distance, so zoom only
 * changes parallel_scale and pan only moves the centre. */
NKV_API int nkv_set_camera_ortho(NkvHandle handle,
                                 double centre_x, double centre_y, double centre_z,
                                 double dir_x, double dir_y, double dir_z,
                                 double parallel_scale,
                                 double aspect, double near_clip, double far_clip);
NKV_API int nkv_set_camera_perspective_mode(NkvHandle handle);
NKV_API int nkv_get_camera_projection(NkvHandle handle, int* out_is_ortho,
                                      double* out_parallel_scale);

/* ---- Surface mesh (bulk upload) ------------------------------------------------ */

/* positions: float64 world-space (V,3) C-contiguous (converted to
 * render-space float32 internally using the same floating-origin rule as
 * the point path). faces: int32 (T,3) triangle indices. colors: uint8
 * (T,3) PER-FACE flat RGB, already baked (ramp*shade) per
 * gui/surface_mode.py's documented geometry contract - this call expands
 * per-face colors to a flat-shaded per-vertex triangle-soup buffer
 * internally (3 verts/face, duplicated positions) because the native
 * SurfaceRenderer's vertex buffer is per-vertex, not per-face; this keeps
 * the CPU-computed shading authoritative (NKV_SHADE_PASSTHROUGH) rather
 * than re-deriving shading in the shader. */
NKV_API int nkv_set_surface(NkvHandle handle,
                             const double* positions, uint64_t vertex_count,
                             const int32_t* faces, uint64_t face_count,
                             const uint8_t* face_colors_rgb);

/* ---- Tiled surface, LOD, culling and atomic swap ---------------------------
 *
 * Everything below is ADDITIVE: the legacy nkv_set_surface entry point still
 * works exactly as before, and a DLL that predates these symbols can be
 * detected with getattr() and degraded from.
 */

/* Revision counters for the stale guard (Part 10).
 *
 * The renderer stamps every PENDING resource set with (dataset_revision,
 * surface_revision) at upload time. Before promoting PENDING to ACTIVE it
 * compares those against the CURRENT values; on any mismatch the pending set
 * is discarded (safely, through the deferred-destruction queue) instead of
 * being shown. This is the native half of the protection the Python side
 * already performs with dataset object identity / surface generation.
 *
 * Bump the dataset revision whenever a different dataset is loaded, and the
 * surface revision whenever a new surface generation is started. */
NKV_API int nkv_set_dataset_revision(NkvHandle handle, uint64_t revision);
NKV_API int nkv_set_surface_revision(NkvHandle handle, uint64_t revision);
NKV_API uint64_t nkv_get_dataset_revision(NkvHandle handle);
NKV_API uint64_t nkv_get_surface_revision(NkvHandle handle);

/* Uploads a surface as a TILE + LOD resource set (Parts 6 and 8) instead of
 * one undifferentiated draw.
 *
 * Same geometry contract as nkv_set_surface, plus:
 *   tile_count     target number of spatial tiles (0 = pick automatically from
 *                  the triangle count). Each tile is a contiguous run of the
 *                  index buffer with its own render-space AABB.
 *   base_cell_m    the mesh's base vertex spacing in metres, used to derive
 *                  the coarse LOD levels. 0 disables LOD generation.
 *   is_preview     1 marks a coarse interactive stand-in. A preview is
 *                  reported as PREVIEW_ACTIVE and can never be mistaken for
 *                  the engineering-quality final surface.
 *   dataset_revision / surface_revision  stamped onto the pending set for the
 *                  stale guard described above.
 *
 * Returns 1 on success. Returns 0 with a diagnostic in nkv_last_error() on
 * failure, INCLUDING when the upload was refused because ACTIVE + PENDING +
 * the new set would exceed the safe VRAM budget - in that case the current
 * surface stays on screen untouched. */
NKV_API int nkv_set_surface_tiled(NkvHandle handle,
                                  const double* positions, uint64_t vertex_count,
                                  const int32_t* faces, uint64_t face_count,
                                  const uint8_t* face_colors_rgb,
                                  uint32_t tile_count, float base_cell_m,
                                  int is_preview,
                                  uint64_t dataset_revision,
                                  uint64_t surface_revision);

/* Atomically promotes a valid PENDING set to ACTIVE. Normally called
 * automatically once per nkv_render() at the frame boundary, so callers do
 * not have to drive it. Returns 1 if a swap happened, 0 otherwise.
 * `discarded_stale` (may be NULL) is set to 1 when a pending set was thrown
 * away by the stale guard instead of promoted. */
NKV_API int nkv_activate_pending_surface(NkvHandle handle, int* discarded_stale);
NKV_API int nkv_set_surface_indexed_blocks(NkvHandle handle, const double* positions,
    uint64_t vertices, const uint32_t* indices, uint64_t triangles,
    const uint8_t* cell_rgb, const uint32_t* block_index_counts, uint32_t blocks,
    uint64_t dataset_revision, uint64_t surface_revision);
/* 0 = points only, 1 = Surface only. Intended for the streaming mode owner. */
NKV_API int nkv_set_streaming_surface_active(NkvHandle handle, int active);
/* DEV-only raster diagnostic. Does not rebuild/upload geometry. Returns 0
 * when wireframe is unsupported; disabled restores the filled pipeline. */
NKV_API int nkv_set_surface_debug_wireframe(NkvHandle handle, int enabled);
NKV_API int nkv_get_surface_debug_wireframe(NkvHandle handle);
/* Fills out_counts[12] with the previous frame's REAL draw record:
 *   [0] streamingSurface active      [1] point draw calls
 *   [2] point count drawn            [3] indexed (triangle) draw calls
 *   [4] submitted triangles          [5] visible surface tiles
 *   [6] resident surface tiles       [7] selected surface LOD
 *   [8] SurfaceState                 [9] resident surface vertices
 *   [10] resident index count        [11] resident triangle count
 * "Surface is drawn as filled indexed triangles" is [3] > 0 and [4] > 0
 * while [1] stays 0, which is exactly what a point primitive cannot produce. */
NKV_API int nkv_get_surface_draw_proof(NkvHandle handle, uint64_t* out_counts);
#define NKV_SURFACE_DRAW_PROOF_WORDS 12

/* ---- Culling (Part 7) --------------------------------------------------- */
NKV_API uint32_t nkv_get_surface_total_tile_count(NkvHandle handle);
NKV_API uint32_t nkv_get_surface_visible_tile_count(NkvHandle handle);
NKV_API uint64_t nkv_get_surface_total_triangle_count(NkvHandle handle);
NKV_API uint64_t nkv_get_surface_visible_triangle_count(NkvHandle handle);

/* ---- LOD (Part 8) -------------------------------------------------------
 * Reports the level actually drawn last frame: 0 final, 1 medium,
 * 2 interaction/coarse. `is_moving` reflects whether the renderer currently
 * believes the camera is in motion (it also self-clears after the idle
 * debounce, so a caller that stops sending events still refines). */
NKV_API uint32_t nkv_get_surface_lod(NkvHandle handle, int* out_is_moving);
NKV_API int nkv_get_surface_lod_triangle_counts(NkvHandle handle,
                                                uint64_t* out_counts /*[3]*/);
/* Marks the camera as moving (1) or settled (0). While moving, the renderer
 * forces the interactive/coarse level; after `idle_refine_ms` of stillness it
 * refines to the requested quality on its own. */
NKV_API int nkv_set_surface_interacting(NkvHandle handle, int moving,
                                        double idle_refine_ms);
/* Per-level projected-error budget in pixels. Smaller = finer. Default 1.0. */
NKV_API int nkv_set_surface_lod_error_pixels(NkvHandle handle, float pixels);

/* ---- Atomic swap state (Part 9) ----------------------------------------
 * 0 EMPTY, 1 PREVIEW_ACTIVE, 2 FINAL_PENDING, 3 FINAL_ACTIVE. */
NKV_API int nkv_get_surface_state(NkvHandle handle);
NKV_API uint64_t nkv_get_surface_swap_count(NkvHandle handle);
NKV_API uint64_t nkv_get_surface_stale_discard_count(NkvHandle handle);

/* ---- Memory (Part 11) ---------------------------------------------------
 * Bytes for budget/active/pending/lodCache, plus the device's safe budget and
 * the measured peak coexistence during the worst swap. Any out pointer may be
 * NULL. */
NKV_API int nkv_get_surface_memory(NkvHandle handle,
                                   uint64_t* out_budget, uint64_t* out_active,
                                   uint64_t* out_pending, uint64_t* out_lod_cache,
                                   uint64_t* out_peak_during_swap);
NKV_API int nkv_get_surface_memory_rejected(NkvHandle handle);

/* Reports what UploadTiledMesh() WOULD decide for a mesh of the given size,
 * WITHOUT allocating or uploading anything.
 *
 * The budget rule is "ACTIVE + PENDING + incoming must fit in 70% of the
 * device-local heap", and the incoming cost is a pure function of the triangle
 * count (a triangle soup is 3 vertices x 28 B + 3 x 4 B of index = 96 B per
 * triangle, plus the coarse LOD levels). This entry point exposes that decision
 * so the refusal path can be verified and reported for meshes too large to
 * materialise on the host - which is exactly the SLOW-quality case.
 *
 * `out_required_bytes` receives the estimated incoming cost; `out_budget` the
 * effective budget; `out_would_accept` 1/0. Returns 1 when the handle is
 * valid. */
NKV_API int nkv_estimate_surface_bytes(NkvHandle handle, uint64_t triangle_count,
                                       uint64_t* out_required_bytes,
                                       uint64_t* out_budget,
                                       int* out_would_accept);

/* Overrides the safe VRAM budget. 0 restores the default (70% of the
 * device-local heap). Exposed so the budget guard can be exercised - and
 * tuned - without having to build a multi-gigabyte mesh to overflow it.
 * Returns 1 on success. */
NKV_API int nkv_set_surface_budget_bytes(NkvHandle handle, uint64_t bytes);

NKV_API int nkv_set_shading_parameters(NkvHandle handle, int formula /* NkvShadeFormula */,
                                        float azimuth_deg, float elevation_deg,
                                        float ambient);

/* ---- Shaded Class (crisp hybrid, real GLSL port) --------------------------------
 *
 * Reuses the geometry gui/shading_display.py's _compute_shading_geometry_backend
 * already computed (Delaunay + degenerate/edge/feature filtering + mixed-face
 * detection stay CPU-side, unchanged, and are NOT re-run here) - this call is
 * strictly the upload/GPU-buffer step for that already-computed geometry.
 * `positions`/`faces` are the SAME TIN passed to nkv_set_surface (world-space
 * double, C-contiguous). `vertex_class_id` maps each vertex to its display
 * class (index into `class_color_lut`, 256 entries * RGB). `mixed_face_ids` is
 * the subset of face indices (into `faces`) whose 3 vertices show different
 * display colors - exactly _collect_mixed_display_faces()'s output.
 *
 * Internally this builds TWO GPU buffer sets (see SurfaceRenderer::
 * UploadShadedClassMesh): PASS 1 = every face, flat-shaded from vertex-0's
 * class color; PASS 2 = only the mixed faces, each vertex keeping its own
 * true class color so the rasterizer barycentrically blends them. Face
 * normals are computed on this call (cross product in render space) - this
 * is NOT the expensive Delaunay/filtering CPU work, just a per-face
 * normalize(cross(...)), matching the ~90ms "face normals" stage already
 * measured in the existing Python geometry backend, not the ~1.8s Delaunay
 * stage.
 *
 * Returns 1 on success, 0 on failure. */
NKV_API int nkv_set_shaded_class_surface(NkvHandle handle,
                                          const double* positions, uint64_t vertex_count,
                                          const int32_t* faces, uint64_t face_count,
                                          const uint8_t* vertex_class_id,
                                          const uint8_t* class_color_lut /* 256*3 RGB */,
                                          const int32_t* mixed_face_ids, uint64_t mixed_face_count);

/* Push-constant only - never touches the buffers uploaded by
 * nkv_set_shaded_class_surface. sharpness_raw is the raw 0..999 Sharpness
 * slider value (gui/shading_display.py's shading_sharpness_angle) - the
 * legacy/overdrive split and effective light elevation are derived
 * IN-SHADER from this single value every frame, so this call is always a
 * few bytes regardless of how the Python-side derivation works. */
NKV_API int nkv_set_crisp_shading_parameters(NkvHandle handle,
                                              float azimuth_deg, float sharpness_raw,
                                              float ambient,
                                              float key_intensity, float fill_intensity);

/* ---- GPU class-colour LUT (Phase C) -------------------------------------
 *
 * Uploads ONLY the 256-entry palette (256*3 RGB in, 256*4 RGBA on the GPU).
 * It is the palette-change path: it must not re-upload positions, normals,
 * indices or per-vertex class ids, and it must not change the mesh-upload
 * count, so "a palette change is not a geometry change" is provable by
 * comparing nkv_get_surface_upload_count() across the call.
 *
 * A client that has not uploaded a shaded-class mesh yet still gets a valid
 * LUT (the buffer is created on first use), so a palette can be staged ahead
 * of the geometry. Returns 1 on success. */
NKV_API int nkv_set_class_color_lut(NkvHandle handle, const uint8_t* lut_rgb);

/* Number of successful nkv_set_class_color_lut uploads. Pair with
 * nkv_get_surface_upload_count: the LUT count rising while the mesh count
 * stays flat is the measured form of "recolour without rebuilding". */
NKV_API uint64_t nkv_get_class_lut_update_count(NkvHandle handle);

/* ---- Colour-space / staged-debug parity with the VTK viewport -------------------
 *
 * The swapchain is created as VK_FORMAT_B8G8R8A8_SRGB (see
 * Renderer::ChooseSurfaceFormat), so the hardware encodes every written value
 * linear->sRGB. VTK however writes its shaded byte straight into an 8-bit
 * buffer, so the same facet used to render up to ~35% brighter through Vulkan.
 *
 * color_mode:
 *   1 (default) = shaders pre-compensate that encode, so the framebuffer holds
 *                 exactly the byte gui/shading_display.py computed;
 *   0           = previous raw linear write.
 * Applies to BOTH the point cloud (shaders/point.vert) and the surface /
 * Shaded Class mesh (shaders/surface.frag).
 *
 * debug_stage isolates one stage of the crisp pipeline on screen:
 *   0 = final shaded colour (production), 1 = face normal, 2 = lighting factor,
 *   3 = class colour only, 4 = raw N.L before the crisp remap.
 *
 * ambient_floor / base_light_elevation_deg carry the CPU's own resolved values
 * (_crisp_blend_ambient_floor / _shading_fixed_light_elevation) so an
 * environment override on the Python side can never desync the shader. Pass a
 * negative floor / non-positive elevation to use the shader defaults (0.08/45).
 *
 * Push-constant only: never touches an uploaded buffer. */
NKV_API int nkv_set_color_parity_params(NkvHandle handle, int color_mode, int debug_stage,
                                        float ambient_floor, float base_light_elevation_deg);
NKV_API uint64_t nkv_get_parity_param_update_count(NkvHandle handle);

/* Diagnostics for the rebuild-vs-reupload-vs-param-update proof. */
NKV_API uint64_t nkv_get_surface_upload_count(NkvHandle handle);
NKV_API uint64_t nkv_get_shade_param_update_count(NkvHandle handle);
NKV_API uint64_t nkv_get_overlay_triangle_count(NkvHandle handle);

/* ---- Camera / frame ------------------------------------------------------------ */

NKV_API int nkv_set_camera_lookat(NkvHandle handle,
                                   double eye_x, double eye_y, double eye_z,
                                   double target_x, double target_y, double target_z,
                                   double fov_y_degrees, double near_clip, double far_clip);
NKV_API int nkv_set_render_origin(NkvHandle handle, double x, double y, double z);
NKV_API int nkv_resize(NkvHandle handle, uint32_t width, uint32_t height);

/* Renders and presents one frame.
 *   2 = a real frame was drawn, submitted AND presented (swapchain image
 *       reached vkQueuePresentKHR)
 *   1 = frame skipped (window minimized / swapchain out of date) - NOT an error
 *   0 = failure (invalid handle, AcquireNextImage/submit error)
 * A non-zero return therefore only means "not an error": callers that need
 * "a real frame reached the screen" must test for 2, or use
 * nkv_get_frame_stats(). This distinction was added because returning 1 for
 * both cases made it impossible for the UI to tell a live renderer from a
 * renderer that was silently skipping every frame. */
NKV_API int nkv_render(NkvHandle handle);
NKV_API void nkv_clear(NkvHandle handle); /* drops point/surface buffers, keeps renderer alive */

/* ---- Frame accounting / viewport state ---------------------------------------- */

/* Clear colour used by every subsequent rendered frame (default opaque black).
 * Returns 1 on success. Useful both for theming the viewport and for proving
 * that a captured pixel really came from this renderer. */
NKV_API int nkv_set_clear_color(NkvHandle handle, float r, float g, float b, float a);
NKV_API int nkv_get_clear_color(NkvHandle handle, float* out_rgba4);

/* Last MVP matrix written into the frame UBO (column-major float32[16], the
 * exact values GLSL sees). Zero-filled when the handle is invalid, so callers
 * can always inspect the camera math the GPU actually used. */
NKV_API int nkv_get_last_mvp(NkvHandle handle, float* out_mvp16);

/* Offscreen capture of the CURRENT camera view - the only reliable way to
 * read back engine pixels (QScreen.grabWindow / PrintWindow go through GDI
 * and see nothing of a Vulkan surface). Renders the frame-slot UBO last
 * pushed through nkv_set_camera_lookat into a CPU buffer: RGBA8, top-to-bottom
 * rows, exactly w*h*4 bytes copied into out_rgba (capacity must be >= that;
 * *out_w/*out_h receive the dimensions). Returns 1 on success, 0 on failure
 * (see nkv_last_error). BLOCKING - it waits for the GPU; diagnostic / parity
 * test path, never call it per frame. */
NKV_API int nkv_capture_frame(NkvHandle handle, uint8_t* out_rgba, uint64_t capacity,
                              uint32_t* out_w, uint32_t* out_h);

/* Depth buffer of the SAME offscreen capture, float32 in [0,1], top-to-bottom
 * row order, w*h*4 bytes. Requires a D32_SFLOAT depth attachment (returns 0
 * otherwise).
 *
 * This is what makes a per-pixel shading comparison honest: colour alone
 * cannot prove the sampled pixel belongs to the face under test, because a
 * different, nearer face can own that pixel and still produce a perfectly
 * valid colour. Depth is the ground truth for who won the depth test.
 * BLOCKING - diagnostic / parity test path, never call it per frame. */
NKV_API int nkv_capture_depth(NkvHandle handle, float* out_depth, uint64_t capacity,
                              uint32_t* out_w, uint32_t* out_h);

/* Honest frame accounting - see Renderer::FrameStatistics.
 *   rendered  = frames that reached vkQueuePresentKHR
 *   skipped   = frames dropped because the surface was minimized/out of date
 *   recreates = swapchain rebuilds
 * Returns 1 when the handle is valid (any of the three out pointers may be
 * NULL), else 0. */
NKV_API int nkv_get_frame_stats(NkvHandle handle, uint64_t* rendered,
                                uint64_t* skipped, uint64_t* recreates);

/* Points currently resident in the GPU position buffer (0 when none). */
NKV_API uint64_t nkv_get_point_count(NkvHandle handle);

/* Surface geometry currently resident on the GPU. Needed for an honest
 * GPU-memory breakdown: without the real counts a per-buffer figure can only
 * be estimated, not measured. 0 when no surface has been uploaded. */
NKV_API uint64_t nkv_get_surface_vertex_count(NkvHandle handle);
NKV_API uint64_t nkv_get_surface_index_count(NkvHandle handle);

/* Per-attribute point upload counters. Kept separate from the position
 * counter so [VULKAN GPU PERSISTENCE] can prove that a display change
 * (palette / class visibility / weights) updated the small buffers only and
 * left the geometry resident. */
NKV_API uint64_t nkv_get_point_color_upload_count(NkvHandle handle);
NKV_API uint64_t nkv_get_point_classification_upload_count(NkvHandle handle);

/* Diagnostics only - never required for correct operation. */
/* Real frame timing. GPU numbers come from the VK_QUERY_TYPE_TIMESTAMP pool
 * (timestampPeriod-corrected); CPU submit / present are measured around the
 * individual driver calls. They are intentionally separate: a CPU figure must
 * never be presented as a GPU figure. Any out pointer may be NULL.
 * Returns 1 when the handle is valid, 0 otherwise. `valid` is set to 1 only
 * once a full frame's timestamps have actually been read back. */
/* Screen-space LOD. Sets the visible draw windows into the ALREADY-RESIDENT
 * point buffer: the caller must have uploaded the full cloud in tile-index
 * order, so (firstVertex, vertexCount) address contiguous points. No geometry
 * is re-uploaded here - only the draw commands change.
 * Passing rangeCount 0 (or null pointers) restores the single full-buffer
 * draw, i.e. LOD off. */
NKV_API int nkv_set_point_draw_ranges(NkvHandle handle,
                                      const uint32_t* firsts,
                                      const uint32_t* counts,
                                      uint32_t rangeCount);
NKV_API int nkv_clear_point_draw_ranges(NkvHandle handle);

/* ---- Stable-offset ARENA residency -----------------------------------------
 * nkv_set_point_cloud re-packs the whole cloud at offset 0, so adding ONE tile
 * used to re-upload every point and move every other tile. The arena reserves a
 * fixed point capacity once; tiles are then written at caller-chosen STABLE
 * offsets and nkv_set_point_draw_ranges selects what is drawn (an empty list
 * draws nothing). A draw-set change never touches a buffer.
 *
 * nkv_reserve_point_capacity: 0 = failed, 1 = ready, 2 = (re)allocated - every
 *   earlier tile is invalid and must be uploaded again.
 * nkv_upload_point_tile: positions are WORLD float64 (converted against the
 *   render origin); classification / intensity / normals_oct16 are optional and
 *   use the SAME point offset, so point i is one canonical point in every
 *   stream. normals_oct16 is the packed 4-byte R16G16_SNORM pair, count*4 bytes.
 *   xyz_world_f64 may be NULL to upload the optional streams ONLY for a tile
 *   whose positions are already resident (mode-switch backfill).
 *   Returns 0 on failure (including first+count > capacity).                  */
NKV_API int nkv_reserve_point_capacity(NkvHandle handle, uint64_t capacity_points);
NKV_API int nkv_upload_point_tile(NkvHandle handle, uint64_t first, uint64_t count,
                                  const double* xyz_world_f64,
                                  const uint8_t* classification,
                                  const float* intensity,
                                  const uint8_t* normals_oct16);
NKV_API uint64_t nkv_get_point_capacity(NkvHandle handle);
NKV_API uint64_t nkv_get_arena_uploaded_tiles(NkvHandle handle);
NKV_API uint32_t nkv_get_point_draw_range_count(NkvHandle handle);
NKV_API uint32_t nkv_get_point_draw_range_points(NkvHandle handle);

/* Real GPU frame time in ms, from the VK_QUERY_TYPE_TIMESTAMP pool, corrected
 * by timestampPeriod. Returns -1.0 when unavailable (disabled / unsupported /
 * not yet read back) - never a fake 0.0. */
NKV_API double nkv_get_gpu_frame_time_ms(NkvHandle handle);

/* CPU submit / GPU render / present in one call. Any out pointer may be NULL.
 * `valid` is 1 only once a full frame of timestamps has been read back. */
NKV_API int nkv_get_frame_timing(NkvHandle handle, double* cpuSubmitMs,
                                  double* gpuRenderMs, double* pointGpuMs,
                                  double* surfaceGpuMs, double* presentMs,
                                  int* valid);

NKV_API double nkv_last_frame_ms(NkvHandle handle);
NKV_API const char* nkv_last_error(void);

/* ---- Device/status introspection (for real status-bar/banner display, not
 * a guess) - all populated from VulkanContext::GetReport() at
 * nkv_create_renderer() time. Empty string / 0 if handle is invalid. */
NKV_API const char* nkv_get_device_name(NkvHandle handle);

/* Authoritative GPU memory figures, straight from the physical device the
 * engine already selected.
 *
 * WHY THIS EXISTS. The streaming budget planner has to know how much VRAM it may
 * spend, and it was being told "GPU unknown / 1.00 GB / software-fallback" on a
 * machine where the engine had already opened an NVIDIA T400 with 4 GB. The
 * planner then either under-budgeted the whole cloud or probed the system a
 * second time and disagreed with itself. There is exactly ONE source of truth:
 * the device this handle is running on.
 *
 * nkv_get_vram_bytes        - largest DEVICE_LOCAL heap (real card memory)
 * nkv_get_device_vram_budget - the safe spendable figure: one 70% rule shared
 *                              with the surface budget, kept as a safety margin
 *                              so streaming never drives the card into an OOM or
 *                              a system-memory spill.
 *
 * Both return 0 when there is no device, which callers must treat as UNKNOWN -
 * never as "zero memory".
 */
NKV_API uint64_t nkv_get_device_vram_budget(NkvHandle handle);
NKV_API uint32_t nkv_get_api_version(NkvHandle handle); /* raw VK_API_VERSION uint32 */
NKV_API uint64_t nkv_get_vram_bytes(NkvHandle handle);
