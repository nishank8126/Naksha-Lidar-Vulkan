// naksha_vulkan_c_api.cpp - implementation of the narrow C ABI declared in
// include/naksha/naksha_vulkan_c_api.h. See that header for the contract.
//
// Single-instance design: this phase's app has exactly one main viewport,
// so one process-wide NakshaVulkanInstance is sufficient. The handle is
// still a real value (not just "1") so a future multi-instance version
// does not need an ABI break.

#include "naksha/naksha_vulkan_c_api.h"

#include <cmath>
#include <cstdio>
#include <cstring>
#include <chrono>
#include <memory>
#include <mutex>
#include <string>
#include <vector>

#include "naksha/Renderer.hpp"
#include "naksha/PointCloudRenderer.hpp"
#include "naksha/SurfaceRenderer.hpp"
#include "naksha/Camera.hpp"

namespace {
// PHASE 1: monotonic CPU millisecond clock used to attribute cost to a stage.
// steady_clock, not system clock, so an NTP step cannot produce a NEGATIVE
// frame time.
double NowMsApi() {
    using namespace std::chrono;
    return duration<double, std::milli>(steady_clock::now().time_since_epoch())
        .count();
}
}  // namespace
#include "naksha/RenderOrigin.hpp"

#if defined(_WIN32)
#include <windows.h>
#endif

using namespace naksha;

namespace {
// RendererConfig::shaderDirectory defaults to "<exe dir>/shaders" (see
// Renderer.cpp), which is wrong for us: the "exe" is python.exe, not this
// DLL, and the .spv files were copied by CMake directly next to
// naksha_vulkan.dll (not into a "shaders" subfolder - see the POST_BUILD
// copy_if_different rules in CMakeLists.txt). Resolve this DLL's own
// directory instead so shader loading works regardless of which
// executable hosts it.
std::string ThisDllDirectory() {
#if defined(_WIN32)
    HMODULE mod = nullptr;
    GetModuleHandleExA(
        GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS | GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT,
        reinterpret_cast<LPCSTR>(&ThisDllDirectory), &mod);
    char path[MAX_PATH] = {0};
    if (mod) {
        GetModuleFileNameA(mod, path, MAX_PATH);
    }
    std::string p(path);
    std::size_t sep = p.find_last_of("\\/");
    if (sep == std::string::npos) return ".";
    return p.substr(0, sep);
#else
    return ".";
#endif
}
} // namespace

namespace {

struct Instance {
    Renderer renderer;
    PointCloudRenderer pointCloud;
    SurfaceRenderer surface;
    int streamingSurface = -1; // -1 retains the legacy point+mesh behavior
    uint64_t lastPointDraws = 0, lastIndexedDraws = 0;
    bool ready = false;
    std::string deviceNameCache; // populated at create time, kept alive for nkv_get_device_name()
};

std::mutex gMutex;

// ONE definition of "safe VRAM". SurfaceRenderer.cpp and nkv_get_surface_memory
// apply the same 0.70 inline; this constant exists so the point-cloud budget
// (nkv_get_device_vram_budget) cannot drift away from the surface budget. If the
// margin is ever tuned, it is tuned HERE and both paths follow.
constexpr double kVramBudgetFraction = 0.70;
std::unique_ptr<Instance> gInstance;
NkvHandle gNextHandle = 1;
NkvHandle gActiveHandle = 0;
std::string gLastError;

void SetError(const char* msg) {
    gLastError = msg ? msg : "";
}

Instance* Resolve(NkvHandle h) {
    if (h == 0 || h != gActiveHandle || !gInstance) return nullptr;
    return gInstance.get();
}

} // namespace

NKV_API int nkv_is_available(void) {
    // Cheapest possible probe: try to load the Vulkan loader via volk and
    // create a throwaway VkInstance. Never touches gInstance/gActiveHandle.
    //
    // IMPORTANT: volkGetInstanceVersion() (and every other volk-dispatched
    // call) reads through a function-pointer table that is all-zero until
    // volkInitialize() has run once in this process - calling it first as
    // a "is volk already up" check dereferences a null function pointer
    // and crashes (this was caught empirically via ctypes: an
    // OSError "access violation" on the Python side). volkInitialize()
    // itself is safe to call more than once, so just always call it.
    static bool volkReady = false;
    if (!volkReady) {
        VkResult r = volkInitialize();
        if (r != VK_SUCCESS) {
            SetError("volkInitialize failed - no Vulkan loader on this system");
            return 0;
        }
        volkReady = true;
    }
    VkApplicationInfo appInfo{};
    appInfo.sType = VK_STRUCTURE_TYPE_APPLICATION_INFO;
    appInfo.apiVersion = VK_API_VERSION_1_1;
    VkInstanceCreateInfo ci{};
    ci.sType = VK_STRUCTURE_TYPE_INSTANCE_CREATE_INFO;
    ci.pApplicationInfo = &appInfo;
    VkInstance probe = VK_NULL_HANDLE;
    VkResult r = vkCreateInstance(&ci, nullptr, &probe);
    if (r != VK_SUCCESS || probe == VK_NULL_HANDLE) {
        SetError("vkCreateInstance probe failed - no usable Vulkan driver");
        return 0;
    }
    volkLoadInstance(probe); // load instance-level dispatch table before using it
    uint32_t deviceCount = 0;
    vkEnumeratePhysicalDevices(probe, &deviceCount, nullptr);
    vkDestroyInstance(probe, nullptr);
    if (deviceCount == 0) {
        SetError("no Vulkan-capable physical device");
        return 0;
    }
    return 1;
}

NKV_API NkvHandle nkv_create_renderer(void* hwnd, uint32_t width, uint32_t height,
                                       int enable_validation) {
    std::lock_guard<std::mutex> lock(gMutex);
    if (gInstance) {
        SetError("a renderer already exists in this process; call "
                  "nkv_destroy_renderer first");
        return 0;
    }
    if (hwnd == nullptr) {
        SetError("hwnd is null");
        return 0;
    }
    auto inst = std::make_unique<Instance>();
    RendererConfig cfg;
    cfg.nativeWindowHandle = hwnd;
    cfg.width = width > 0 ? width : 1;
    cfg.height = height > 0 ? height : 1;
    cfg.enableValidation = enable_validation != 0;
    cfg.appName = "NakshaLidar";
    cfg.shaderDirectory = ThisDllDirectory();
    try {
        if (!inst->renderer.Initialize(cfg)) {
            SetError("Renderer::Initialize failed");
            return 0;
        }
        if (!inst->pointCloud.Initialize(inst->renderer)) {
            SetError("PointCloudRenderer::Initialize failed");
            inst->renderer.Shutdown();
            return 0;
        }
        if (!inst->surface.Initialize(inst->renderer)) {
            SetError("SurfaceRenderer::Initialize failed");
            inst->pointCloud.Shutdown();
            inst->renderer.Shutdown();
            return 0;
        }
    } catch (const std::exception& e) {
        SetError(e.what());
        return 0;
    } catch (...) {
        SetError("unknown native exception during renderer creation");
        return 0;
    }
    inst->ready = true;
    inst->deviceNameCache = inst->renderer.GetContext().GetReport().deviceName;
    gInstance = std::move(inst);
    gActiveHandle = gNextHandle++;
    return gActiveHandle;
}

NKV_API void nkv_destroy_renderer(NkvHandle handle) {
    std::lock_guard<std::mutex> lock(gMutex);
    if (!gInstance || handle != gActiveHandle) return;
    try {
        gInstance->renderer.WaitIdle();
        gInstance->surface.Shutdown();
        gInstance->pointCloud.Shutdown();
        gInstance->renderer.Shutdown();
    } catch (...) {
        // Never let shutdown throw across the C boundary.
    }
    gInstance.reset();
    gActiveHandle = 0;
}

/* ---------------------------------------------------------------------------
 * Derive the floating render origin from the data itself, when the caller has
 * not set one explicitly.
 *
 * WHY THIS EXISTS (facet-appearance mismatch, zoomed in)
 * ------------------------------------------------------
 * GPU vertex buffers are float32, but the world coordinates are UTM-scale and
 * float64-authoritative. This dataset sits at y ~ 4.78e6, where one float32 ulp
 * is 0.5 m, while the TIN triangles are ~0.13 m across. Converting with
 * origin = (0,0,0) therefore collapses neighbouring vertices onto the SAME
 * float32 value: triangles lose their shape, computed face normals become
 * wrong or degenerate, and "sharp triangular facets" degrade into mush. It is
 * invisible when the whole cloud is framed (every facet is sub-pixel) and
 * obvious when zoomed in - exactly the reported symptom.
 *
 * The engine already implements the floating-origin rule everywhere else:
 *   - RenderOrigin::WorldToRenderF64ToF32() subtracts the origin on upload
 *   - Camera::GetViewProjectionFloat(origin, ...) builds the MVP in RENDER
 *     space via ApplyToCore(origin)
 *   - the frame UBO carries origin for shaders that need world position
 * so engaging it is a one-line change in behaviour, not a new coordinate
 * system. Choosing the data centre keeps |renderPos| within the cloud extent,
 * which is what restores centimetre-level float32 precision.
 *
 * An explicitly-set origin is always left alone (nkv_set_render_origin wins),
 * so a caller can still pin the origin deliberately.
 * ------------------------------------------------------------------------- */
static void AutoDeriveRenderOrigin(Renderer& renderer, const double* positions,
                                   std::size_t vertexCount) {
    if (positions == nullptr || vertexCount == 0) return;
    // Already pinned by the caller: respect it.
    const RenderOrigin& existing = renderer.GetOrigin();
    if (existing.x != 0.0 || existing.y != 0.0 || existing.z != 0.0) return;

    double lo[3] = { 1e300, 1e300, 1e300 };
    double hi[3] = { -1e300, -1e300, -1e300 };
    const std::size_t n = vertexCount * 3;
    for (std::size_t i = 0; i < n; i += 3) {
        for (int k = 0; k < 3; ++k) {
            const double v = positions[i + k];
            if (!(v == v)) continue;                 // skip NaN
            if (v < lo[k]) lo[k] = v;
            if (v > hi[k]) hi[k] = v;
        }
    }
    if (!(lo[0] <= hi[0]) || !(lo[1] <= hi[1]) || !(lo[2] <= hi[2])) return;
    renderer.SetOrigin(0.5 * (lo[0] + hi[0]), 0.5 * (lo[1] + hi[1]),
                       0.5 * (lo[2] + hi[2]));
}

