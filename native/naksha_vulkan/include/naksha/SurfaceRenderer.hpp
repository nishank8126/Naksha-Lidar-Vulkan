#pragma once
#include <cstddef>
#include <cstdint>
#include <memory>
#include <vector>

#include "naksha/Buffer.hpp"
#include "naksha/SurfaceTiling.hpp"
#include "naksha/core/vulkan/VulkanFrameManager.h"
#include "naksha/core/vulkan/VulkanPipelineManager.h"

namespace naksha {
using namespace vulkan;

enum class ShadingMode : uint32_t {
    Flat = 0,
    Smooth = 1,
    Elevation = 2,
    Wireframe = 3
};

// ---------------------------------------------------------------------------
// Atomic surface swap states (Part 9).
//
// EMPTY          nothing resident
// PREVIEW_ACTIVE a coarse/interactive resource set is on screen
// FINAL_PENDING  a final-quality set is uploaded but not yet swapped in
// FINAL_ACTIVE   the final-quality set is on screen
// ---------------------------------------------------------------------------
enum class SurfaceState : uint32_t {
    Empty = 0,
    PreviewActive = 1,
    FinalPending = 2,
    FinalActive = 3,
};

// GPU buffers for ONE LOD level. Each level is a self-contained mesh: the
// clustered coarse levels have their own compact vertex arrays, so they need
// their own vertex buffers as well as their own index buffer.
struct SurfaceLodBuffers {
    GPUBuffer positions;
    GPUBuffer normals;
    GPUBuffer colors;
    GPUBuffer indices;
    GPUBuffer cellColors;
    VkDescriptorPool cellPool = VK_NULL_HANDLE;
    VkDescriptorSet cellSet = VK_NULL_HANDLE;
    VkDevice cellDevice = VK_NULL_HANDLE;
    bool IsValid() const { return positions.IsValid() && indices.IsValid(); }
    VkDeviceSize Bytes() const {
        return positions.size + normals.size + colors.size + indices.size + cellColors.size;
    }
    void Release();
};

// One complete, independently-owned GPU resource set. ACTIVE and PENDING are
// separate instances of this, so a new final mesh can be uploaded, validated
// and only then swapped in without ever touching the set currently on screen.
struct SurfaceResourceSet {
    // One buffer set per LOD level. Level 0 is the final mesh; 1 and 2 are
    // the clustered coarse levels. Each is fully tiled and independently
    // cullable, which is what lets a pan/zoom change the drawn triangle count
    // with no geometry upload at all.
    SurfaceLodBuffers lods[kSurfaceLodCount];
    std::vector<SurfaceLodMesh> lodMeshes;   // CPU copy of the tiled levels
    // Per level, the tile indices that survived this frame's frustum test.
    // Rebuilt every frame from the live camera - never from an upload.
    std::vector<uint32_t> visibleTiles[kSurfaceLodCount];
    std::size_t vertexCount = 0;
    std::size_t indexCount = 0;
    std::size_t triangleCount = 0;
    uint32_t selectedLod = 0;                // level actually drawn last frame
    bool isPreview = false;                  // coarse stand-in, not final
    bool indexedCells = false;
    // Stale-guard identity. A pending set whose revisions no longer match the
    // renderer's current ones is discarded instead of swapped in.
    uint64_t datasetRevision = 0;
    uint64_t surfaceRevision = 0;
    // Frame in which this set was last submitted. Retirement waits for the
    // frame fence to prove that frame finished - never a device-wide idle.
    uint64_t lastUsedFrame = VulkanFrameManager::kNeverSubmitted;
    // Free/dispatch stats for the memory report.
    VkDeviceSize gpuBytes = 0;
    uint32_t totalTiles = 0;
    uint32_t visibleTilesLastFrame = 0;
    uint64_t submittedTrianglesLastFrame = 0;

    bool IsValid() const { return lods[0].IsValid(); }
    void ReleaseBuffers();
};

class Renderer;

// ---------------------------------------------------------------------------
// Surface/mesh renderer.
//
// Mesh arrives as a single UploadMesh() call (positions, normals, RGB
// colors, indices). Shading mode, light direction, ambient and elevation
// range are set via push constants (or, for wireframe, a pipeline
// switch). None of these change the GPU buffers - meshUploadCount
// increments exactly once per mesh and never again until the next
// UploadMesh().
// ---------------------------------------------------------------------------
class SurfaceRenderer {
public:
    SurfaceRenderer() = default;
    ~SurfaceRenderer();

