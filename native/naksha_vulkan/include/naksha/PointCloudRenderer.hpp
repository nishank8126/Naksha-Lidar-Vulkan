#pragma once
#include <cstddef>
#include <cstdint>
#include <utility>
#include <vector>

#include "naksha/Buffer.hpp"
#include "naksha/RenderOrigin.hpp"
#include "naksha/core/vulkan/VulkanPipelineManager.h"

namespace naksha {
using namespace vulkan;
class Renderer;

// ---------------------------------------------------------------------------
// Point-cloud renderer.
//
// All point data arrives through the bulk Upload*() methods below. There
// is deliberately NO per-point API anywhere in this engine (spec rule).
// Once uploaded the buffers are resident on the GPU; shading changes
// (point size, color mapping) never touch them - they are push-constant
// only.
// ---------------------------------------------------------------------------
class PointCloudRenderer {
public:
    PointCloudRenderer() = default;
    ~PointCloudRenderer();

    bool Initialize(Renderer& renderer);
    void Shutdown();
    bool IsInitialized() const { return initialized_; }

    // ---- Bulk uploads (one call per attribute, C-contiguous) ----
    // float64 world XYZ (count*3 floats) -> converted to float32
    // render-space (world - origin) in a single linear pass, then
    // staged and copied once. Counts as a position upload.
    UploadStats UploadPositionsWorldF64(const double* xyz, std::size_t count,
                                        const RenderOrigin& origin);
    // float32 render-space XYZ (count*3 floats) - no conversion;
    // staging + copy only.
    UploadStats UploadPositionsRenderF32(const float* xyz, std::size_t count);
    // RGB uint8 (count*3) -> RGBA8 (alpha = 255). This is the colour *data*
    // for the RGB display mode (true-colour LiDAR); it is not a shading bake -
    // Classification/Intensity/Elevation colours are computed in point.vert
    // from the attributes below plus the LUTs in the frame UBO.
    UploadStats UploadColorsRGB8(const uint8_t* rgb, std::size_t count);
    // Classification uint8 (count) - bound as a vertex attribute (R8_UNORM;
    // the shader recovers the integer id with round(v*255)) and used for the
    // palette lookup in classification mode.
    UploadStats UploadClassificationU8(const uint8_t* cls, std::size_t count);
    // Intensity float32 (count) - bound as R32_SFLOAT; drives the intensity
    // ramp and modulates class colours.
    UploadStats UploadIntensityF32(const float* intensity, std::size_t count);

    // ---- Stored oct16x2 normals (Instant Shaded) -----------------------
    // Packed octahedral signed-normalized int16 x2 = 4 bytes per point,
    // uploaded verbatim and decoded in the shader. `packed` is the caller's
    // byte buffer and `count` is the POINT count (so count*4 bytes are read),
    // which keeps the call sites free of unit conversion.
    //
    // The point order MUST match the position stream exactly.
    static constexpr std::size_t kOct16BytesPerNormal = 4;
    UploadStats UploadNormalsOct16(const uint8_t* packed, std::size_t count);
    // Points currently covered by the normal buffer, its byte size, and how
    // many times it has been (re)uploaded. The counter is what proves a warm
    // style switch uploaded 0 normal bytes.
    std::size_t GetNormalCount() const { return normalCount_; }
    VkDeviceSize GetNormalBytes() const { return normalBytes_; }
    uint64_t GetNormalUploadCount() const { return normalUploads_; }

  private:
    // Grows the zero-filled oct16 stand-in to cover `count` points. Called on
    // every position upload: the splat pass binds it whenever no real normal
    // stream is resident, and a short buffer would be read out of bounds.
    void ensureFallbackNormals(std::size_t count);

  public:

    // ---- Shading / point sizing: push constants only, never a re-upload ----
    // mode is an NkvDisplayMode (0 RGB, 1 classification, 2 intensity,
    // 3 elevation) and lands in pc.shading.x.
    void SetDisplayMode(int mode);
    int GetDisplayMode() const { return displayMode_; }
    // Elevation ramp endpoints (world Z, same units as the dataset) plus the
    // gamma applied before the LUT lookup.
    void SetElevationRange(float lo, float hi, float gamma = 1.0f);
    // Intensity endpoints plus contrast (pivot around mid) and gamma.
    void SetIntensityRange(float lo, float hi, float contrast = 1.0f, float gamma = 1.0f);
    // Screen footprint model. footprintMeters is the physical laser footprint:
    // the shader converts it to pixels with the frame's projection scale and
    // divides by view depth, then clamps to [minPx, maxPx] (the VTK-parity
    // band, 2-5 px at typical zoom). classIntensityMix blends return strength
    // into classification colours (0 = flat class palette).
    void SetPointSizeParams(float footprintMeters, float minPx, float maxPx,
                            float classIntensityMix);
    // Legacy fixed-pixel knob kept for the existing C ABI: pins the clamp band
    // to a single size, reproducing the old constant gl_PointSize behaviour.
    void SetPointSize(float pixels);
    // DEPTH mode normalisation (view-space distance -> grayscale ramp). Travels in
    // the free parity.y/z/w push-constant slots; see shaders/point.vert.
    void SetDepthRange(float lo, float hi, float gamma);
    float GetPointSize() const;
    // Sprite rim falloff (0 = hard-edged disc, 1 = maximally soft) and overall
    // brightness multiplier.
    void SetSpriteParams(float softness, float brightness);
    // Colour-space byte parity with the VTK viewport (see shaders/point.vert):
    // 1 (default) = the shader pre-compensates the swapchain's sRGB encode so
    // the framebuffer holds exactly the palette/ramp/data byte VTK paints;
    // 0 = the previous raw linear write. Push-constant only.
    void SetColorParityMode(int mode);
    int GetColorParityMode() const { return colorParityMode_; }
    // Draw toggle. Shaded Classification and Surface modes present a TIN mesh
    // instead of the raw cloud, and the app HIDES the point actors for exactly
    // that reason (gui/shading_display._hide_point_cloud_actors_for_shading /
    // surface_mode._hide_point_cloud_actors_for_surface). Without this the raw
    // points stay in front of the mesh and hide it. Hiding keeps every buffer
    // resident: nothing is destroyed, so re-showing is a single push-free flag
    // write and never a re-upload.
    void SetVisible(bool visible) { visible_ = visible; }
    bool IsVisible() const { return visible_; }

    // Record draw commands into cmd (must be the current frame's
    // command buffer). Binds set 0 (frame UBO) and writes the point-
    // draw timestamp pair.
    // ---- Screen-space LOD (opt-in, NAKSHA_VULKAN_LOD=1) -------------------
    // When ranges are set, Record issues one vkCmdDraw per contiguous visible
    // range instead of a single full-buffer draw. The point buffers are NOT
    // re-uploaded: the caller uploads the FULL cloud once in cell order, so a
    // range is just a window into the same persistent buffers.
    // Clearing the ranges restores the exact single-draw behaviour.
    void SetDrawRanges(const uint32_t* firsts, const uint32_t* counts,
                       uint32_t rangeCount) {
        drawRanges_.clear();
        if (!firsts || !counts || rangeCount == 0) return;
        drawRanges_.reserve(rangeCount);
        for (uint32_t i = 0; i < rangeCount; ++i)
            drawRanges_.emplace_back(firsts[i], counts[i]);
    }
    void ClearDrawRanges() { drawRanges_.clear(); }

