#pragma once
#include <functional>
#include <memory>
#include <vector>

#include "naksha/VulkanContext.hpp"
#include "naksha/Camera.hpp"
#include "naksha/RenderOrigin.hpp"
#include "naksha/core/vulkan/VulkanRenderPass.h"
#include "naksha/core/vulkan/VulkanFrameManager.h"
#include "naksha/core/vulkan/VulkanSwapchain.h"
#include "naksha/core/vulkan/VulkanDescriptorManager.h"
#include "naksha/core/vulkan/VulkanPipelineManager.h"
#include "naksha/core/vulkan/VulkanShaderManager.h"
#include "naksha/Buffer.hpp"

namespace naksha {
using namespace vulkan;

class PointCloudRenderer;
class SurfaceRenderer;

// Per-slot UBO layout (std140). Column-major float32 mat4, matching GLSL mat4.
//
// The three 256-entry LUTs at the tail are the GPU-side colouring tables the
// point shader reads (classification -> RGB, normalized z -> RGB, normalized
// intensity -> RGB). They are refreshed only when a dataset loads or the user
// edits the class palette, and are copied into every frame's mapped UBO, so a
// display-mode switch stays a push-constant change instead of a per-point
// colour re-bake on the CPU.
//
// Surface shaders may declare only the 96-byte mvp/cameraPos/origin prefix:
// declaring a prefix of a uniform block is valid and leaves the LUT tail
// unused by those stages.
struct FrameUbo {
    float mvp[16];
    float cameraPos[4];   // render-space eye (w=0)
    float origin[4];      // world render origin (w=0)
    float classPalette[256 * 4];    // GLSL: vec4 classPalette[256]
    float elevationLut[256 * 4];    // GLSL: vec4 elevationLut[256]
    float intensityLut[256 * 4];    // GLSL: vec4 intensityLut[256]
};

struct RendererConfig {
    void* nativeWindowHandle = nullptr; // HWND (engine creates Win32 surface)
    VkSurfaceKHR externalSurface = VK_NULL_HANDLE;
    uint32_t width = 1280;
    uint32_t height = 720;
    bool enableValidation = false;
    uint32_t maxFramesInFlight = 2;
    std::string shaderDirectory; // directory containing .spv ("" -> exeDir/shaders)
    bool enableGpuTimestamps = true;
    std::string appName = "NakshaVulkan";
};

struct GpuTimings {
    double frameGpuMs = 0.0;   // timestamp span: frame start -> frame end
    double pointGpuMs = 0.0;
    double surfaceGpuMs = 0.0;
    bool valid = false;
    // CPU-side costs, measured around the individual driver calls. These are
    // deliberately separate from frameGpuMs so a "CPU time" report can never
    // be a GPU number in disguise.
    double cpuSubmitMs = 0.0;  // time inside vkQueueSubmit
    double presentMs = 0.0;    // time inside vkQueuePresentKHR

    // ---- Per-stage CPU frame breakdown (PHASE 1 of the optimisation pass) ----
    // The engine ALREADY had cpuSubmitMs/presentMs but the C API exposed none of
    // it, which made "reduce fixed frame overhead" unmeasurable. These are the
    // remaining stages, each measured around the real driver call, so a report
    // can never confuse a CPU number with a GPU one.
    double fenceWaitMs = 0.0;      // VulkanFrameManager::BeginFrame
    double acquireMs = 0.0;        // vkAcquireNextImageKHR
    double uboUpdateMs = 0.0;      // UpdateFrameUbo + aspect set
    double beginFrameMs = 0.0;     // cmd reset/begin + render-pass begin
    double commandRecordMs = 0.0;  // point/surface Record() only
    double endFrameMs = 0.0;       // cmd end + fence reset (excl. submit/present)
    double frameCpuTotalMs = 0.0;  // BeginFrame..EndFrame inclusive