    bool Initialize(Renderer& renderer);
    void Shutdown();
    bool IsInitialized() const { return initialized_; }

    // Upload a single mesh. position/normal/color are N,3 C-contiguous
    // float32; indices are count uint32. Creates/replaces all buffers
    // in one staged->GPU copy batch.
    UploadStats UploadMesh(const float* positions, const float* normals,
                           const uint8_t* colorsRGB,
                           std::size_t vertexCount,
                           const uint32_t* indices, std::size_t indexCount);

    // ---- State changes (push constants / pipeline switch, no upload) ----
    void SetShadingMode(ShadingMode mode);
    ShadingMode GetShadingMode() const;
    void SetLightDirection(float x, float y, float z);
    void SetAmbient(float ambient);     // 0..1
    void SetElevationRange(float zMin, float zMax);

    // ---- GPU class-colour LUT (Phase C) -----------------------------------
    //
    // A palette change must re-upload ONLY these 256*4 bytes. It must NOT
    // touch positions, normals, indices or the per-vertex class-id stream, and
    // it must NOT increment the mesh-upload counter. The LUT lives in its own
    // GPU buffer and is bound to the surface descriptor set, so the fragment
    // shader resolves colour = LUT[vertex_class_id] at draw time.
    //
    // colorsRGB is 256*3 uint8 (RGB); it is expanded to 256*4 RGBA here so the
    // shader can index it with a byte stride without a second lookup.
    UploadStats SetClassColorLut(const uint8_t* colorsRGB);
    uint64_t GetClassLutUpdateCount() const { return classLutUpdates_; }

    // Record draw commands into cmd (current frame's command buffer).
    // Writes the surface timestamp pair and binds set 0 (frame UBO).
    void Record(VkCommandBuffer cmd, uint32_t frameSlot);

    // ---- Tiled / culled / LOD-aware upload (Parts 6-9) --------------------
    //
    // Uploads a mesh as a TILE + LOD resource set. Unlike UploadMesh() this
    // never overwrites the live GPU buffers: it builds a fresh PENDING set,
    // and ActivatePendingIfFresh() promotes it to ACTIVE at a frame boundary.
    //
    // positions/normals/colors are a flat triangle soup in RENDER space
    // (3 vertices per triangle, exactly what nkv_set_surface already builds).
    // `lodCellMeters` is the base vertex spacing used to derive the coarse
    // levels; pass 0 to disable LOD generation.
    //
    // `isPreview` marks a coarse interactive stand-in. A preview can never
    // silently become the final engineering surface: it is reported through
    // GetState() as PREVIEW_ACTIVE until a real final set is uploaded.
    struct UploadTiledResult {
        bool ok = false;
        UploadStats stats;
        uint32_t tileCount = 0;
        std::size_t lodTriangles[kSurfaceLodCount] = {0, 0, 0};
        bool memoryRejected = false;   // budget refused the upload
        double peakBytesDuringSwap = 0.0;
    };
    UploadTiledResult UploadTiledMesh(const float* positions, const float* normals,
                                      const uint8_t* colors,
                                      std::size_t triangleCount,
                                      uint32_t targetTileCount,
                                      float lodCellMeters,
                                      bool isPreview,
                                      uint64_t datasetRevision,
                                      uint64_t surfaceRevision);
    UploadTiledResult UploadIndexedBlocks(const float* positions, std::size_t vertexCount,
                                          const uint32_t* indices, std::size_t indexCount,
                                          const uint32_t* cellColors,
                                          const uint32_t* blockCounts, uint32_t blockCount,
                                          uint64_t datasetRevision, uint64_t surfaceRevision);

    // Promotes PENDING -> ACTIVE when its revisions still match the current
    // ones. Called once per frame from nkv_render, at the frame boundary.
    // Returns true when a swap happened. A stale pending set is retired
    // instead of promoted, and `discardedPending` is set.
    bool ActivatePendingIfFresh(bool* discardedPending = nullptr);

    // Destroys any resource set whose last submitted frame has provably
    // completed. Uses the frame fences, never vkDeviceWaitIdle().
    void RetireCompleted();

    // Rebuilds the visible-tile list from the CURRENT camera and picks the LOD
    // to draw. Called once per frame from Record(). Pure CPU: it touches no
    // GPU buffer, so panning/zooming never triggers an upload.
    void CullVisibleTiles(double nowMs);