NKV_API int nkv_set_point_cloud(NkvHandle handle,
                                 const double* xyz, const uint8_t* rgb,
                                 const uint8_t* classification,
                                 const float* intensity,
                                 uint64_t count) {
    std::lock_guard<std::mutex> lock(gMutex);
    Instance* inst = Resolve(handle);
    if (!inst || !xyz || count == 0) {
        SetError("invalid handle or null xyz/zero count");
        return 0;
    }
    try {
        // Floating origin: derive it from the cloud's bounds before the
        // float64->float32 conversion (see AutoDeriveRenderOrigin).
        AutoDeriveRenderOrigin(inst->renderer, xyz, static_cast<std::size_t>(count));
        inst->pointCloud.UploadPositionsWorldF64(xyz, static_cast<std::size_t>(count),
                                                   inst->renderer.GetOrigin());
        if (rgb) {
            inst->pointCloud.UploadColorsRGB8(rgb, static_cast<std::size_t>(count));
        }
        if (classification) {
            inst->pointCloud.UploadClassificationU8(classification, static_cast<std::size_t>(count));
        }
        // Intensity is a bound vertex stream (R32_SFLOAT): point.vert uses it
        // for the intensity ramp and to modulate classification colours.
        if (intensity) {
            inst->pointCloud.UploadIntensityF32(intensity, static_cast<std::size_t>(count));
        }
        // PHASE 1: this comment used to claim intensity-mode colouring was "not
        // yet implemented natively". That was true when intensity was a texture
        // bake and is now wrong: intensity is uploaded above as a real vertex
        // stream and nkv_set_point_intensity_range + the intensity LUT drive it.
        // It is corrected here rather than left to mislead the next reader.
    } catch (const std::exception& e) {
        SetError(e.what());
        return 0;
    }
    return 1;
}

NKV_API int nkv_set_display_mode(NkvHandle handle, int mode /* NkvDisplayMode */) {
    Instance* inst = Resolve(handle);
    if (!inst) return 0;
    // point.vert branches on this value (pc.shading.x): NKV_DISPLAY_RGB reads the
    // uploaded RGBA8, CLASSIFICATION does a class-palette lookup, INTENSITY and
    // ELEVATION normalise the attribute and sample their ramp LUT. Push constant
    // only - no attribute buffer is touched, so switching modes costs nothing.
    inst->pointCloud.SetDisplayMode(mode);
    // Mode 4 additionally routes the FRAME through the two-pass instant path
    // (see Renderer::RecordInstantShadedPass). Same contract: this writes two
    // members and nothing else - no upload, no mesh, no vkDeviceWaitIdle. Any
    // other mode hands the frame back to the ordinary point/surface Record().
    inst->renderer.SetInstantShadedActive(mode == NKV_DISPLAY_SHADED_CLASS_INSTANT);
    return 1;
}

NKV_API int nkv_set_instant_shading_parameters(NkvHandle handle,
                                               float azimuth_deg,
                                               float elevation_deg,
                                               float ambient,
                                               int debug_stage) {
    Instance* inst = Resolve(handle);
    if (!inst) return 0;
    inst->renderer.SetInstantShading(azimuth_deg, elevation_deg, ambient);
    inst->renderer.SetInstantDebugStage(debug_stage);
    return 1;
}

NKV_API int nkv_set_instant_normal_radius(NkvHandle handle, int radius_texels) {
    Instance* inst = Resolve(handle);
    if (!inst) return 0;
    inst->renderer.SetInstantNormalRadius(radius_texels);
    return 1;
}

NKV_API uint64_t nkv_get_instant_shaded_frame_count(NkvHandle handle) {
    Instance* inst = Resolve(handle);
    if (!inst) return 0;
    return inst->renderer.GetInstantFrameCount();
}

NKV_API int nkv_get_instant_shaded_available(NkvHandle handle) {
    Instance* inst = Resolve(handle);
    if (!inst) return 0;
    return inst->renderer.IsInstantShadedAvailable() ? 1 : 0;
}

NKV_API int nkv_set_point_normals(NkvHandle handle, const uint8_t* packed,
                                   uint64_t point_count) {
    std::lock_guard<std::mutex> lock(gMutex);
    Instance* inst = Resolve(handle);
    if (!inst) return 0;
    if (!packed) { SetError("packed normal buffer is NULL"); return 0; }
    if (point_count == 0) { SetError("point_count is 0"); return 0; }
    inst->pointCloud.UploadNormalsOct16(packed,
                                        static_cast<std::size_t>(point_count));
    return 1;
}

NKV_API uint64_t nkv_get_point_normal_count(NkvHandle handle) {
    Instance* inst = Resolve(handle);
    if (!inst) return 0;
    return static_cast<uint64_t>(inst->pointCloud.GetNormalCount());
}

NKV_API uint64_t nkv_get_point_normal_bytes(NkvHandle handle) {
    Instance* inst = Resolve(handle);
    if (!inst) return 0;
    return static_cast<uint64_t>(inst->pointCloud.GetNormalBytes());
}

NKV_API uint64_t nkv_get_point_normal_upload_count(NkvHandle handle) {
    Instance* inst = Resolve(handle);
    if (!inst) return 0;
    return inst->pointCloud.GetNormalUploadCount();
}

NKV_API int nkv_set_instant_normal_source(NkvHandle handle, int screen_space) {
    Instance* inst = Resolve(handle);
    if (!inst) return 0;
    inst->renderer.SetInstantNormalSource(screen_space);
    return 1;
}

NKV_API int nkv_set_class_visibility(NkvHandle handle, const uint8_t* visible256) {
    std::lock_guard<std::mutex> lock(gMutex);
    Instance* inst = Resolve(handle);
    if (!inst) { SetError("invalid handle"); return 0; }
    if (!visible256) { SetError("visible256 is NULL"); return 0; }
    inst->renderer.SetClassVisibility(visible256);
    return 1;
}

NKV_API uint64_t nkv_get_class_visibility_update_count(NkvHandle handle) {
    Instance* inst = Resolve(handle);
    if (!inst) return 0;
    return inst->renderer.GetClassVisibilityUpdateCount();
}

NKV_API int nkv_set_point_size(NkvHandle handle, float pixels) {
    Instance* inst = Resolve(handle);
    if (!inst) return 0;
    inst->pointCloud.SetPointSize(pixels);
    return 1;
}

/* Adaptive sizing band. footprint_m is the physical laser footprint in world
 * units: the shader converts it to pixels with the frame's own projection scale
 * and divides by view depth, then clamps to [min_px, max_px] so the cloud keeps
 * VTK-like density at every zoom instead of collapsing to 1px specks or
 * ballooning into blobs. class_intensity_mix (0..1) blends return strength into
 * classification colours. Pass min_px == max_px for a fixed pixel size. */
NKV_API int nkv_set_point_size_params(NkvHandle handle, float footprint_m, float min_px,
                                      float max_px, float class_intensity_mix) {
    std::lock_guard<std::mutex> lock(gMutex);
    Instance* inst = Resolve(handle);
    if (!inst) { SetError("invalid handle"); return 0; }
    inst->pointCloud.SetPointSizeParams(footprint_m, min_px, max_px, class_intensity_mix);
    return 1;
}

/* Ramp endpoints, in the dataset's own units. Python computes them the same way
 * gui/pointcloud_display.py does (percentile-clamped, not raw min/max) so the
 * GPU ramp maps a point to the same colour VTK would. */
NKV_API int nkv_set_point_elevation_range(NkvHandle handle, float lo, float hi, float gamma) {
    std::lock_guard<std::mutex> lock(gMutex);
    Instance* inst = Resolve(handle);
    if (!inst) { SetError("invalid handle"); return 0; }
    inst->pointCloud.SetElevationRange(lo, hi, gamma);
    return 1;
}

NKV_API int nkv_set_point_intensity_range(NkvHandle handle, float lo, float hi,
                                          float contrast, float gamma) {
    std::lock_guard<std::mutex> lock(gMutex);
    Instance* inst = Resolve(handle);
    if (!inst) { SetError("invalid handle"); return 0; }
    inst->pointCloud.SetIntensityRange(lo, hi, contrast, gamma);
    return 1;
}

/* Depth normalisation for NKV_DISPLAY_DEPTH. Push-constant only.
 *
 * Depth is the distance from the eye, which the legacy VTK mode defines exactly
 * this way (gui/naksha_cache/display_modes.py depth_lut(): "Depth is
 * CAMERA/VIEW dependent in the legacy path (it shades by distance from the
 * eye)"). Computing it in the shader means Depth needs no attribute stream, no
 * second geometry pass and no XYZ upload - and it updates automatically with
 * the camera in both the 2D orthographic and the 3D perspective projection,
 * because the eye is always known to the frame UBO. */
NKV_API int nkv_set_point_depth_range(NkvHandle handle, float lo, float hi,
                                      float gamma) {
    std::lock_guard<std::mutex> lock(gMutex);
    Instance* inst = Resolve(handle);
    if (!inst) { SetError("invalid handle"); return 0; }
    inst->pointCloud.SetDepthRange(lo, hi, gamma);
    return 1;
}

/* Sprite shaping: softness 0 = hard disc, ~0.35 = anti-aliased rim (the value
 * that makes a sparse cloud read as continuous when blended). */