    // Frames the CPU wanted but could not issue because every frame slot was
    // still legitimately busy (PHASE 6). Must never be silently blocked.
    uint64_t framesDeferred = 0;
};

struct FrameStatistics {
    uint64_t framesRendered = 0;
    uint64_t framesSkipped = 0;   // minimized / out-of-date, no submit
    uint64_t swapchainRecreates = 0;
};

// ---------------------------------------------------------------------------
// Top-level renderer. Owns the Vulkan instance, device, swapchain, render
// pass, framegraph, descriptor/pipeline/shader managers and a shared
// staging uploader. Frame slot N keeps its own command buffer, fence,
// semaphore, UBO and descriptor set so N frames can be in flight.
//
// Render pass, frame UBO and the frame descriptor set layout are the
// authoritative shared set-0 layout for both sub-renderers. The engine
// begins and ends the render pass around sub-renderer::Record() calls,
// so sub-renderers only issue draw commands and timestamp writes.
// ---------------------------------------------------------------------------
class Renderer {
public:
    Renderer() = default;
    ~Renderer();

    bool Initialize(const RendererConfig& cfg);
    void Shutdown();
    // Blocks until the GPU has finished all submitted work. Callers that own
    // sub-renderers (PointCloudRenderer/SurfaceRenderer, which hold a raw
    // Renderer* and destroy pipelines/buffers through it) must call this
    // before tearing those sub-renderers down whenever the last frame's
    // command buffer may still be in flight - otherwise the destroy calls
    // race the GPU and trip "still in use" validation errors.
    void WaitIdle();
    bool IsInitialized() const { return initialized_; }

    // ---- Frame loop (spec section: frame sync rule) ----
    // Returns false when the frame must be skipped
    // (minimized / VK_ERROR_OUT_OF_DATE_KHR). The fence for a skipped
    // frame is NOT reset (BeginFrame waits on it and the submit that
    // re-signals it is deferred until restore).
    bool BeginFrame();
    void EndFrame();

    // Monotonic count of submitted frames, and the true "is every frame up to
    // F provably finished?" test. Surface resource retirement uses these to
    // destroy a retired buffer set WITHOUT the vkDeviceWaitIdle() that the
    // old single-resource upload path effectively depended on.
    uint64_t GetFrameCounter() const { return frameManager_.GetFrameCounter(); }
    bool IsFrameRetired(uint64_t frameIndex) const {
        return frameManager_.IsFrameRetired(frameIndex);
    }

    VkCommandBuffer CurrentCommandBuffer() const;
    uint32_t CurrentFrameSlot() const;
    VkExtent2D GetExtent() const;
    bool IsMinimized() const;

    // Rebuild everything that depends on swapchain extent. Called
    // automatically on VK_ERROR_OUT_OF_DATE_KHR / VK_SUBOPTIMAL_KHR,
    // or explicitly after a window resize. Returns false when still
    // minimized (nothing rebuilt, frame skipped).
    bool RecreateSwapchain(uint32_t w, uint32_t h);

    // ---- Shared resources for sub-renderers ----
    VulkanContext& GetContext();
    Camera& GetCamera();
    const Camera& GetCamera() const;
    RenderOrigin& GetOrigin();
    const RenderOrigin& GetOrigin() const;
    void SetOrigin(double x, double y, double z);

    // GPU colouring tables for the point path (see FrameUbo). Each argument is
    // optional - pass nullptr to leave that table as-is - and is 256*3 uint8
    // RGB indexed exactly the way the point shader indexes it (class id, or
    // normalized z / intensity at t*255). Refreshed on dataset load or palette
    // edit; copied into the mapped UBO every frame.
    void SetPointLUTs(const uint8_t* classRgb, const uint8_t* elevationRgb,
                      const uint8_t* intensityRgb);
    // How many times a colour table was replaced. Must increase on a PTC /
    // palette / visibility / weight change while the point upload count does
    // not (see GetPositionUploadCount in PointCloudRenderer).
    uint64_t GetLutUpdateCount() const { return lutUpdates_; }