    void SetInteractionState(bool moving, double nowMs);
    // Bytes UploadTiledMesh() would need for a mesh of `triangleCount`
    // triangles, and whether the budget would accept it right now. Exposed so
    // the refusal decision can be reported for meshes too large to materialise
    // on the host (the SLOW-quality case) without allocating anything.
    uint64_t EstimateRequiredBytes(std::size_t triangleCount) const;
    bool WouldAcceptUpload(std::size_t triangleCount) const;
    double GetVramBudgetBytes() const { return vramBudgetBytes_; }
    // Overrides the safe VRAM budget; 0 restores the 70%-of-device default.
    void SetVramBudgetBytes(double bytes) { vramBudgetBytes_ = bytes; }
    bool IsInteractionMoving() const { return interactionMoving_; }
    // Minimum LOD floor while the camera is moving; the requested level is
    // restored automatically once the idle timer expires.
    void SetMovingLodFloor(uint32_t lod) { movingLodFloor_ = lod; }
    void SetIdleRefineMs(double ms) { idleRefineMs_ = ms; }
    void SetLodErrorPixelLimit(float px) { lodErrorPixelLimit_ = px; }

    SurfaceState GetState() const;
    // Stale-guard revisions. Bumping the dataset/surface revision invalidates
    // any PENDING set built against the old one, so it is discarded instead of
    // swapped in.
    void SetDatasetRevision(uint64_t r) { currentDatasetRevision_ = r; }
    void SetSurfaceRevision(uint64_t r) { currentSurfaceRevision_ = r; }
    uint64_t GetDatasetRevision() const { return currentDatasetRevision_; }
    uint64_t GetSurfaceRevision() const { return currentSurfaceRevision_; }
    // Culling/LOD telemetry for the reports.
    uint32_t GetTotalTileCount() const;
    uint32_t GetVisibleTileCount() const;
    uint64_t GetTotalTriangleCount() const;
    uint64_t GetVisibleTriangleCount() const;
    uint32_t GetSelectedLod() const;
    // Resident triangle count of one LOD level (0 = final, 1 = medium,
    // 2 = interaction). This is what proves the coarse levels are real,
    // measured geometry rather than a label.
    uint64_t GetLodTriangleCount(uint32_t lod) const;
    uint64_t GetSwapCount() const { return swapCount_; }
    uint64_t GetStaleDiscardCount() const { return staleDiscards_; }
    VkDeviceSize GetActiveBytes() const;
    VkDeviceSize GetPendingBytes() const;
    VkDeviceSize GetLodCacheBytes() const;
    uint64_t GetPeakBytesDuringSwap() const { return peakBytesDuringSwap_; }
    // True when the last budget check refused an upload for lack of VRAM.
    bool WasMemoryRejected() const { return memoryRejected_; }

    std::size_t GetVertexCount() const;
    // Resident index count for the level actually being DRAWN. Exposed so the
    // GPU-memory breakdown can report a measured index-buffer size instead of
    // an estimate, and so a caller asking "how many triangles is on screen"
    // gets the drawn level rather than a stale legacy count. The per-level
    // breakdown is available through GetLodTriangleCount().
    std::size_t GetIndexCount() const;
    std::size_t GetTriangleCount() const;
    bool IsLoaded() const;
    uint64_t GetMeshUploadCount() const; // increments ONLY on UploadMesh
    uint64_t GetModeSwitchCount() const;
    VkDeviceSize GetGpuBytes() const;
    bool IsWireframeSupported() const;

    // ---- Shaded Class (crisp hybrid) path --------------------------------
    // Real GLSL port of gui/shading_display.py's crisp-hybrid formula
    // (see CHECKPOINTS.md for the Python -> GLSL expression map). Two
    // persistent buffer sets, at most two draw calls per frame:
    //   PASS 1 (base, reuses positions_/normals_/colors_/indices_): ALL
    //     faces, unlit hillshade computed in-shader from the face normal +
    //     azimuth/sharpness/ambient (mirrors _crisp_shade_chunk exactly).
    //   PASS 2 (overlay, positions2_/normals2_/colors2_/indices2_): ONLY
    //     mixed-class faces, true per-vertex class colors (GPU rasterizer
    //     barycentrically interpolates them - no CPU blending), lit by a
    //     key+fill two-light approximation of
    //     _configure_nakshatech_color_blend_lighting.
    // SetCrispShadingParams() is push-constant only - never touches these
    // buffers. UploadShadedClassMesh() is the only thing that increments
    // meshUploads_ / touches GPU buffers for this path.
    UploadStats UploadShadedClassMesh(
        const float* basePositions, const float* baseNormals, const uint8_t* baseColorsRGB,
        std::size_t baseVertexCount, const uint32_t* baseIndices, std::size_t baseIndexCount,
        const float* overlayPositions, const float* overlayNormals, const uint8_t* overlayColorsRGB,
        std::size_t overlayVertexCount, const uint32_t* overlayIndices, std::size_t overlayIndexCount);