NKV_API int nkv_set_point_sprite_params(NkvHandle handle, float softness, float brightness) {
    std::lock_guard<std::mutex> lock(gMutex);
    Instance* inst = Resolve(handle);
    if (!inst) { SetError("invalid handle"); return 0; }
    inst->pointCloud.SetSpriteParams(softness, brightness);
    return 1;
}

/* The three 256*3 uint8 colour tables the point shader reads (see
 * Renderer::SetPointLUTs). Any argument may be NULL to leave that table alone.
 * Python builds them from gui/pointcloud_display.py's own colouring helpers, so
 * "GPU-side shading" here means the same tables VTK's lookup tables produce -
 * not a lookalike ramp. */
NKV_API int nkv_set_point_luts(NkvHandle handle,
                               const uint8_t* class_rgb,
                               const uint8_t* elevation_rgb,
                               const uint8_t* intensity_rgb) {
    std::lock_guard<std::mutex> lock(gMutex);
    Instance* inst = Resolve(handle);
    if (!inst) { SetError("invalid handle"); return 0; }
    inst->renderer.SetPointLUTs(class_rgb, elevation_rgb, intensity_rgb);
    return 1;
}

/* Proof that an intensity channel really reached the GPU (0 when the dataset
 * had none or the upload failed). */
NKV_API uint64_t nkv_get_point_intensity_upload_count(NkvHandle handle) {
    Instance* inst = Resolve(handle);
    if (!inst) return 0;
    return inst->pointCloud.GetIntensityUploadCount();
}

/* ---- Attribute-only stream upload (PHASE 1) ------------------------------
 *
 * The reason this exists at all: a display-mode switch must be able to ADD a
 * required attribute (Class needs classification, Intensity needs intensity)
 * without touching the position buffer. Re-calling nkv_set_point_cloud would
 * work functionally but would move positionUploads_, which is precisely the
 * counter the "no XYZ upload on a mode switch" guarantee is measured with.
 *
 * The count guard is the whole safety story. Both streams are bound by point
 * INDEX, so accepting a stream whose length disagrees with the resident cloud
 * would attribute-shift every point past the end - a silent corruption that no
 * per-block test can catch. Refuse the whole call instead: a display mode that
 * cannot be satisfied must report PENDING, not draw wrong colours. */
NKV_API int nkv_set_point_attributes(NkvHandle handle,
                                     const uint8_t* classification,
                                     const float* intensity,
                                     uint64_t count) {
    std::lock_guard<std::mutex> lock(gMutex);
    Instance* inst = Resolve(handle);
    if (!inst) return 0;
    if (!classification && !intensity) {
        SetError("both attribute pointers are NULL");
        return 0;
    }
    const std::size_t resident = inst->pointCloud.GetPointCount();
    if (count == 0 || resident == 0) {
        SetError("attribute upload with zero count or no resident cloud");
        return 0;
    }
    if (static_cast<std::size_t>(count) != resident) {
        SetError("attribute count does not match the resident point count");
        return 0;
    }
    try {
        if (classification) {
            inst->pointCloud.UploadClassificationU8(classification, resident);
        }
        if (intensity) {
            inst->pointCloud.UploadIntensityF32(intensity, resident);
        }
    } catch (const std::exception& e) {
        SetError(e.what());
        return 0;
    }
    return 1;
}

NKV_API uint64_t nkv_get_point_classification_count(NkvHandle handle) {
    Instance* inst = Resolve(handle);
    if (!inst) return 0;
    return inst->pointCloud.GetClassificationCount();
}

NKV_API uint64_t nkv_get_point_intensity_count(NkvHandle handle) {
    Instance* inst = Resolve(handle);
    if (!inst) return 0;
    return inst->pointCloud.GetIntensityCount();
}

NKV_API uint64_t nkv_get_point_position_upload_count(NkvHandle handle) {
    Instance* inst = Resolve(handle);
    if (!inst) return 0;
    return inst->pointCloud.GetPositionUploadCount();
}

/* Show/hide the resident point cloud. Shaded Classification and Surface present
 * a TIN mesh instead of the raw cloud, and the app hides the point actors for
 * that reason; without this the raw points stay in front of the mesh. Buffers
 * stay resident, so this is a flag write - never an upload. */
NKV_API int nkv_set_point_cloud_visible(NkvHandle handle, int visible) {
    std::lock_guard<std::mutex> lock(gMutex);
    Instance* inst = Resolve(handle);
    if (!inst) return 0;
    inst->pointCloud.SetVisible(visible != 0);
    return 1;
}

NKV_API int nkv_get_point_cloud_visible(NkvHandle handle) {
    Instance* inst = Resolve(handle);
    if (!inst) return -1;
    return inst->pointCloud.IsVisible() ? 1 : 0;
}

/* ---- Draw / LUT telemetry (diagnostics; all counters are free) ---------- */

/* Colour-table replacements. Rises on every PTC / palette / visibility /
 * weight / display-mode re-tint. Pair it with nkv_get_point_position_upload_count:
 * lut rising WHILE positions stay flat is the proof that a re-tint never
 * re-uploads the point buffer. */
NKV_API uint64_t nkv_get_lut_update_count(NkvHandle handle) {
    Instance* inst = Resolve(handle);
    if (!inst) return 0;
    return inst->renderer.GetLutUpdateCount();
}

/* vkCmdDraw submissions. */
NKV_API uint64_t nkv_get_point_draw_call_count(NkvHandle handle) {
    Instance* inst = Resolve(handle);
    if (!inst) return 0;
    return inst->pointCloud.GetDrawCallCount();
}
NKV_API uint64_t nkv_get_surface_draw_call_count(NkvHandle handle) {
    Instance* inst = Resolve(handle);
    if (!inst) return 0;
    return inst->surface.GetDrawCallCount();
}
NKV_API uint64_t nkv_get_surface_overlay_draw_call_count(NkvHandle handle) {
    Instance* inst = Resolve(handle);
    if (!inst) return 0;
    return inst->surface.GetOverlayDrawCallCount();
}

/* Device point-size ceiling, limits.pointSizeRange[1] (float because Vulkan
 * reports it as a range). The shader clamps gl_PointSize to this. */
NKV_API int nkv_get_max_point_size(NkvHandle handle, float* out) {
    Instance* inst = Resolve(handle);
    if (!inst || !out) return 0;
    *out = inst->pointCloud.GetMaxPointSizeDevice();
    return 1;
}

NKV_API int nkv_get_render_origin(NkvHandle handle, double* out3) {
    Instance* inst = Resolve(handle);
    if (!inst || !out3) return 0;
    const RenderOrigin& o = inst->renderer.GetOrigin();
    out3[0] = o.x; out3[1] = o.y; out3[2] = o.z;
    return 1;
}

/* ---- ORTHOGRAPHIC (VTK parallel) camera ---------------------------------
 * The real projection, not a perspective FOV approximation: this is what the
 * Naksha main view actually is (vtkCamera with ParallelProjection = true).
 *
 * State is exactly what the task specifies - centre, parallel scale, viewport
 * aspect, near, far - and NOTHING else. There is no eye distance to move:
 * zooming scales `parallelScale` and panning moves `centre`, so a zoom can
 * no longer change the projection the way a dolly did, and no point can
 * stretch (the last matrix row is (0,0,0,1): w_clip = 1, no perspective
 * divide).
 *
 * `viewDir` is the unit direction the camera looks along. For the Naksha 2D
 * plan view that is (0,0,-1) (straight down -Z) with world up +Y, which makes
 * screen-right = +X and screen-up = +Y, matching VTK.
 */
NKV_API int nkv_set_camera_ortho(NkvHandle handle,
                                 double centre_x, double centre_y, double centre_z,
                                 double dir_x, double dir_y, double dir_z,
                                 double parallel_scale,
                                 double aspect, double near_clip, double far_clip) {
    std::lock_guard<std::mutex> lock(gMutex);
    Instance* inst = Resolve(handle);
    if (!inst) { SetError("invalid handle"); return 0; }
    const double centre[3] = {centre_x, centre_y, centre_z};
    const double dir[3] = {dir_x, dir_y, dir_z};
    inst->renderer.GetCamera().SetOrthographicWorld(
        centre, dir, parallel_scale, aspect, near_clip, far_clip);
    return 1;
}

NKV_API int nkv_set_camera_perspective_mode(NkvHandle handle) {
    std::lock_guard<std::mutex> lock(gMutex);
    Instance* inst = Resolve(handle);
    if (!inst) { SetError("invalid handle"); return 0; }
    inst->renderer.GetCamera().SetPerspectiveMode();
    return 1;
}

NKV_API int nkv_get_camera_projection(NkvHandle handle, int* out_is_ortho,
                                      double* out_parallel_scale) {
    Instance* inst = Resolve(handle);
    if (!inst || !out_is_ortho || !out_parallel_scale) return 0;
    const naksha::Camera& cam = inst->renderer.GetCamera();
    *out_is_ortho = cam.IsOrthographic() ? 1 : 0;
    *out_parallel_scale = cam.GetParallelScale();
    return 1;
}