    VkRenderPass GetRenderPass() const;
    // The Instant Shaded render pass (colour + STORED-normal + depth). Public
    // so PointCloudRenderer can build the matching splat pipeline: a pipeline is
    // only compatible with the render pass it was created against.
    VkRenderPass GetInstantRenderPass() const {
        return instantRenderPass_.GetRenderPass();
    }
    // 0 = STORED oct16 normals (production), 1 = screen-space reconstruction
    // (DEV fallback / A-B comparison). Pushed as the lighting pass's
    // camera.w, so switching normal source costs one float.
    void SetInstantNormalSource(int screenSpace) {
        instantNormalSource_ = (screenSpace != 0) ? 1 : 0;
    }
    int GetInstantNormalSource() const { return instantNormalSource_; }
    VkDescriptorSetLayout GetFrameSetLayout() const;
    const VkDescriptorSet& GetFrameDescriptorSet() const { return frameSets_[frameManager_.GetCurrentFrameIndex()]; }
    UploadContext& GetUploadContext();
    VulkanPipelineManager& Pipelines();
    VulkanShaderManager& Shaders();
    VulkanDescriptorManager& Descriptors();

    // ---- Timestamps (enabled only if RendererConfig::enableGpuTimestamps) ----
    static constexpr uint32_t kTimestampsPerFrame = 8;
    // Points at offset kTsPointBefore/kTsPointAfter within the slot's
    // range; surface at kTsSurfaceBefore/kTsSurfaceAfter; frame at 0/5.
    enum TimestampSlot : uint32_t {
        kTsPointBefore = 1, kTsPointAfter = 2,
        kTsSurfaceBefore = 3, kTsSurfaceAfter = 4
    };
    // Writes a timestamp at (slot * kTimestampsPerFrame + subSlot) into
    // the per-frame query pool. The command must be the one currently in
    // flight. NO-op when timestamps are disabled.
    void WriteTimestamp(VkCommandBuffer cmd, uint32_t slot, uint32_t subSlot);
    VkQueryPool GetTimestampQueryPool() const { return timestampPool_; }

    const GpuTimings& LastGpuTimings() const;
    void SetCommandRecordMs(double ms) { lastTimings_.commandRecordMs = ms; }
    double LastCpuFrameMs() const;

    // ---- Pipeline rebuild callback (format change) ----
    using RebuildCallback = std::function<void()>;
    uint64_t RegisterSwapchainRebuildCallback(RebuildCallback cb);
    void UnregisterSwapchainRebuildCallback(uint64_t id);

    const FrameStatistics& GetFrameStatistics() const;
    void PrintDiagnostics() const;

    // ---- Viewport clear colour ------------------------------------------
    // Colour the BeginFrame() render pass clears the colour attachment to.
    // Defaults to opaque black (the historic behaviour). Exposed so the
    // viewport can use the app's dark theme colour AND so a pixel test can
    // prove a captured frame really came from this renderer (a Vulkan clear
    // colour that differs from every Qt/Win32 background colour is
    // unambiguous evidence of a real presented frame).
    void SetClearColor(float r, float g, float b, float a = 1.0f);
    void GetClearColor(float out[4]) const;

    // Last MVP matrix actually written into the frame UBO for the render
    // space (column-major float32, exactly as GLSL mat4 sees it). Lets a
    // caller verify the camera/matrix pipeline without a pixel readback.
    void GetLastMvp(float out[16]) const;