    void SetCrispHybridActive(bool active) { crispHybridActive_ = active; }
    bool GetCrispHybridActive() const { return crispHybridActive_; }
    void SetCrispShadingParams(float azimuthDeg, float sharpnessRaw, float ambient,
                                float keyIntensity, float fillIntensity);

    // ---- Shading parity with the VTK/CPU path (gui/shading_display.py) ----
    // colorMode        1 = the shader pre-compensates the swapchain's sRGB
    //                      encode so the framebuffer holds exactly the byte
    //                      VTK would have written (shipping default);
    //                  0 = the previous raw linear write.
    // debugStage       0 = final shaded colour, 1 = face normal, 2 = lighting
    //                  factor, 3 = class colour, 4 = raw N.L (staged isolation
    //                  of the pipeline; see shaders/surface.frag).
    // ambientFloor     _crisp_blend_ambient_floor(app); < 0 = in-shader default.
    // baseLightElevationDeg _shading_fixed_light_elevation(app); <= 0 = default.
    // Push-constant only - never touches a GPU buffer.
    void SetShadingParityParams(int colorMode, int debugStage,
                                float ambientFloor, float baseLightElevationDeg);
    uint64_t GetParityParamUpdateCount() const { return parityParamUpdates_; }

    uint64_t GetShadeParamUpdateCount() const { return shadeParamUpdates_; }
    std::size_t GetOverlayTriangleCount() const { return overlayIndexCount_ / 3; }
    // Base-mesh and mixed-face-overlay draw submissions. "Shaded Class is on
    // screen" is exactly: meshUploads == 1 AND these counts advancing per frame.
    uint64_t GetDrawCallCount() const { return drawCalls_; }
    uint64_t GetIndexedDrawCallCount() const { return indexedDrawCalls_; }
    uint64_t GetOverlayDrawCallCount() const { return overlayDrawCalls_; }

    // Called by Renderer when the swapchain format changed (pipeline
    // rebuild with a new color attachment format).
    void OnSwapchainRebuild();

private:
    Renderer* renderer_ = nullptr;
    PipelineConfig pipelineCfg_;

    struct PushConstants {
        float lightDirAmbient[4];   // xyz = light dir, w = ambient
        float elevRangeMode[4];     // x = zMin, y = zMax, z = mode,
                                    // w = base light elevation (crisp modes 4/5)
        // Crisp-hybrid Shaded Class params (mode 4/5 only; unused by 0-3).
        float shadeParamsA[4];      // x = azimuthDeg, y = sharpnessRaw(0-999),
                                    // z = ambient, w = ambient floor
        float shadeParamsB[4];      // x = keyIntensity, y = fillIntensity,
                                    // z = debug stage, w = colour mode
    };

    GPUBuffer positions_;
    GPUBuffer normals_;
    GPUBuffer colors_;
    GPUBuffer indices_;

    // PASS 2 (mixed-face barycentric overlay) buffers - separate from the
    // PASS 1 base buffers above so a shading-parameter change never has to
    // touch either set.
    GPUBuffer positions2_;
    GPUBuffer normals2_;
    GPUBuffer colors2_;
    GPUBuffer indices2_;
    std::size_t overlayIndexCount_ = 0;
    bool crispHybridActive_ = false;
    float crispAzimuthDeg_ = 45.0f;
    float crispSharpnessRaw_ = 45.0f;
    float crispAmbient_ = 0.25f;
    float crispKeyIntensity_ = 0.85f;
    float crispFillIntensity_ = 0.18f;
    uint64_t shadeParamUpdates_ = 0;

    // ---- GPU class-colour LUT (Phase C) -----------------------------------
    // 256 entries x RGBA8 = 1024 B. Deliberately its OWN buffer so a palette
    // change is a single 1 KB upload that touches neither the geometry buffers
    // above nor the mesh-upload counter.
    GPUBuffer classLut_;
    uint64_t classLutUpdates_ = 0;
    uint64_t lutRevision_ = 0;   // bumped on every LUT write
    static constexpr std::size_t kClassLutEntries = 256;