    // ---- Stable-offset ARENA residency ------------------------------------
    // The whole-cloud upload (UploadPositionsWorldF64) re-packs EVERY point at
    // offset 0 and invalidates every attribute stream, so one new tile used to
    // cost a full re-upload and moved every existing tile's offset. The arena
    // instead reserves a fixed point capacity ONCE; each tile is written at a
    // caller-chosen, STABLE offset, and a draw range is just a window into it.
    // Draw-set changes (LOD, retirement) therefore never touch a buffer.
    //
    // ReserveArena: 0 = failed, 1 = ready (nothing to re-upload),
    //               2 = (re)allocated - every earlier tile is INVALID and must be
    //                   uploaded again.
    int ReserveArena(std::size_t capacityPoints);
    // Writes positions (world f64 -> render f32) and any provided attribute
    // streams for [first, first+count) in ONE staged submit. All streams use the
    // SAME point offset, so XYZ i / class i / intensity i / normal i stay one
    // canonical point. Returns bytes==0 on failure.
    UploadStats UploadArenaTile(std::size_t first, std::size_t count,
                                const double* xyzWorldF64, const RenderOrigin& origin,
                                const uint8_t* classification, const float* intensity,
                                const uint8_t* normalsOct16);
    bool ArenaMode() const { return arenaMode_; }
    std::size_t ArenaCapacity() const { return arenaCapacity_; }
    std::size_t ArenaUploadedTiles() const { return arenaTilesUploaded_; }
    bool HasDrawRanges() const { return !drawRanges_.empty(); }
    uint32_t DrawRangePointTotal() const {
        uint64_t t = 0;
        for (const auto& r : drawRanges_) t += r.second;
        return static_cast<uint32_t>(t);
    }
    uint32_t DrawRangeCount() const {
        return static_cast<uint32_t>(drawRanges_.size());
    }

    void Record(VkCommandBuffer cmd, uint32_t frameSlot);

    // Instant Shaded SPLAT pass (display mode 4). Same persistent buffers as
    // Record(), but a 2-attachment pipeline that ALSO writes each splat's
    // STORED oct16x2 normal to colour attachment 1, which the lighting pass
    // reads per pixel. Touches no buffer: it only re-binds what is already
    // resident and pushes the same PointParams.
    void RecordSplat(VkCommandBuffer cmd, uint32_t frameSlot);
    std::size_t GetPointCount() const;
    bool IsLoaded() const;
    uint64_t GetPositionUploadCount() const;
    uint64_t GetColorUploadCount() const;
    uint64_t GetClassificationUploadCount() const;
    uint64_t GetIntensityUploadCount() const;
    // PHASE 1: points COVERED by each attribute stream. 0 means the stream was
    // never uploaded. This is what separates "the dataset has no intensity"
    // from "intensity is present but every value happens to be zero" - the
    // difference between UNSUPPORTED and a legitimate black cloud, and the
    // reason [INTENSITY TRACE] can never report a blank screen as READY.
    std::size_t GetClassificationCount() const { return classificationCount_; }
    std::size_t GetIntensityCount() const { return intensityCount_; }
    // Draw calls actually recorded, and the device's point-size ceiling
    // (limits.pointSizeRange[1]) as observed in the last Record().
    uint64_t GetDrawCallCount() const { return drawCalls_; }
    float GetMaxPointSizeDevice() const { return maxPointSizeDevice_; }
    VkDeviceSize GetGpuBytes() const;
    void OnSwapchainRebuild();

private:
    Renderer* renderer_ = nullptr;
    PipelineConfig pipelineCfg_;
    GPUBuffer positions_;
    GPUBuffer colors_;
    GPUBuffer classification_;
    GPUBuffer intensity_;
    // Packed oct16x2 normal stream (4 bytes/point), bound as vertex location 4.
    // Separate from positions_: no duplication, and the two streams must use an
    // IDENTICAL point order.
    GPUBuffer normals_;
    // Zero-filled stand-ins bound in place of classification/intensity when a
    // dataset has no such channel: Vulkan requires every vertex binding the
    // pipeline references to be bound, and uninitialised memory would read as
    // random class colours / intensities on screen.
    GPUBuffer fallbackClass_;
    // Zero-filled oct16x2 stand-in, bound when no normal stream is resident.
    GPUBuffer fallbackNormals_;
    GPUBuffer fallbackIntensity_;