    // ---- Instant Shaded Class (NkvDisplayMode NKV_DISPLAY_SHADED_CLASS_INSTANT) ----
    //
    // A two-pass replacement for the legacy TIN path. NO mesh is built, ever:
    //   pass 1  the point pipeline draws the resident points into a private
    //           colour+depth target (class colour + depth), at 1-3 px splat
    //           size - the same buffers and the same pipeline as every other
    //           point mode, just a different framebuffer;
    //   pass 2  a fullscreen triangle lights that target, reconstructing the
    //           per-pixel normal from the depth attachment (finite differences
    //           + a discontinuity guard) and applying the SAME hillshade
    //           gui/shading_display.py::_compute_shading uses.
    //
    // Switching modes is therefore pure state: it writes a float (display
    // mode) and a few push constants. It never uploads positions, never
    // uploads a mesh, never calls vkDeviceWaitIdle and never touches
    // Delaunay. That is the whole point of this path.
    //
    // The two offscreen attachments are created lazily on the first instant
    // frame and destroyed/resized with the swapchain (DestroyInstantTargets,
    // called from the same places that already WaitIdle before a rebuild).
    bool IsInstantShadedActive() const { return instantActive_; }
    // True only when the mode is requested AND the pass actually built - the
    // caller must fall back to the ordinary point/surface Record() path when
    // this is false (missing shaders, allocation failure).
    bool IsInstantShadedAvailable() const {
        return instantActive_ && instantPassReady_;
    }
    void SetInstantShadedActive(bool on);
    // Sun azimuth (compass degrees), light elevation (degrees above horizon)
    // and the ambient floor - the exact triple _compute_shading() consumes.
    // Push-constant only, safe to call on every slider move.
    void SetInstantShading(float azimuthDeg, float elevationDeg, float ambient);
    // 0 final colour, 1 reconstructed normal, 2 lighting factor, 3 class
    // colour - mirrors NAKSHA_SHADED_DEBUG_VIEW on the Python side.
    void SetInstantDebugStage(int stage) { instantDebugStage_ = stage; }
    // Reconstruction neighbourhood radius in texels (1..8). Wider averages over
    // the same planar surface, which is what stops a 1-2 px splat depth buffer
    // from reconstructing pure per-pixel noise.
    void SetInstantNormalRadius(int radiusTexels) {
        instantNormalRadius_ = (radiusTexels < 1) ? 1
                             : (radiusTexels > 8 ? 8 : radiusTexels);
    }
    int GetInstantNormalRadius() const { return instantNormalRadius_; }
    // Frames recorded through the instant path since creation. Lets a test
    // prove the mode really rendered without reading a pixel back.
    uint64_t GetInstantFrameCount() const { return instantFrames_; }
    // Sub-renderer record step for this mode. Called between BeginFrame() and
    // EndFrame() INSTEAD of pointCloud/surface Record(): it closes the
    // (still-empty) main render pass, runs the splat pass offscreen, barriers
    // the results to shader-read, then reopens the main render pass and runs
    // the fullscreen lighting pass into the swapchain. EndFrame() then ends and
    // submits exactly as it always does.
    void RecordInstantShadedPass(VkCommandBuffer cmd, uint32_t frameSlot,
                                 class PointCloudRenderer& pointCloud);

    // Class visibility: 256 bytes (1 = shown). Stored beside the class colour
    // LUT and folded into the palette's alpha, so a Display Mode checkbox
    // change is a KB-scale uniform upload rather than a point re-upload.
    // Returns the number of times a visibility table has been replaced.
    void SetClassVisibility(const uint8_t* visible256);
    uint64_t GetClassVisibilityUpdateCount() const { return classVisibilityUpdates_; }

    // ---- Offscreen diagnostic capture (Part 6: real pixel comparison) ----
    // Fully self-contained: does NOT touch the swapchain, the per-frame
    // BeginFrame/EndFrame path, or any state the normal render loop relies
    // on - it renders the CURRENT persistent point/surface buffers (via
    // PointCloudRenderer::Record/SurfaceRenderer::Record, same calls the
    // normal frame uses) into a private offscreen color+depth image pair
    // using the same render pass/pipelines, using VulkanAllocator's
    // BeginSingleTimeCommands/EndSingleTimeCommands (blocking - a diagnostic
    // capture, not a per-frame path). Uses the CURRENT frame slot's UBO
    // (last camera set via nkv_set_camera_lookat), so the captured image
    // reflects the same camera as the live view. Returns RGBA8,
    // top-to-bottom row order, width*height*4 bytes.
    bool CaptureOffscreenRGBA8(class PointCloudRenderer* pointCloud, class SurfaceRenderer* surface,
                                std::vector<uint8_t>& outRGBA, uint32_t& outW, uint32_t& outH);

    // Depth buffer readback for the SAME offscreen capture above, as float32
    // in [0,1] (raw depth attachment value, no near/far remap applied here).
    // Top-to-bottom row order, width*height*4 bytes.
    //
    // Why this exists: a per-pixel shading comparison is only meaningful if the
    // sampled pixel is actually owned by the face being compared. Colour alone
    // cannot tell you that - a different, nearer face can legitimately own the
    // pixel and produce a perfectly valid colour. Depth is the ground truth for
    // "who won the depth test here", so the parity gate reads it and rejects any
    // sample where a nearer face won.
    //
    // Renders with the same calls as CaptureOffscreenRGBA8 (pointCloud/surface
    // Record into the same render pass, same frame slot), so the depth returned
    // corresponds to the same geometry/camera as a colour capture taken at the
    // same point in the frame.
    bool CaptureOffscreenDepth32F(class PointCloudRenderer* pointCloud, class SurfaceRenderer* surface,
                                   std::vector<float>& outDepth, uint32_t& outW, uint32_t& outH);

private:
    bool RecreateSwapchainInternal();
    void UpdateFrameUbo(uint32_t slot);
    void ReadTimestamps(uint32_t slot);
    void CreateFrameResources(uint32_t slot);
    void DestroyFrameResources(uint32_t slot);
    bool CreateDepthForImage(uint32_t swapchainImageIndex);
    void DestroyCaptureTarget();