// Expands a per-face-coloured indexed mesh into a flat-shaded triangle soup
// in RENDER space (float32), which is the layout both the legacy
// SurfaceRenderer upload and the new tiled/LOD path consume.
//
// Shared by nkv_set_surface and nkv_set_surface_tiled so the two paths cannot
// drift: identical validation, identical origin handling, identical memory
// layout. Returns false with nkv_last_error() set on any validation failure.
bool ExpandFaceColoredSoup(Renderer& renderer, const double* positions,
                           uint64_t vertex_count, const int32_t* faces,
                           uint64_t face_count, const uint8_t* face_colors_rgb,
                           std::vector<float>& posF32, std::vector<float>& nrmF32,
                           std::vector<uint8_t>& colU8) {
    const std::size_t triCount = static_cast<std::size_t>(face_count);
    // Face indices are caller-owned raw memory: a single out-of-range or
    // negative value would be dereferenced below and read far outside the
    // positions array (an access violation, not a clean failure). Scan once up
    // front and reject the whole upload with a diagnostic.
    const int64_t vertexSpan = static_cast<int64_t>(vertex_count);
    for (std::size_t i = 0; i < triCount * 3; ++i) {
        const int64_t vi = faces[i];
        if (vi < 0 || vi >= vertexSpan) {
            char buf[192];
            std::snprintf(buf, sizeof(buf),
                          "face index %lld out of range [0, %lld) at element %zu "
                          "(vertex_count=%llu, face_count=%llu)",
                          static_cast<long long>(vi),
                          static_cast<long long>(vertexSpan), i,
                          static_cast<unsigned long long>(vertex_count),
                          static_cast<unsigned long long>(face_count));
            SetError(buf);
            return false;
        }
    }
    posF32.resize(triCount * 9);
    nrmF32.assign(triCount * 9, 0.0f);
    // RGBA8, 4 bytes per vertex: the colour vertex format is
    // VK_FORMAT_R8G8B8A8_UNORM and the upload copies vertexCount*4 bytes.
    colU8.assign(triCount * 12, 255);
    const RenderOrigin& origin = renderer.GetOrigin();
    for (std::size_t f = 0; f < triCount; ++f) {
        for (int k = 0; k < 3; ++k) {
            const int32_t vi = faces[f * 3 + k];
            double out[3];
            origin.WorldToRender(positions[vi * 3 + 0], positions[vi * 3 + 1],
                                 positions[vi * 3 + 2], out);
            const std::size_t dst = (f * 3 + k);
            posF32[dst * 3 + 0] = static_cast<float>(out[0]);
            posF32[dst * 3 + 1] = static_cast<float>(out[1]);
            posF32[dst * 3 + 2] = static_cast<float>(out[2]);
            colU8[dst * 4 + 0] = face_colors_rgb[f * 3 + 0];
            colU8[dst * 4 + 1] = face_colors_rgb[f * 3 + 1];
            colU8[dst * 4 + 2] = face_colors_rgb[f * 3 + 2];
            colU8[dst * 4 + 3] = 255;
        }
    }
    return true;
}

NKV_API int nkv_set_surface(NkvHandle handle,
                             const double* positions, uint64_t vertex_count,
                             const int32_t* faces, uint64_t face_count,
                             const uint8_t* face_colors_rgb) {
    std::lock_guard<std::mutex> lock(gMutex);
    Instance* inst = Resolve(handle);
    if (!inst || !positions || !faces || !face_colors_rgb || vertex_count == 0 || face_count == 0) {
        SetError("invalid handle or null/empty surface arrays");
        return 0;
    }
    try {
        // Floating origin: derive it from THIS surface's bounds before any
        // float32 conversion, so UTM-scale coordinates do not quantise the
        // triangles away (see AutoDeriveRenderOrigin). Must run BEFORE the
        // origin reference below is taken.
        AutoDeriveRenderOrigin(inst->renderer, positions, vertex_count);
        // Expand to a flat-shaded triangle soup: SurfaceRenderer's vertex
        // buffer is per-vertex, but gui/surface_mode.py's contract hands us
        // PER-FACE colors that are already fully baked (ramp*shade). Rather
        // than approximate that in the shader, duplicate each face's 3
        // vertices with the face's single baked color attached to all 3 -
        // this reproduces flat shading exactly using NKV_SHADE_PASSTHROUGH.
        const std::size_t triCount = static_cast<std::size_t>(face_count);
        std::vector<float> posF32;
        std::vector<float> nrmF32;
        std::vector<uint8_t> colU8;
        if (!ExpandFaceColoredSoup(inst->renderer, positions, vertex_count, faces,
                                   face_count, face_colors_rgb, posF32, nrmF32, colU8)) {
            return 0;
        }
        std::vector<uint32_t> idx(triCount * 3);
        for (std::size_t i = 0; i < idx.size(); ++i) idx[i] = static_cast<uint32_t>(i);
        inst->surface.UploadMesh(posF32.data(), nrmF32.data(), colU8.data(),
                                  triCount * 3, idx.data(), idx.size());
        // ambient=1.0 with the existing SurfaceRenderer push-constant formula
        // (shade = ambient + (1-ambient)*clip(dot,0,1)) makes shade==1
        // unconditionally, i.e. a pass-through of the CPU-baked color - see
        // NKV_SHADE_PASSTHROUGH in the header. This does NOT port formula
        // (A)/(B) into GLSL; it reuses the CPU-computed result as-is.
        inst->surface.SetAmbient(1.0f);
        inst->surface.SetShadingMode(ShadingMode::Flat);
    } catch (const std::exception& e) {
        SetError(e.what());
        return 0;
    }
    return 1;
}

NKV_API int nkv_set_dataset_revision(NkvHandle handle, uint64_t revision) {
    Instance* inst = Resolve(handle);
    if (!inst) return 0;
    inst->surface.SetDatasetRevision(revision);
    return 1;
}

NKV_API int nkv_set_surface_revision(NkvHandle handle, uint64_t revision) {
    Instance* inst = Resolve(handle);
    if (!inst) return 0;
    inst->surface.SetSurfaceRevision(revision);
    return 1;
}

NKV_API uint64_t nkv_get_dataset_revision(NkvHandle handle) {
    Instance* inst = Resolve(handle);
    return inst ? inst->surface.GetDatasetRevision() : 0;
}

NKV_API uint64_t nkv_get_surface_revision(NkvHandle handle) {
    Instance* inst = Resolve(handle);
    return inst ? inst->surface.GetSurfaceRevision() : 0;
}

NKV_API int nkv_set_surface_tiled(NkvHandle handle,
                                  const double* positions, uint64_t vertex_count,
                                  const int32_t* faces, uint64_t face_count,
                                  const uint8_t* face_colors_rgb,
                                  uint32_t tile_count, float base_cell_m,
                                  int is_preview,
                                  uint64_t dataset_revision,
                                  uint64_t surface_revision) {
    std::lock_guard<std::mutex> lock(gMutex);
    Instance* inst = Resolve(handle);
    if (!inst || !positions || !faces || !face_colors_rgb ||
        vertex_count == 0 || face_count == 0) {
        SetError("invalid handle or null/empty surface arrays");
        return 0;
    }
    try {
        AutoDeriveRenderOrigin(inst->renderer, positions, vertex_count);
        std::vector<float> posF32;
        std::vector<float> nrmF32;
        std::vector<uint8_t> colU8;
        if (!ExpandFaceColoredSoup(inst->renderer, positions, vertex_count, faces,
                                   face_count, face_colors_rgb, posF32, nrmF32, colU8)) {
            return 0;
        }
        // An automatic tile count keeps tiles roughly balanced in triangle
        // terms: a few hundred is enough for culling to bite at high zoom
        // without the per-tile draw overhead dominating.
        uint32_t tiles = tile_count;
        if (tiles == 0) {
            const std::size_t tris = static_cast<std::size_t>(face_count);
            tiles = static_cast<uint32_t>(
                std::min<std::size_t>(512, std::max<std::size_t>(16, tris / 20000)));
        }
        // Base cell size defaults to the mesh's own mean triangle edge, so
        // the coarse LOD levels come from the real geometry rather than a
        // guessed constant.
        float cell = base_cell_m;
        if (!(cell > 0.0f)) {
            double perimeter = 0.0;
            const std::size_t sample = std::min<std::size_t>(face_count, 4096);
            for (std::size_t f = 0; f < sample; ++f) {
                const float* a = &posF32[(f * 3 + 0) * 3];
                const float* b = &posF32[(f * 3 + 1) * 3];
                const float* c = &posF32[(f * 3 + 2) * 3];
                perimeter += std::sqrt((a[0]-b[0])*(a[0]-b[0]) + (a[1]-b[1])*(a[1]-b[1]) + (a[2]-b[2])*(a[2]-b[2]));
                perimeter += std::sqrt((b[0]-c[0])*(b[0]-c[0]) + (b[1]-c[1])*(b[1]-c[1]) + (b[2]-c[2])*(b[2]-c[2]));
                perimeter += std::sqrt((c[0]-a[0])*(c[0]-a[0]) + (c[1]-a[1])*(c[1]-a[1]) + (c[2]-a[2])*(c[2]-a[2]));
            }
            const double meanEdge = sample ? (perimeter / (sample * 3.0)) : 1.0;
            cell = static_cast<float>(std::max(0.05, meanEdge));
        }
        auto r = inst->surface.UploadTiledMesh(
            posF32.data(), nrmF32.data(), colU8.data(),
            static_cast<std::size_t>(face_count), tiles, cell,
            is_preview != 0, dataset_revision, surface_revision);
        if (!r.ok) {
            if (r.memoryRejected) {
                SetError("surface upload refused: ACTIVE + PENDING + new set would "
                         "exceed the safe VRAM budget; previous surface kept");
            } else {
                SetError("tiled surface upload failed (tiling, LOD build or VMA)");
            }
            return 0;
        }
        // Passthrough of the CPU-baked per-face colour (see NKV_SHADE_PASSTHROUGH).
        inst->surface.SetAmbient(1.0f);
        inst->surface.SetShadingMode(ShadingMode::Flat);
    } catch (const std::exception& e) {
        SetError(e.what());
        return 0;
    }
    return 1;
}