    // Shading-parity state (see SetShadingParityParams). Defaults are the
    // shipping byte-parity behaviour so a client that never calls the setter
    // still matches the VTK viewport instead of silently reverting to the
    // brighter raw-linear write.
    int parityColorMode_ = 1;
    int parityDebugStage_ = 0;
    float parityAmbientFloor_ = -1.0f;          // < 0 -> shader default (0.08)
    float parityBaseElevationDeg_ = 0.0f;       // <= 0 -> shader default (45)
    uint64_t parityParamUpdates_ = 0;

    VkPipeline pipelineSolid_ = VK_NULL_HANDLE;
    VkPipeline indexedPipeline_ = VK_NULL_HANDLE;
    VkPipelineLayout indexedLayout_ = VK_NULL_HANDLE;
    VkDescriptorSetLayout cellLayout_ = VK_NULL_HANDLE;
    ShaderModule indexedVs_ = {}, indexedFs_ = {};
    // PASS 2 uses LESS_OR_EQUAL, not LESS: the overlay is the same triangles as
    // the base (bit-identical vertex positions), so with LESS every overlay
    // fragment was rejected against the base's own depth and the entire
    // mixed-class barycentric blend disappeared. VTK renders that overlay with
    // ResolveCoincidentTopology polygon offset for the same reason (see
    // gui/shading_display.py _build_static_multiclass_blend_overlays).
    VkPipeline pipelineSolidOverlay_ = VK_NULL_HANDLE;
    VkPipeline pipelineWireframe_ = VK_NULL_HANDLE;
    VkPipelineLayout pipelineLayout_ = VK_NULL_HANDLE;
    ShaderModule vs_ = {};
    ShaderModule fsSolid_ = {};
    ShaderModule fsWireframe_ = {};

    std::size_t vertexCount_ = 0;
    std::size_t indexCount_ = 0;
    std::size_t triangleCount_ = 0;
    ShadingMode shadingMode_ = ShadingMode::Flat;
    float zMin_ = 0.0f;
    float zMax_ = 1.0f;
    float lightDir_[3] = {0.0f, 0.0f, 1.0f};
    float ambient_ = 0.3f;
    uint64_t meshUploads_ = 0;
    uint64_t modeSwitches_ = 0;
    uint64_t drawCalls_ = 0;
    uint64_t indexedDrawCalls_ = 0;
    uint64_t overlayDrawCalls_ = 0;
    bool wireframeSupported_ = false;
    bool initialized_ = false;

    // ---- Tiled / culled / LOD state (Parts 6-9) ---------------------------
    //
    // active_ is what the draw path uses. pending_ is a fully uploaded but not
    // yet promoted set. retired_ holds sets whose GPU work has provably
    // finished and which can be destroyed on the next RetireCompleted().
    // This ACTIVE/PENDING split is what removes the old behaviour where a new
    // surface destroyed the live buffers before the swap.
    std::unique_ptr<SurfaceResourceSet> active_;
    std::unique_ptr<SurfaceResourceSet> pending_;
    std::vector<std::unique_ptr<SurfaceResourceSet>> retired_;

    // Revisions the stale guard compares PENDING against. Bumped by the C API
    // when the dataset or the surface generation changes underneath us.
    uint64_t currentDatasetRevision_ = 0;
    uint64_t currentSurfaceRevision_ = 0;

    // Interaction / LOD policy. Moving forces at least `movingLodFloor_`;
    // after `idleRefineMs_` of stillness the requested level is allowed back.
    bool interactionMoving_ = false;
    double lastInteractionMs_ = 0.0;
    double idleRefineMs_ = 400.0;
    uint32_t movingLodFloor_ = 2;      // interaction/coarse while dragging
    uint32_t requestedLod_ = 0;        // what a settled camera may use
    float lodErrorPixelLimit_ = 1.0f;  // geometric error budget, in pixels

    // Safe VRAM budget (Part 11). 70% of the device-local heap leaves room for
    // the swapchain, depth, staging and driver overhead during a swap.
    double vramBudgetBytes_ = 0.0;
    VkDeviceSize peakBytesDuringSwap_ = 0;
    bool memoryRejected_ = false;
    uint64_t swapCount_ = 0;
    uint64_t staleDiscards_ = 0;
    // Cached for the current frame: the frustum and projected scale are
    // derived from the live MVP once per Record(), never per tile.
    FrustumPlanes lastFrustum_;
    ProjectedScale lastScale_;
    bool lastCullingValid_ = false;
};

} // namespace naksha