    // ---- Instant Shaded Class pass ----
    // Built once with the renderer (pipeline, layouts, sampler); the two
    // attachments are rebuilt whenever the extent changes.
    bool InitializeInstantPass();
    void ShutdownInstantPass();
    bool EnsureInstantTargets();
    void DestroyInstantTargets();

    RendererConfig cfg_;
    VulkanContext context_;
    VulkanSwapchain swapchain_;
    VulkanRenderPass renderPass_;
    VulkanFrameManager frameManager_;
    VulkanDescriptorManager descriptorManager_;
    VulkanPipelineManager pipelineManager_;
    VulkanShaderManager shaderManager_;
    UploadContext uploadContext_;

    std::vector<VkFramebuffer> framebuffers_;
    // Persistently mapped UBO, one per in-flight frame slot.
    std::vector<GPUBuffer> frameUboBuffers_;
    std::vector<VmaAllocation> frameUboAllocs_;
    std::vector<VkDescriptorSet> frameSets_;
    // One depth image + view per swapchain image (no shared depth
    // across in-flight frames).
    std::vector<GPUImage> depthImages_;
    std::vector<VkImageView> depthViews_;

    // ---- Offscreen capture target (CaptureOffscreenRGBA8 / nkv_capture_frame)
    // A private colour image at the CURRENT swapchain format/extent plus a
    // framebuffer for the SAME render pass, so the point/surface pipelines are
    // reused verbatim - there is deliberately no second pipeline set for a
    // different colour format. Rebuilt lazily whenever the extent changes and
    // destroyed with the swapchain (see DestroyCaptureTarget).
    GPUImage captureImage_{};
    VkImageView captureView_ = VK_NULL_HANDLE;
    VkFramebuffer captureFramebuffer_ = VK_NULL_HANDLE;

    // ---- Instant Shaded Class (see the public section) --------------------
    // The render pass is a PRIVATE one, not renderPass_, for two reasons:
    //   * its colour attachment is an offscreen image, so its final layout
    //     must be COLOR_ATTACHMENT_OPTIMAL (renderPass_ hardcodes PRESENT_SRC,
    //     which only the swapchain image may use);
    //   * the depth attachment must be STORED and is read back by the lighting
    //     pass, which renderPass_ supports only for nkv_capture_depth and which
    //     still ends in DEPTH_STENCIL_ATTACHMENT_OPTIMAL (not shader-readable).
    // It is created with the same colour/depth FORMAT and sample count as
    // renderPass_, so the EXISTING point pipeline is compatible with it and is
    // reused verbatim - there is no second point pipeline to keep in sync.
    VulkanRenderPass instantRenderPass_;
    // Attachment 1 of the instant render pass: the per-splat STORED oct16x2
    // normal. Separate from the depth image and recreated with it.
    GPUImage instantNormalImage_{};
    GPUImage instantColorImage_{};
    GPUImage instantDepthImage_{};
    VkFramebuffer instantFramebuffer_ = VK_NULL_HANDLE;
    VkPipeline instantPipeline_ = VK_NULL_HANDLE;
    VkPipelineLayout instantPipelineLayout_ = VK_NULL_HANDLE;
    ShaderModule instantVs_ = {};
    ShaderModule instantFs_ = {};
    VkDescriptorSetLayout instantSet1Layout_ = VK_NULL_HANDLE;
    VkDescriptorPool instantPool_ = VK_NULL_HANDLE;
    VkDescriptorSet instantSet1_ = VK_NULL_HANDLE;
    VkSampler instantSampler_ = VK_NULL_HANDLE;
    bool instantActive_ = false;
    bool instantPassReady_ = false;
    bool instantTargetsReady_ = false;
    // Colour format the private render pass was built with. The swapchain can
    // come back with a different format after a surface change; a render pass
    // is format-specific, so EnsureInstantTargets() rebuilds the pass when this
    // stops matching instead of handing Vulkan a mismatched framebuffer.
    VkFormat instantPassColorFormat_ = VK_FORMAT_UNDEFINED;
    float instantAzimuthDeg_ = 45.0f;
    float instantElevationDeg_ = 45.0f;
    float instantAmbient_ = 0.25f;
    int instantDebugStage_ = 0;
    int instantNormalRadius_ = 2;
    int instantNormalSource_ = 0;   // 0 = stored oct16 (production)
    uint64_t instantFrames_ = 0;
    uint64_t instantTargetRebuilds_ = 0;
    bool instantSelfTestDone_ = false;