NKV_API int nkv_set_surface_indexed_blocks(NkvHandle handle, const double* positions,
    uint64_t vertices, const uint32_t* indices, uint64_t triangles,
    const uint8_t* cell_rgb, const uint32_t* block_index_counts, uint32_t blocks,
    uint64_t dataset_revision, uint64_t surface_revision) {
    std::lock_guard<std::mutex> lock(gMutex);
    Instance* inst = Resolve(handle);
    if (!inst || !positions || !indices || !cell_rgb || !block_index_counts || !vertices || !triangles || !blocks) return 0;
    try {
        std::vector<float> local(vertices * 3);
        for (uint64_t i = 0; i < vertices; ++i) {
            double value[3]; inst->renderer.GetOrigin().WorldToRender(positions[i*3], positions[i*3+1], positions[i*3+2], value);
            for (int k = 0; k < 3; ++k) {
                if (!std::isfinite(value[k])) { SetError("nonfinite Surface position"); return 0; }
                local[i*3+k] = static_cast<float>(value[k]);
            }
        }
        std::vector<uint32_t> colors(triangles);
        for (uint64_t i = 0; i < triangles; ++i)
            colors[i] = cell_rgb[i*3] | (uint32_t(cell_rgb[i*3+1]) << 8) | (uint32_t(cell_rgb[i*3+2]) << 16) | 0xff000000u;
        auto result = inst->surface.UploadIndexedBlocks(local.data(), vertices, indices, triangles*3,
            colors.data(), block_index_counts, blocks, dataset_revision, surface_revision);
        if (!result.ok) { SetError(result.memoryRejected ? "Surface VRAM budget refused" : "indexed Surface upload failed"); return 0; }
        return 1;
    } catch (const std::exception& error) { SetError(error.what()); return 0; }
}

NKV_API int nkv_set_streaming_surface_active(NkvHandle handle, int active) {
    std::lock_guard<std::mutex> lock(gMutex);
    Instance* inst = Resolve(handle);
    if (!inst || (active && !inst->surface.IsLoaded())) return 0;
    inst->streamingSurface = active ? 1 : 0;
    if (active) inst->renderer.SetInstantShadedActive(false);
    return 1;
}

NKV_API int nkv_get_surface_draw_proof(NkvHandle handle, uint64_t* out_counts) {
    std::lock_guard<std::mutex> lock(gMutex);
    Instance* inst = Resolve(handle);
    if (!inst || !out_counts) return 0;
    out_counts[0] = inst->streamingSurface == 1;
    out_counts[1] = inst->lastPointDraws;
    out_counts[2] = inst->lastPointDraws ? inst->pointCloud.GetPointCount() : 0;
    out_counts[3] = inst->lastIndexedDraws;
    out_counts[4] = inst->streamingSurface == 1 ? inst->surface.GetVisibleTriangleCount() : 0;
    // Extended proof (see nkv_get_surface_draw_proof's contract in the header):
    // these make "is the Surface actually drawn as indexed triangles" a
    // measured answer instead of an inference from a screenshot.
    out_counts[5] = inst->surface.GetVisibleTileCount();
    out_counts[6] = inst->surface.GetTotalTileCount();
    out_counts[7] = inst->surface.GetSelectedLod();
    out_counts[8] = static_cast<uint64_t>(inst->surface.GetState());
    out_counts[9] = inst->surface.GetVertexCount();
    out_counts[10] = inst->surface.GetIndexCount();
    out_counts[11] = inst->surface.GetTriangleCount();
    return 1;
}

NKV_API int nkv_activate_pending_surface(NkvHandle handle, int* discarded_stale) {
    std::lock_guard<std::mutex> lock(gMutex);
    Instance* inst = Resolve(handle);
    if (discarded_stale) *discarded_stale = 0;
    if (!inst) return 0;
    bool discarded = false;
    const bool swapped = inst->surface.ActivatePendingIfFresh(&discarded);
    if (discarded_stale) *discarded_stale = discarded ? 1 : 0;
    return swapped ? 1 : 0;
}

NKV_API uint32_t nkv_get_surface_total_tile_count(NkvHandle handle) {
    Instance* inst = Resolve(handle);
    return inst ? inst->surface.GetTotalTileCount() : 0;
}

NKV_API uint32_t nkv_get_surface_visible_tile_count(NkvHandle handle) {
    Instance* inst = Resolve(handle);
    return inst ? inst->surface.GetVisibleTileCount() : 0;
}

NKV_API uint64_t nkv_get_surface_total_triangle_count(NkvHandle handle) {
    Instance* inst = Resolve(handle);
    return inst ? inst->surface.GetTotalTriangleCount() : 0;
}

NKV_API uint64_t nkv_get_surface_visible_triangle_count(NkvHandle handle) {
    Instance* inst = Resolve(handle);
    return inst ? inst->surface.GetVisibleTriangleCount() : 0;
}

NKV_API uint32_t nkv_get_surface_lod(NkvHandle handle, int* out_is_moving) {
    Instance* inst = Resolve(handle);
    if (out_is_moving) {
        *out_is_moving = inst ? (inst->surface.IsInteractionMoving() ? 1 : 0) : 0;
    }
    return inst ? inst->surface.GetSelectedLod() : 0;
}

NKV_API int nkv_get_surface_lod_triangle_counts(NkvHandle handle, uint64_t* out_counts) {
    Instance* inst = Resolve(handle);
    if (!inst || !out_counts) return 0;
    for (uint32_t i = 0; i < 3; ++i) out_counts[i] = inst->surface.GetLodTriangleCount(i);
    return 1;
}

NKV_API int nkv_set_surface_interacting(NkvHandle handle, int moving, double idle_refine_ms) {
    Instance* inst = Resolve(handle);
    if (!inst) return 0;
    if (idle_refine_ms > 0.0) inst->surface.SetIdleRefineMs(idle_refine_ms);
    inst->surface.SetInteractionState(moving != 0, NowMs());
    return 1;
}

NKV_API int nkv_set_surface_lod_error_pixels(NkvHandle handle, float pixels) {
    Instance* inst = Resolve(handle);
    if (!inst) return 0;
    if (pixels > 0.0f) inst->surface.SetLodErrorPixelLimit(pixels);
    return 1;
}

NKV_API int nkv_get_surface_state(NkvHandle handle) {
    Instance* inst = Resolve(handle);
    return inst ? static_cast<int>(inst->surface.GetState()) : 0;
}

NKV_API uint64_t nkv_get_surface_swap_count(NkvHandle handle) {
    Instance* inst = Resolve(handle);
    return inst ? inst->surface.GetSwapCount() : 0;
}

NKV_API uint64_t nkv_get_surface_stale_discard_count(NkvHandle handle) {
    Instance* inst = Resolve(handle);
    return inst ? inst->surface.GetStaleDiscardCount() : 0;
}

NKV_API int nkv_get_surface_memory(NkvHandle handle,
                                   uint64_t* out_budget, uint64_t* out_active,
                                   uint64_t* out_pending, uint64_t* out_lod_cache,
                                   uint64_t* out_peak_during_swap) {
    Instance* inst = Resolve(handle);
    if (!inst) return 0;
    // The budget is read from the renderer, not recomputed from the device, so
    // an override (nkv_set_surface_budget_bytes) is reported honestly instead
    // of the report silently disagreeing with what the guard is enforcing.
    if (out_budget) {
        *out_budget = static_cast<uint64_t>(inst->surface.GetVramBudgetBytes());
    }
    if (out_active) *out_active = static_cast<uint64_t>(inst->surface.GetActiveBytes());
    if (out_pending) *out_pending = static_cast<uint64_t>(inst->surface.GetPendingBytes());
    if (out_lod_cache) *out_lod_cache = static_cast<uint64_t>(inst->surface.GetLodCacheBytes());
    if (out_peak_during_swap) *out_peak_during_swap = inst->surface.GetPeakBytesDuringSwap();
    return 1;
}

NKV_API int nkv_get_surface_memory_rejected(NkvHandle handle) {
    Instance* inst = Resolve(handle);
    return inst ? (inst->surface.WasMemoryRejected() ? 1 : 0) : 0;
}

NKV_API int nkv_estimate_surface_bytes(NkvHandle handle, uint64_t triangle_count,
                                       uint64_t* out_required_bytes,
                                       uint64_t* out_budget,
                                       int* out_would_accept) {
    Instance* inst = Resolve(handle);
    if (!inst) return 0;
    if (out_required_bytes) {
        *out_required_bytes = inst->surface.EstimateRequiredBytes(
            static_cast<std::size_t>(triangle_count));
    }
    if (out_budget) {
        *out_budget = static_cast<uint64_t>(inst->surface.GetVramBudgetBytes());
    }
    if (out_would_accept) {
        *out_would_accept = inst->surface.WouldAcceptUpload(
            static_cast<std::size_t>(triangle_count)) ? 1 : 0;
    }
    return 1;
}

NKV_API int nkv_set_surface_budget_bytes(NkvHandle handle, uint64_t bytes) {
    Instance* inst = Resolve(handle);
    if (!inst) return 0;
    if (bytes > 0) {
        inst->surface.SetVramBudgetBytes(static_cast<double>(bytes));
    } else {
        const uint64_t vram = inst->renderer.GetContext().GetReport().vramBytes;
        inst->surface.SetVramBudgetBytes(static_cast<double>(vram) * 0.70);
    }
    return 1;
}

NKV_API int nkv_set_shading_parameters(NkvHandle handle, int /*formula*/,
                                        float /*azimuth_deg*/, float /*elevation_deg*/,
                                        float /*ambient*/) {
    Instance* inst = Resolve(handle);
    if (!inst) return 0;
    // NOT YET IMPLEMENTED: GLSL ports of formula (A) (class-LUT, ambient
    // floor, compass-flipped azimuth) and (B) (elevation-ramp, ambient
    // lerp) are not present in shaders/surface.frag in this slice. Colors
    // arriving via nkv_set_surface are pre-baked on the CPU and displayed
    // via NKV_SHADE_PASSTHROUGH; changing azimuth/elevation/ambient today
    // requires a CPU recompute + nkv_set_surface re-upload, which violates
    // the "uniform update only" requirement from the task spec. Flagged
    // here rather than silently claimed as done.
    return 1;
}