    VkPipeline pipeline_ = VK_NULL_HANDLE;
    VkPipelineLayout pipelineLayout_ = VK_NULL_HANDLE;
    ShaderModule vs_ = {};
    ShaderModule fs_ = {};
    // Instant Shaded splat pipeline (mode 4). Shares pipelineLayout_ and every
    // buffer with pipeline_; it differs only in its vertex layout (5 streams,
    // adding the oct16 normal) and its two colour outputs.
    VkPipeline splatPipeline_ = VK_NULL_HANDLE;
    ShaderModule splatVs_ = {};
    ShaderModule splatFs_ = {};

    std::size_t pointCount_ = 0;
    // Arena residency (see ReserveArena). In arena mode `pointCount_` is the
    // HIGH-WATER extent, an attribute stream is bound whenever any tile has
    // written it (the caller only draws tiles that carry it), and an EMPTY draw
    // list draws NOTHING (a full-buffer draw would rasterise unwritten gaps).
    bool arenaMode_ = false;
    std::size_t arenaCapacity_ = 0;
    std::size_t arenaTilesUploaded_ = 0;
    bool classInArena_ = false;
    bool intensityInArena_ = false;
    // Push-constant state (see shaders/point.vert's PointParams layout).
    int displayMode_ = 0;              // NkvDisplayMode
    float footprintMeters_ = 0.05f;    // typical LiDAR laser footprint
    float pointSizeMinPx_ = 1.5f;
    float pointSizeMaxPx_ = 6.0f;
    float classIntensityMix_ = 0.35f;
    float elevationLo_ = 0.0f;
    float elevationHi_ = 1.0f;
    float elevationGamma_ = 1.0f;
    float intensityLo_ = 0.0f;
    float intensityHi_ = 255.0f;
    float intensityContrast_ = 1.0f;
    float intensityGamma_ = 1.0f;
    // VTK parity: 0.0 = hard-edged opaque disc, matching the `opacity = 1.0`
    // in gui/unified_actor_manager.py's //VTK::Color::Impl. The previous 0.35
    // default faded the rim to transparent and alpha-blended it, which is what
    // made points look like soft balls instead of crisp returns.
    float spriteSoftness_ = 0.0f;
    float brightness_ = 1.0f;
    // Colour-space byte parity (see point.vert's toDisplayByte): 1 = the shader
    // pre-compensates the sRGB attachment so the palette/ramp/data byte lands in
    // the framebuffer unchanged, 0 = the previous raw linear write.
    int colorParityMode_ = 1;
    // DEPTH mode (NKV_DISPLAY_DEPTH) normalisation, pushed into the free
    // parity.y/z/w push-constant slots. See Record(). Depth is a VIEW-dependent
    // quantity, so unlike class/intensity/elevation this range is re-derived
    // from the camera; it is still push-constant only and never an upload.
    float depthLo_ = 0.0f;
    float depthHi_ = 1.0f;
    float depthGamma_ = 1.0f;
    float pointSize_ = 2.0f;           // legacy mirror reported by GetPointSize()
    bool visible_ = true;              // draw toggle, see SetVisible()
    uint64_t drawCalls_ = 0;           // vkCmdDraw submissions
    // (firstVertex, vertexCount) windows for the LOD draw path. Empty means
    // "draw the whole buffer in one call" (the default, LOD off).
    std::vector<std::pair<uint32_t, uint32_t>> drawRanges_;
    float maxPointSizeDevice_ = 0.0f;  // limits.pointSizeRange[1]
    uint64_t positionUploads_ = 0;
    uint64_t colorUploads_ = 0;
    uint64_t classificationUploads_ = 0;
    uint64_t intensityUploads_ = 0;
    // PHASE 1: coverage of each attribute stream, in POINTS. Distinct from the
    // upload counters, which say "how many times" rather than "how many points".
    std::size_t classificationCount_ = 0;
    std::size_t intensityCount_ = 0;
    // Stored-normal telemetry: coverage, resident bytes and upload count.
    std::size_t normalCount_ = 0;
    VkDeviceSize normalBytes_ = 0;
    uint64_t normalUploads_ = 0;
    bool initialized_ = false;
};

} // namespace naksha