    // Class visibility folded into lutClass_'s alpha (see SetClassVisibility).
    // Kept separately from the colour table so a colour push never silently
    // re-shows a class the user unchecked (StoreLut always writes alpha = 1).
    uint8_t classVisible_[256];
    uint64_t classVisibilityUpdates_ = 0;

    VkDescriptorSetLayout frameSetLayout_ = VK_NULL_HANDLE;
    VkDescriptorPool descriptorPool_ = VK_NULL_HANDLE;
    VkQueryPool timestampPool_ = VK_NULL_HANDLE;
    VkExtent2D extent_ = {0, 0};
    VkFormat depthFormat_ = VK_FORMAT_UNDEFINED;
    VkCommandPool frameCmdPool_ = VK_NULL_HANDLE;
    uint32_t imageCount_ = 0;
    std::vector<VkCommandBuffer> commandBuffers_;
    // Command buffers/framebuffers/depth images are indexed by the ACQUIRED
    // SWAPCHAIN IMAGE (0..imageCount-1), because BeginFrame() begins the
    // render pass in commandBuffers_[imageIndex] and EndFrame() submits that
    // same buffer. The per-in-flight-frame slot (0..maxFramesInFlight-1) is a
    // DIFFERENT index space and only owns fences/semaphores/UBO/descriptor
    // sets/timestamp queries. Recording the draws into commandBuffers_[slot]
    // (as CurrentCommandBuffer() used to) put them in a buffer that was never
    // submitted, so every presented frame showed the clear colour alone.
    uint32_t currentImageIndex_ = 0;
    uint32_t maxFramesInFlight_ = 2;

    Camera camera_;
    RenderOrigin origin_;
    bool enableGpuTimestamps_ = true;
    // True while CaptureOffscreenRGBA8() records into its own single-time
    // command buffer. That path never runs the per-frame vkCmdResetQueryPool,
    // so letting PointCloudRenderer/SurfaceRenderer write timestamps from it
    // made validation report "vkCmdWriteTimestamp(): query N not reset"
    // (VUID-vkCmdWriteTimestamp-None-00830) on every capture.
    bool captureActive_ = false;
    std::vector<bool> timestampsWritten_;
    bool recreateRequested_ = false;
    GpuTimings lastTimings_{};
    FrameStatistics stats_{};
    float clearColor_[4] = {0.0f, 0.0f, 0.0f, 1.0f};
    // Colouring tables mirrored into the LUT tail of every FrameUbo. Filled
    // with sensible defaults in Renderer::Initialize so a cloud drawn before
    // Python pushes its own tables still gets colours instead of black.
    float lutClass_[256 * 4];
    float lutElevation_[256 * 4];
    float lutIntensity_[256 * 4];
    // Number of nkv_set_point_luts() calls that actually changed a table.
    // Together with PointCloudRenderer::GetPositionUploadCount() this is the
    // measured form of "the GPU re-tints on a palette change, the point buffer
    // is never re-uploaded".
    uint64_t lutUpdates_ = 0;
    double lastCpuFrameStartMs_ = 0.0;

    std::vector<std::pair<uint64_t, RebuildCallback>> rebuildCallbacks_;
    uint64_t nextCallbackId_ = 1;
    bool initialized_ = false;

    friend class PointCloudRenderer;
    friend class SurfaceRenderer;
};

} // namespace naksha