NKV_API int nkv_set_shaded_class_surface(NkvHandle handle,
                                          const double* positions, uint64_t vertex_count,
                                          const int32_t* faces, uint64_t face_count,
                                          const uint8_t* vertex_class_id,
                                          const uint8_t* class_color_lut,
                                          const int32_t* mixed_face_ids, uint64_t mixed_face_count) {
    std::lock_guard<std::mutex> lock(gMutex);
    Instance* inst = Resolve(handle);
    if (!inst || !positions || !faces || !vertex_class_id || !class_color_lut ||
        vertex_count == 0 || face_count == 0) {
        SetError("invalid handle or null/empty shaded-class surface arrays");
        return 0;
    }
    try {
        // Floating origin: derive it from THIS mesh's bounds before any float32
        // conversion, so UTM-scale coordinates do not quantise the triangles
        // away (see AutoDeriveRenderOrigin). Must run BEFORE the origin
        // reference below is taken.
        AutoDeriveRenderOrigin(inst->renderer, positions, vertex_count);
        const RenderOrigin& origin = inst->renderer.GetOrigin();
        const std::size_t triCount = static_cast<std::size_t>(face_count);

        // Same caller-owned-index guard as nkv_set_surface: these indices are
        // read to index both `positions` and `vertex_class_id`, so an
        // out-of-range or negative value would read outside both buffers.
        const int64_t vertexSpan = static_cast<int64_t>(vertex_count);
        for (std::size_t i = 0; i < triCount * 3; ++i) {
            const int64_t vi = faces[i];
            if (vi < 0 || vi >= vertexSpan) {
                char buf[192];
                std::snprintf(buf, sizeof(buf),
                              "face index %lld out of range [0, %lld) at element %zu "
                              "(vertex_count=%llu, face_count=%llu)",
                              static_cast<long long>(vi),
                              static_cast<long long>(vertexSpan), i,
                              static_cast<unsigned long long>(vertex_count),
                              static_cast<unsigned long long>(face_count));
                SetError(buf);
                return 0;
            }
        }
        if (mixed_face_ids != nullptr) {
            const int64_t faceSpan = static_cast<int64_t>(face_count);
            for (std::size_t i = 0; i < static_cast<std::size_t>(mixed_face_count); ++i) {
                const int64_t fi = mixed_face_ids[i];
                if (fi < 0 || fi >= faceSpan) {
                    char buf[192];
                    std::snprintf(buf, sizeof(buf),
                                  "mixed face id %lld out of range [0, %lld) at element %zu",
                                  static_cast<long long>(fi),
                                  static_cast<long long>(faceSpan), i);
                    SetError(buf);
                    return 0;
                }
            }
        }

        auto renderPos = [&](int32_t vi, float out3[3]) {
            double out[3];
            origin.WorldToRender(positions[vi * 3 + 0], positions[vi * 3 + 1],
                                  positions[vi * 3 + 2], out);
            out3[0] = static_cast<float>(out[0]);
            out3[1] = static_cast<float>(out[1]);
            out3[2] = static_cast<float>(out[2]);
        };
        auto faceNormal = [](const float p0[3], const float p1[3], const float p2[3], float n[3]) {
            float ux = p1[0] - p0[0], uy = p1[1] - p0[1], uz = p1[2] - p0[2];
            float vx = p2[0] - p0[0], vy = p2[1] - p0[1], vz = p2[2] - p0[2];
            float cx = uy * vz - uz * vy;
            float cy = uz * vx - ux * vz;
            float cz = ux * vy - uy * vx;
            float len = std::sqrt(cx * cx + cy * cy + cz * cz);
            if (len < 1e-12f) { n[0] = 0.0f; n[1] = 0.0f; n[2] = 1.0f; return; }
            n[0] = cx / len; n[1] = cy / len; n[2] = cz / len;
        };
        auto classColor = [&](uint8_t cid, uint8_t rgb[3]) {
            rgb[0] = class_color_lut[cid * 3 + 0];
            rgb[1] = class_color_lut[cid * 3 + 1];
            rgb[2] = class_color_lut[cid * 3 + 2];
        };

        // ---- PASS 1: base, ALL faces, one flat class color per face -----
        std::vector<float> basePos(triCount * 9);
        std::vector<float> baseNrm(triCount * 9);
        std::vector<uint8_t> baseCol(triCount * 12); // RGBA8 (unused alpha=255) per vertex, matches surfAttributes format
        std::vector<uint32_t> baseIdx(triCount * 3);
        for (std::size_t f = 0; f < triCount; ++f) {
            int32_t vidx[3] = {faces[f * 3 + 0], faces[f * 3 + 1], faces[f * 3 + 2]};
            float p[3][3];
            for (int k = 0; k < 3; ++k) renderPos(vidx[k], p[k]);
            float n[3];
            faceNormal(p[0], p[1], p[2], n);
            uint8_t rgb[3];
            classColor(vertex_class_id[vidx[0]], rgb);
            for (int k = 0; k < 3; ++k) {
                std::size_t dst = f * 3 + k;
                basePos[dst * 3 + 0] = p[k][0]; basePos[dst * 3 + 1] = p[k][1]; basePos[dst * 3 + 2] = p[k][2];
                baseNrm[dst * 3 + 0] = n[0]; baseNrm[dst * 3 + 1] = n[1]; baseNrm[dst * 3 + 2] = n[2];
                baseCol[dst * 4 + 0] = rgb[0]; baseCol[dst * 4 + 1] = rgb[1];
                baseCol[dst * 4 + 2] = rgb[2]; baseCol[dst * 4 + 3] = 255;
                baseIdx[dst] = static_cast<uint32_t>(dst);
            }
        }

        // ---- PASS 2: overlay, only mixed faces, per-vertex true colors ---
        const std::size_t mixCount = static_cast<std::size_t>(mixed_face_count);
        std::vector<float> ovPos(mixCount * 9), ovNrm(mixCount * 9);
        std::vector<uint8_t> ovCol(mixCount * 12);
        std::vector<uint32_t> ovIdx(mixCount * 3);
        for (std::size_t m = 0; m < mixCount; ++m) {
            int32_t f = mixed_face_ids ? mixed_face_ids[m] : -1;
            if (f < 0 || static_cast<uint64_t>(f) >= face_count) continue;
            int32_t vidx[3] = {faces[f * 3 + 0], faces[f * 3 + 1], faces[f * 3 + 2]};
            float p[3][3];
            for (int k = 0; k < 3; ++k) renderPos(vidx[k], p[k]);
            float n[3];
            faceNormal(p[0], p[1], p[2], n);
            for (int k = 0; k < 3; ++k) {
                std::size_t dst = m * 3 + k;
                ovPos[dst * 3 + 0] = p[k][0]; ovPos[dst * 3 + 1] = p[k][1]; ovPos[dst * 3 + 2] = p[k][2];
                ovNrm[dst * 3 + 0] = n[0]; ovNrm[dst * 3 + 1] = n[1]; ovNrm[dst * 3 + 2] = n[2];
                uint8_t rgb[3];
                classColor(vertex_class_id[vidx[k]], rgb); // per-vertex TRUE color, not vertex-0
                ovCol[dst * 4 + 0] = rgb[0]; ovCol[dst * 4 + 1] = rgb[1];
                ovCol[dst * 4 + 2] = rgb[2]; ovCol[dst * 4 + 3] = 255;
                ovIdx[dst] = static_cast<uint32_t>(dst);
            }
        }

        inst->surface.UploadShadedClassMesh(
            basePos.data(), baseNrm.data(), baseCol.data(), triCount * 3, baseIdx.data(), baseIdx.size(),
            mixCount > 0 ? ovPos.data() : nullptr, mixCount > 0 ? ovNrm.data() : nullptr,
            mixCount > 0 ? ovCol.data() : nullptr, mixCount * 3,
            mixCount > 0 ? ovIdx.data() : nullptr, ovIdx.size());
    } catch (const std::exception& e) {
        SetError(e.what());
        return 0;
    }
    return 1;
}

NKV_API int nkv_set_crisp_shading_parameters(NkvHandle handle,
                                              float azimuth_deg, float sharpness_raw,
                                              float ambient,
                                              float key_intensity, float fill_intensity) {
    Instance* inst = Resolve(handle);
    if (!inst) return 0;
    inst->surface.SetCrispShadingParams(azimuth_deg, sharpness_raw, ambient, key_intensity, fill_intensity);
    return 1;
}

/* ---- GPU class-colour LUT (Phase C) -------------------------------------
 *
 * The whole point of this entry point is that it is the ONLY thing a palette
 * change needs. It touches one 1 KB buffer and nothing else: no positions, no
 * normals, no indices, no per-vertex class ids, and no mesh-upload counter.
 * That is what makes "recolour without rebuilding" a measured property rather
 * than an aspiration - compare nkv_get_surface_upload_count() across the call.
 */
NKV_API int nkv_set_class_color_lut(NkvHandle handle, const uint8_t* lut_rgb) {
    Instance* inst = Resolve(handle);
    if (!inst) return 0;
    if (!lut_rgb) {
        SetError("nkv_set_class_color_lut: null lut");
        return 0;
    }
    inst->surface.SetClassColorLut(lut_rgb);
    return 1;
}

NKV_API uint64_t nkv_get_class_lut_update_count(NkvHandle handle) {
    Instance* inst = Resolve(handle);
    if (!inst) return 0;
    return inst->surface.GetClassLutUpdateCount();
}

NKV_API int nkv_set_color_parity_params(NkvHandle handle, int color_mode, int debug_stage,
                                        float ambient_floor, float base_light_elevation_deg) {
    Instance* inst = Resolve(handle);
    if (!inst) return 0;
    // One switch for the whole frame: the point cloud and the surface mesh share
    // the same sRGB swapchain, so they must agree or a mode switch changes
    // brightness. See the header for the full contract.
    inst->pointCloud.SetColorParityMode(color_mode);
    inst->surface.SetShadingParityParams(color_mode, debug_stage,
                                         ambient_floor, base_light_elevation_deg);
    return 1;
}

NKV_API uint64_t nkv_get_parity_param_update_count(NkvHandle handle) {
    Instance* inst = Resolve(handle);
    if (!inst) return 0;
    return inst->surface.GetParityParamUpdateCount();
}

NKV_API uint64_t nkv_get_surface_upload_count(NkvHandle handle) {
    Instance* inst = Resolve(handle);
    if (!inst) return 0;
    return inst->surface.GetMeshUploadCount();
}

NKV_API uint64_t nkv_get_shade_param_update_count(NkvHandle handle) {
    Instance* inst = Resolve(handle);
    if (!inst) return 0;
    return inst->surface.GetShadeParamUpdateCount();
}

NKV_API uint64_t nkv_get_overlay_triangle_count(NkvHandle handle) {
    Instance* inst = Resolve(handle);
    if (!inst) return 0;
    return inst->surface.GetOverlayTriangleCount();
}

NKV_API int nkv_set_camera_lookat(NkvHandle handle,
                                   double eye_x, double eye_y, double eye_z,
                                   double target_x, double target_y, double target_z,
                                   double fov_y_degrees, double near_clip, double far_clip) {
    Instance* inst = Resolve(handle);
    if (!inst) return 0;
    double eye[3] = {eye_x, eye_y, eye_z};
    double target[3] = {target_x, target_y, target_z};
    Camera& cam = inst->renderer.GetCamera();
    cam.SetLookAtWorld(eye, target);
    VkExtent2D ext = inst->renderer.GetExtent();
    double aspect = (ext.height > 0) ? (double)ext.width / (double)ext.height : 1.0;
    cam.SetPerspective(fov_y_degrees, aspect, near_clip, far_clip);
    // A look-at camera IS a perspective camera: leave the orthographic
    // (VTK-parallel) box that nkv_set_camera_ortho() installed. Without this
    // the engine kept projecting through the ortho half-height left over from
    // the 2D plan view, so a 3D pan translated the cloud out of that window
    // and every frame came back black - the exact "3D renders nothing after
    // Shift+P" symptom.
    cam.SetPerspectiveMode();
    return 1;
}

NKV_API int nkv_set_render_origin(NkvHandle handle, double x, double y, double z) {
    Instance* inst = Resolve(handle);
    if (!inst) return 0;
    inst->renderer.SetOrigin(x, y, z);
    return 1;
}

NKV_API int nkv_resize(NkvHandle handle, uint32_t width, uint32_t height) {
    std::lock_guard<std::mutex> lock(gMutex);
    Instance* inst = Resolve(handle);
    if (!inst) return 0;
    try {
        inst->renderer.RecreateSwapchain(width, height);
    } catch (const std::exception& e) {
        SetError(e.what());
        return 0;
    }
    return 1;
}

NKV_API int nkv_render(NkvHandle handle) {
    std::lock_guard<std::mutex> lock(gMutex);
    Instance* inst = Resolve(handle);
    if (!inst) return 0;
    try {
        if (!inst->renderer.BeginFrame()) {
            // Minimized / out-of-date: deliberately NOT reported as success.
            // Callers must be able to tell "presented" from "skipped" - see
            // the return-code contract in the header.
            return 1;
        }
        // Atomic swap at the frame boundary (Part 9). Promote a valid PENDING
        // set to ACTIVE BEFORE anything is recorded, so the very first frame
        // after the swap draws the new geometry and there is no window in which
        // neither set is drawable - i.e. no blank frame. The outgoing set is
        // retired, not destroyed, and its buffers are freed later by the frame
        // fence (RetireCompleted), never by a device-wide idle.
        inst->surface.ActivatePendingIfFresh(nullptr);
        VkCommandBuffer cmd = inst->renderer.CurrentCommandBuffer();
        uint32_t slot = inst->renderer.CurrentFrameSlot();
        // PHASE 1: time ONLY the Record() work. Everything around it is
        // accounted for by BeginFrame/EndFrame, so this is exactly what the
        // draw itself costs the CPU.
        const double recordT0 = NowMsApi();
        const uint64_t pointDrawBefore = inst->pointCloud.GetDrawCallCount();
        const uint64_t indexedDrawBefore = inst->surface.GetIndexedDrawCallCount();
        if (inst->streamingSurface != 1 && inst->renderer.IsInstantShadedAvailable() && inst->pointCloud.IsLoaded()) {
            // Instant Shaded Class: splat pass into the private colour+depth
            // target, then a fullscreen lighting pass into the swapchain. The
            // surface is deliberately NOT recorded here - this mode draws no
            // mesh at all, which is the entire point of it.
            inst->renderer.RecordInstantShadedPass(cmd, slot, inst->pointCloud);
        } else {
            if (inst->pointCloud.IsLoaded() && inst->streamingSurface != 1) {
                inst->pointCloud.Record(cmd, slot);
            }
            if (inst->surface.IsLoaded() && inst->streamingSurface != 0) {
                inst->surface.Record(cmd, slot);
            }
        }
        inst->lastPointDraws = inst->pointCloud.GetDrawCallCount() - pointDrawBefore;
        inst->lastIndexedDraws = inst->surface.GetIndexedDrawCallCount() - indexedDrawBefore;
        inst->renderer.SetCommandRecordMs(NowMsApi() - recordT0);
        inst->renderer.EndFrame();
    } catch (const std::exception& e) {
        SetError(e.what());
        return 0;
    }
    return 2; // drawn + submitted + presented
}

NKV_API int nkv_set_clear_color(NkvHandle handle, float r, float g, float b, float a) {
    std::lock_guard<std::mutex> lock(gMutex);
    Instance* inst = Resolve(handle);
    if (!inst) return 0;
    inst->renderer.SetClearColor(r, g, b, a);
    return 1;
}

NKV_API int nkv_get_clear_color(NkvHandle handle, float* out_rgba4) {
    Instance* inst = Resolve(handle);
    if (!inst || !out_rgba4) return 0;
    inst->renderer.GetClearColor(out_rgba4);
    return 1;
}

NKV_API int nkv_get_last_mvp(NkvHandle handle, float* out_mvp16) {
    Instance* inst = Resolve(handle);
    if (!inst || !out_mvp16) return 0;
    inst->renderer.GetLastMvp(out_mvp16);
    return 1;
}

NKV_API int nkv_capture_frame(NkvHandle handle, uint8_t* out_rgba, uint64_t capacity,
                              uint32_t* out_w, uint32_t* out_h) {
    std::lock_guard<std::mutex> lock(gMutex);
    Instance* inst = Resolve(handle);
    if (!inst || !out_rgba || !out_w || !out_h) return 0;
    std::vector<uint8_t> pixels;
    uint32_t w = 0, h = 0;
    if (!inst->renderer.CaptureOffscreenRGBA8(inst->streamingSurface == 1 ? nullptr : &inst->pointCloud,
                                              inst->streamingSurface == 0 ? nullptr : &inst->surface,
                                              pixels, w, h)) {
        SetError("CaptureOffscreenRGBA8 failed (no frame resources yet?)");
        return 0;
    }
    *out_w = w;   // reported even when capacity is short, so the caller can
    *out_h = h;   // resize its buffer and retry (Python capture_frame does)
    if (capacity < pixels.size()) {
        SetError("capture buffer too small (need w*h*4 bytes)");
        return 0;
    }
    std::memcpy(out_rgba, pixels.data(), pixels.size());
    return 1;
}

NKV_API int nkv_capture_depth(NkvHandle handle, float* out_depth, uint64_t capacity,
                              uint32_t* out_w, uint32_t* out_h) {
    std::lock_guard<std::mutex> lock(gMutex);
    Instance* inst = Resolve(handle);
    if (!inst || !out_depth || !out_w || !out_h) return 0;
    std::vector<float> depth;
    uint32_t w = 0, h = 0;
    if (!inst->renderer.CaptureOffscreenDepth32F(inst->streamingSurface == 1 ? nullptr : &inst->pointCloud,
                                                 inst->streamingSurface == 0 ? nullptr : &inst->surface,
                                                 depth, w, h)) {
        SetError("CaptureOffscreenDepth32F failed (needs D32_SFLOAT depth?)");
        return 0;
    }
    *out_w = w;
    *out_h = h;
    if (capacity < depth.size()) {
        SetError("depth buffer too small (need w*h*4 bytes)");
        return 0;
    }
    std::memcpy(out_depth, depth.data(), depth.size() * sizeof(float));
    return 1;
}

NKV_API int nkv_get_frame_stats(NkvHandle handle, uint64_t* rendered,
                                uint64_t* skipped, uint64_t* recreates) {
    Instance* inst = Resolve(handle);
    if (!inst) return 0;
    const FrameStatistics& fs = inst->renderer.GetFrameStatistics();
    if (rendered) *rendered = fs.framesRendered;
    if (skipped) *skipped = fs.framesSkipped;
    if (recreates) *recreates = fs.swapchainRecreates;
    return 1;
}

/* ---- Per-stage CPU frame breakdown (PHASE 1) ---------------------------
 *
 * The engine already timed cpuSubmitMs / presentMs internally but exposed
 * nothing, so "reduce fixed frame overhead" could not be measured. This
 * returns the CPU cost of each stage of the LAST completed frame.
 *
 * All values are CPU-side wall time around the real driver call. There is
 * deliberately NO GPU field here: GPU timestamps are optional in Vulkan and
 * unavailable on some devices, so a caller that wants GPU time must use
 * nkv_get_last_frame_stats(), which reports validity explicitly.
 *
 * out[] must have room for NKV_FRAME_STAGE_COUNT doubles. Returns 1 on
 * success, 0 on an invalid handle or a buffer that was not filled.
 */
NKV_API int nkv_get_frame_timings(NkvHandle handle, double* out);

NKV_API uint64_t nkv_get_point_count(NkvHandle handle) {
    Instance* inst = Resolve(handle);
    if (!inst) return 0;
    return static_cast<uint64_t>(inst->pointCloud.GetPointCount());
}

NKV_API int nkv_get_frame_timings(NkvHandle handle, double* out) {
    if (!out) return 0;
    Instance* inst = Resolve(handle);
    if (!inst) return 0;
    const GpuTimings& t = inst->renderer.LastGpuTimings();
    out[0] = t.frameCpuTotalMs;   // whole frame, CPU side
    out[1] = t.fenceWaitMs;       // vkWaitForFences
    out[2] = t.acquireMs;         // vkAcquireNextImageKHR
    out[3] = t.uboUpdateMs;       // camera UBO + aspect
    out[4] = t.beginFrameMs;      // cmd reset/begin + render pass begin
    out[5] = t.commandRecordMs;   // point/surface Record()
    out[6] = t.cpuSubmitMs;       // vkQueueSubmit
    out[7] = t.presentMs;         // vkQueuePresentKHR
    out[8] = t.endFrameMs;        // cmd end + fence reset
    return 1;
}

NKV_API uint64_t nkv_get_surface_vertex_count(NkvHandle handle) {
    Instance* inst = Resolve(handle);
    if (!inst) return 0;
    return static_cast<uint64_t>(inst->surface.GetVertexCount());
}

NKV_API uint64_t nkv_get_surface_index_count(NkvHandle handle) {
    Instance* inst = Resolve(handle);
    if (!inst) return 0;
    return static_cast<uint64_t>(inst->surface.GetIndexCount());
}

NKV_API uint64_t nkv_get_point_color_upload_count(NkvHandle handle) {
    Instance* inst = Resolve(handle);
    if (!inst) return 0;
    return inst->pointCloud.GetColorUploadCount();
}

NKV_API uint64_t nkv_get_point_classification_upload_count(NkvHandle handle) {
    Instance* inst = Resolve(handle);
    if (!inst) return 0;
    return inst->pointCloud.GetClassificationUploadCount();
}

NKV_API int nkv_reserve_point_capacity(NkvHandle handle, uint64_t capacity_points) {
    std::lock_guard<std::mutex> lock(gMutex);
    Instance* inst = Resolve(handle);
    if (!inst || capacity_points == 0) {
        SetError("invalid handle or zero capacity");
        return 0;
    }
    try {
        // The previous arena (if any) is about to be destroyed; the GPU may
        // still be reading it, so this is the one place a wait is legitimate.
        inst->renderer.WaitIdle();
        return inst->pointCloud.ReserveArena(static_cast<std::size_t>(capacity_points));
    } catch (const std::exception& e) {
        SetError(e.what());
        return 0;
    }
}

NKV_API int nkv_upload_point_tile(NkvHandle handle, uint64_t first, uint64_t count,
                                  const double* xyz_world_f64,
                                  const uint8_t* classification,
                                  const float* intensity,
                                  const uint8_t* normals_oct16) {
    std::lock_guard<std::mutex> lock(gMutex);
    Instance* inst = Resolve(handle);
    if (!inst || count == 0 ||
        (!xyz_world_f64 && !classification && !intensity && !normals_oct16)) {
        SetError("invalid handle, zero count, or no stream to upload");
        return 0;
    }
    try {
        // Same floating-origin rule as nkv_set_point_cloud: a caller-pinned origin
        // wins; otherwise it is derived from the FIRST tile and then stays fixed
        // (every later tile must use the origin the earlier ones were built with).
        // xyz_world_f64 == NULL uploads the optional streams ONLY (positions are
        // already resident).
        if (xyz_world_f64)
            AutoDeriveRenderOrigin(inst->renderer, xyz_world_f64,
                                   static_cast<std::size_t>(count));
        UploadStats st = inst->pointCloud.UploadArenaTile(
            static_cast<std::size_t>(first), static_cast<std::size_t>(count),
            xyz_world_f64, inst->renderer.GetOrigin(),
            classification, intensity, normals_oct16);
        if (st.bytes == 0) {
            SetError("arena tile upload failed (no arena, or tile past capacity)");
            return 0;
        }
    } catch (const std::exception& e) {
        SetError(e.what());
        return 0;
    }
    return 1;
}

NKV_API uint64_t nkv_get_point_capacity(NkvHandle handle) {
    std::lock_guard<std::mutex> lock(gMutex);
    Instance* inst = Resolve(handle);
    return inst ? static_cast<uint64_t>(inst->pointCloud.ArenaCapacity()) : 0;
}

NKV_API uint64_t nkv_get_arena_uploaded_tiles(NkvHandle handle) {
    std::lock_guard<std::mutex> lock(gMutex);
    Instance* inst = Resolve(handle);
    return inst ? static_cast<uint64_t>(inst->pointCloud.ArenaUploadedTiles()) : 0;
}

NKV_API void nkv_clear(NkvHandle handle) {
    std::lock_guard<std::mutex> lock(gMutex);
    Instance* inst = Resolve(handle);
    if (!inst) return;
    inst->renderer.WaitIdle();
    inst->surface.Shutdown();
    inst->pointCloud.Shutdown();
    inst->surface.Initialize(inst->renderer);
    inst->pointCloud.Initialize(inst->renderer);
}

NKV_API int nkv_get_frame_timing(NkvHandle handle, double* cpuSubmitMs,
                                  double* gpuRenderMs, double* pointGpuMs,
                                  double* surfaceGpuMs, double* presentMs,
                                  int* valid) {
    Instance* inst = Resolve(handle);
    if (!inst) return 0;
    const GpuTimings& t = inst->renderer.LastGpuTimings();
    if (cpuSubmitMs)  *cpuSubmitMs  = t.cpuSubmitMs;
    if (gpuRenderMs)  *gpuRenderMs  = t.frameGpuMs;
    if (pointGpuMs)   *pointGpuMs   = t.pointGpuMs;
    if (surfaceGpuMs) *surfaceGpuMs = t.surfaceGpuMs;
    if (presentMs)    *presentMs    = t.presentMs;
    if (valid)        *valid        = t.valid ? 1 : 0;
    return 1;
}

// Real GPU frame time in milliseconds, measured from the
// VK_QUERY_TYPE_TIMESTAMP pool and corrected by timestampPeriod.
//
// Returns -1.0 when no GPU timing is available (timestamps disabled, the
// device/driver does not support them, or no full frame has been read back
// yet). It NEVER returns 0.0 as a stand-in for "unknown": 0.0 is a
// legitimate measurement for a genuinely empty frame, and conflating the two
// is exactly what made nkv_last_frame_ms print "nan" downstream.
NKV_API int nkv_set_point_draw_ranges(NkvHandle handle,
                                      const uint32_t* firsts,
                                      const uint32_t* counts,
                                      uint32_t rangeCount) {
    Instance* inst = Resolve(handle);
    if (!inst) return 0;
    inst->pointCloud.SetDrawRanges(firsts, counts, rangeCount);
    return 1;
}

NKV_API int nkv_clear_point_draw_ranges(NkvHandle handle) {
    Instance* inst = Resolve(handle);
    if (!inst) return 0;
    inst->pointCloud.ClearDrawRanges();
    return 1;
}

NKV_API uint32_t nkv_get_point_draw_range_count(NkvHandle handle) {
    Instance* inst = Resolve(handle);
    if (!inst) return 0;
    return inst->pointCloud.DrawRangeCount();
}

NKV_API uint32_t nkv_get_point_draw_range_points(NkvHandle handle) {
    Instance* inst = Resolve(handle);
    if (!inst) return 0;
    return inst->pointCloud.DrawRangePointTotal();
}

NKV_API double nkv_get_gpu_frame_time_ms(NkvHandle handle) {
    Instance* inst = Resolve(handle);
    if (!inst) return -1.0;
    const GpuTimings& t = inst->renderer.LastGpuTimings();
    if (!t.valid) return -1.0;
    return t.frameGpuMs;
}

NKV_API double nkv_last_frame_ms(NkvHandle handle) {
    Instance* inst = Resolve(handle);
    if (!inst) return -1.0;
    // Historically this returned "time since BeginFrame", which reads back as
    // 0.0 at the point callers query it and printed "FPS: nan". It is a CPU
    // number and is superseded by nkv_get_frame_timing / the timestamp pool.
    // Preserve the symbol but make it honest: -1 means "not available".
    return -1.0;
}

NKV_API const char* nkv_last_error(void) {
    return gLastError.c_str();
}

NKV_API const char* nkv_get_device_name(NkvHandle handle) {
    Instance* inst = Resolve(handle);
    if (!inst) return "";
    return inst->deviceNameCache.c_str();
}

/* Authoritative VRAM figures for the device this handle is already running on
 * (see the header for why the planner must not guess its own).
 *
 * There was already an nkv_get_vram_bytes() returning the largest DEVICE_LOCAL
 * heap; what was missing was the SAFE BUDGET. Two components were each applying
 * their own idea of "how much may I spend", so a caller could ask for the card
 * size and then be handed a different, larger allowance elsewhere. Now the
 * margin is defined once, here, and every budget - point cloud and surface -
 * derives from it. */
NKV_API uint64_t nkv_get_device_vram_budget(NkvHandle handle) {
    Instance* inst = Resolve(handle);
    if (!inst) return 0;
    const uint64_t total = inst->renderer.GetContext().GetReport().vramBytes;
    // Never hand out the whole card: the swapchain, the allocator's own
    // bookkeeping and the driver all need headroom, and streaming a 27M-point
    // cloud straight to 100% is how an OOM crash looks.
    return (uint64_t)((double)total * kVramBudgetFraction);
}

NKV_API uint32_t nkv_get_api_version(NkvHandle handle) {
    Instance* inst = Resolve(handle);
    if (!inst) return 0;
    return inst->renderer.GetContext().GetReport().apiVersion;
}

NKV_API uint64_t nkv_get_vram_bytes(NkvHandle handle) {
    Instance* inst = Resolve(handle);
    if (!inst) return 0;
    return inst->renderer.GetContext().GetReport().vramBytes;
}
