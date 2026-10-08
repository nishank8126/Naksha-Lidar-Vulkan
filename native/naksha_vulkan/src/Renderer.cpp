#include "naksha/Renderer.hpp"
#include "naksha/PointCloudRenderer.hpp"
#include "naksha/SurfaceRenderer.hpp"
#include <algorithm>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <limits>
#include <utility>

namespace naksha {

Renderer::~Renderer() { Shutdown(); }

static VkSurfaceFormatKHR ChooseSurfaceFormat(
    const std::vector<VkSurfaceFormatKHR>& formats) {
    for (auto& f : formats) {
        if (f.format == VK_FORMAT_B8G8R8A8_SRGB &&
            f.colorSpace == VK_COLOR_SPACE_SRGB_NONLINEAR_KHR)
            return f;
    }
    return formats.front();
}

static VkPresentModeKHR ChoosePresentMode(
    const std::vector<VkPresentModeKHR>& modes) {
    for (auto& m : modes) {
        if (m == VK_PRESENT_MODE_MAILBOX_KHR) return m;
    }
    return VK_PRESENT_MODE_FIFO_KHR;
}

// Defined further down in this file, next to the UBO update that consumes it.
namespace { void FillDefaultLut(float* lut, int kind); }

bool Renderer::Initialize(const RendererConfig& cfg) {
    cfg_ = cfg;
    // The colouring tables must exist before the first frame: the point shader
    // indexes them for every display mode except RGB. Python replaces them
    // with the app's own LUTs (nkv_set_point_luts) as soon as a dataset loads;
    // these defaults only cover the gap before that.
    FillDefaultLut(lutClass_, 0);
    FillDefaultLut(lutElevation_, 1);
    FillDefaultLut(lutIntensity_, 2);
    // Every class starts visible; nkv_set_class_visibility replaces this.
    std::memset(classVisible_, 1, sizeof(classVisible_));
    maxFramesInFlight_ = std::max<uint32_t>(cfg.maxFramesInFlight, 1u);
    enableGpuTimestamps_ = cfg.enableGpuTimestamps;

    if (cfg.shaderDirectory.empty()) {
        char path[MAX_PATH];
        GetModuleFileNameA(nullptr, path, MAX_PATH);
        std::string exePath(path);
        std::size_t sep = exePath.find_last_of("\\/");
        cfg_.shaderDirectory = exePath.substr(0, sep + 1) + "shaders";
    }

    // ---- Context ----
    ContextConfig ccfg;
    ccfg.enableValidation = cfg.enableValidation;
    ccfg.nativeWindowHandle = cfg.nativeWindowHandle;
    ccfg.externalSurface = cfg.externalSurface;
    ccfg.appName = cfg.appName;
    if (!context_.Initialize(ccfg)) return false;
    fprintf(stderr, "[REN] context init OK\n");

    // ---- GPU allocator (VMA), bound to volk-loaded dynamic Vulkan functions ----
    // NOTE: this call was previously missing entirely, leaving VulkanAllocator's
    // singleton allocator_ handle at VK_NULL_HANDLE. Every vmaCreateBuffer() call
    // then dereferenced a null VmaAllocator_T*, which crashed inside VMA's
    // CreateBuffer() (SIGSEGV) on the very first buffer allocation.
    {
        VmaVulkanFunctions vmaFuncs{};
        vmaFuncs.vkGetInstanceProcAddr = vkGetInstanceProcAddr;
        vmaFuncs.vkGetDeviceProcAddr = vkGetDeviceProcAddr;
        VulkanAllocator::Get().Initialize(
            context_.GetInstance(), context_.GetPhysicalDevice(), context_.GetDevice(),
            context_.GetGraphicsQueue(), context_.GetQueueFamilies().graphicsFamily,
            vmaFuncs);
        if (VulkanAllocator::Get().GetAllocator() == VK_NULL_HANDLE) {
            fprintf(stderr, "[REN] VulkanAllocator::Initialize FAILED\n");
            return false;
        }
        fprintf(stderr, "[REN] allocator init OK\n");
    }

    // ---- Swapchain ----
    fprintf(stderr, "[REN] querying swapchain support...\n");
    auto support = context_.QuerySwapchainSupport();
    fprintf(stderr, "[REN] swapchain support OK\n");
    auto surfFmt = ChooseSurfaceFormat(support.formats);
    VkExtent2D ext = {cfg.width, cfg.height};
    if (support.capabilities.currentExtent.width !=
            (std::numeric_limits<uint32_t>::max)())
        ext = support.capabilities.currentExtent;
    extent_ = ext;
    imageCount_ = support.capabilities.minImageCount + 1;
    if (support.capabilities.maxImageCount > 0)
        imageCount_ = std::min(imageCount_, support.capabilities.maxImageCount);

    depthFormat_ = ChooseDepthFormat(context_.GetPhysicalDevice());
    fprintf(stderr, "[REN] depthFormat=%d\n", depthFormat_);

    if (!swapchain_.Initialize(context_.GetDevice(), context_.GetPhysicalDevice(),
                                    context_.GetSurface(), ext.width, ext.height,
                                    support, context_.GetQueueFamilies()))
        return false;
    fprintf(stderr, "[REN] swapchain OK\n");

    // ---- Render pass ----
    RenderPassConfig rpc;
    rpc.colorFormat = swapchain_.GetImageFormat();
    rpc.depthFormat = depthFormat_;
    rpc.loadColorClear = true;
    rpc.storeColor = true;
    // STORE the depth attachment. RenderPassConfig::storeDepth defaults to
    // false, which makes VulkanRenderPass build the attachment with
    // storeOp = DONT_CARE - the depth values are then discarded instead of
    // written to memory. Depth testing still works during the frame, so
    // nothing looks wrong on screen, but nkv_capture_depth's
    // vkCmdCopyImageToBuffer then reads uninitialised memory: measured garbage
    // spanning ~1e30 while the coverage mask still looked correct. A depth
    // readback is impossible without this.
    rpc.storeDepth = true;
    rpc.loadDepthClear = true;
    rpc.depthLoadOp = VK_ATTACHMENT_LOAD_OP_CLEAR;
    fprintf(stderr, "[REN] initializing render pass...\n");
    if (!renderPass_.Initialize(context_.GetDevice(), rpc)) return false;
    fprintf(stderr, "[REN] render pass OK\n");

    // ---- Frame sync + command pool ----
    if (!frameManager_.Initialize(context_.GetDevice(), maxFramesInFlight_, imageCount_))
        return false;

    VkCommandPoolCreateInfo cpi{VK_STRUCTURE_TYPE_COMMAND_POOL_CREATE_INFO};
    cpi.flags = VK_COMMAND_POOL_CREATE_RESET_COMMAND_BUFFER_BIT;
    cpi.queueFamilyIndex = context_.GetQueueFamilies().graphicsFamily;
    if (vkCreateCommandPool(context_.GetDevice(), &cpi, nullptr, &frameCmdPool_) != VK_SUCCESS)
        return false;
    VkCommandBufferAllocateInfo ai{VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO};
    ai.commandPool = frameCmdPool_;
    ai.level = VK_COMMAND_BUFFER_LEVEL_PRIMARY;
    ai.commandBufferCount = imageCount_;
    commandBuffers_.resize(imageCount_);
    if (vkAllocateCommandBuffers(context_.GetDevice(), &ai, commandBuffers_.data()) != VK_SUCCESS)
        return false;

    // ---- Upload context (shared single-queue) ----
    float tsPeriod = static_cast<float>(context_.GetReport().timestampPeriodNs);
    if (!uploadContext_.Initialize(context_.GetDevice(), context_.GetGraphicsQueue(),
                                         context_.GetQueueFamilies().graphicsFamily, tsPeriod))
        return false;

    // ---- Timestamp query pool (per frame, slot * 8) ----
    {
        VkQueryPoolCreateInfo qpi{VK_STRUCTURE_TYPE_QUERY_POOL_CREATE_INFO};
        qpi.queryType = VK_QUERY_TYPE_TIMESTAMP;
        qpi.queryCount = maxFramesInFlight_ * kTimestampsPerFrame;
        if (vkCreateQueryPool(context_.GetDevice(), &qpi, nullptr, &timestampPool_) != VK_SUCCESS)
            return false;
    }

    // ---- Descriptor manager + layout + pool ----
    if (!descriptorManager_.Initialize(context_.GetDevice(),
                                              maxFramesInFlight_ * 4u))
        return false;
    if (!shaderManager_.Initialize(context_.GetDevice())) return false;
    if (!pipelineManager_.Initialize(context_.GetDevice())) return false;
    {
        std::vector<DescriptorBinding> bindings;
        bindings.push_back({0, VK_DESCRIPTOR_TYPE_UNIFORM_BUFFER, 1,
                              VK_SHADER_STAGE_VERTEX_BIT | VK_SHADER_STAGE_FRAGMENT_BIT});
        frameSetLayout_ = descriptorManager_.CreateLayout(bindings);
    }
    {
        std::vector<VkDescriptorPoolSize> poolSizes;
        poolSizes.push_back({VK_DESCRIPTOR_TYPE_UNIFORM_BUFFER, maxFramesInFlight_ * 4u});
        descriptorPool_ = descriptorManager_.CreatePool(poolSizes, maxFramesInFlight_ * 4u);
        if (descriptorPool_ == VK_NULL_HANDLE) return false;
    }

    // ---- Per-slot UBOs (persistently mapped) ----
    const uint32_t uboSize = static_cast<uint32_t>(sizeof(FrameUbo));
    frameUboBuffers_.resize(maxFramesInFlight_);
    for (uint32_t s = 0; s < maxFramesInFlight_; ++s) {
        frameUboBuffers_[s] = VulkanAllocator::Get().CreateBuffer(
            uboSize, VK_BUFFER_USAGE_UNIFORM_BUFFER_BIT, VMA_MEMORY_USAGE_AUTO,
            VMA_ALLOCATION_CREATE_HOST_ACCESS_SEQUENTIAL_WRITE_BIT |
            VMA_ALLOCATION_CREATE_MAPPED_BIT);
        if (!frameUboBuffers_[s].IsValid()) return false;
        VmaAllocationInfo info{};
        vmaGetAllocationInfo(VulkanAllocator::Get().GetAllocator(),
                             frameUboBuffers_[s].allocation, &info);
        frameUboBuffers_[s].mappedData = info.pMappedData;
    }

    // ---- Depth + framebuffers ----
    depthImages_.resize(imageCount_);
    depthViews_.resize(imageCount_);
    framebuffers_.resize(imageCount_);
    for (uint32_t i = 0; i < imageCount_; ++i) {
        depthImages_[i] = VulkanAllocator::Get().CreateImage(
            // TRANSFER_SRC is what makes the depth buffer readable back to the
            // CPU (nkv_capture_depth). Without it a validation layer rejects the
            // copy, and the parity gate has no way to tell "this face owns this
            // pixel" from "some other face won the depth test here" - which is
            // the only honest way to gate a per-pixel shading comparison.
            depthFormat_, extent_,
            VK_IMAGE_USAGE_DEPTH_STENCIL_ATTACHMENT_BIT | VK_IMAGE_USAGE_TRANSFER_SRC_BIT,
            VMA_MEMORY_USAGE_GPU_ONLY);
        VkImageViewCreateInfo vi{VK_STRUCTURE_TYPE_IMAGE_VIEW_CREATE_INFO};
        vi.image = depthImages_[i].image;
        vi.viewType = VK_IMAGE_VIEW_TYPE_2D;
        vi.format = depthFormat_;
        vi.subresourceRange = {VK_IMAGE_ASPECT_DEPTH_BIT, 0, 1, 0, 1};
        if (vkCreateImageView(context_.GetDevice(), &vi, nullptr, &depthViews_[i]) != VK_SUCCESS)
            return false;
        VkFramebufferCreateInfo fbi{VK_STRUCTURE_TYPE_FRAMEBUFFER_CREATE_INFO};
        fbi.renderPass = renderPass_.GetRenderPass();
        VkImageView atts[2] = {swapchain_.GetImageView(i), depthViews_[i]};
        fbi.attachmentCount = 2;
        fbi.pAttachments = atts;
        fbi.width = extent_.width;
        fbi.height = extent_.height;
        fbi.layers = 1;
        if (vkCreateFramebuffer(context_.GetDevice(), &fbi, nullptr, &framebuffers_[i]) != VK_SUCCESS)
            return false;
    }

    // ---- Per-slot descriptor sets ----
    // frameSets_ must be sized before CreateFrameResources() writes into
    // frameSets_[slot] via &frameSets_[slot] - it was previously never
    // resized, so this indexed into an empty vector's null data() pointer,
    // which is why vkAllocateDescriptorSets() received pDescriptorSets=NULL.
    frameSets_.resize(maxFramesInFlight_, VK_NULL_HANDLE);
    timestampsWritten_.assign(maxFramesInFlight_, false);
    for (uint32_t s = 0; s < maxFramesInFlight_; ++s)
        CreateFrameResources(s);

    camera_.SetAspectRatio(
        static_cast<double>(extent_.width) / static_cast<double>(extent_.height));

    // Instant Shaded Class pass (mode NKV_DISPLAY_SHADED_CLASS_INSTANT).
    // A failure here must NOT take the renderer down: the mode just stays
    // unavailable and the app falls back to the legacy TIN path.
    if (!InitializeInstantPass()) ShutdownInstantPass();

    initialized_ = true;
    stats_.framesRendered = 0;
    stats_.framesSkipped = 0;
    stats_.swapchainRecreates = 0;
    lastCpuFrameStartMs_ = NowMs();
    return true;
}

void Renderer::Shutdown() {
    if (!initialized_) return;
    initialized_ = false;

    // Per the spec: vkDeviceWaitIdle() is never called per-frame, only here
    // (shutdown) and during swapchain recreation. Without it, resources below
    // are destroyed while still referenced by an in-flight/pending command
    // buffer, which is exactly the batch of "in use" validation errors seen
    // during teardown (descriptor sets, framebuffer, query pool, command
    // pool, swapchain, semaphores, fence).
    context_.CoreDevice().WaitIdle();
    DestroyCaptureTarget();   // offscreen capture target dies with the swapchain
    // Instant Shaded Class pass: targets, pipeline, layouts, sampler, set and
    // shaders. Everything here is idempotent, so an instant pass that never
    // initialised is a no-op.
    ShutdownInstantPass();

    for (uint32_t s = 0; s < maxFramesInFlight_; ++s)
        DestroyFrameResources(s);

    for (uint32_t i = 0; i < imageCount_; ++i) {
        if (framebuffers_[i] != VK_NULL_HANDLE) {
            vkDestroyFramebuffer(context_.GetDevice(), framebuffers_[i], nullptr);
            framebuffers_[i] = VK_NULL_HANDLE;
        }
        if (depthViews_[i] != VK_NULL_HANDLE) {
            vkDestroyImageView(context_.GetDevice(), depthViews_[i], nullptr);
            depthViews_[i] = VK_NULL_HANDLE;
        }
        if (depthImages_[i].image != VK_NULL_HANDLE) {
            VulkanAllocator::Get().DestroyImage(depthImages_[i]);
            depthImages_[i] = {};
        }
    }
    depthImages_.clear();
    depthViews_.clear();
    framebuffers_.clear();

    for (auto& b : frameUboBuffers_)
        VulkanAllocator::Get().DestroyBuffer(b);
    frameUboBuffers_.clear();

    if (timestampPool_) {
        vkDestroyQueryPool(context_.GetDevice(), timestampPool_, nullptr);
        timestampPool_ = VK_NULL_HANDLE;
    }

    if (frameCmdPool_) {
        vkDestroyCommandPool(context_.GetDevice(), frameCmdPool_, nullptr);
        frameCmdPool_ = VK_NULL_HANDLE;
    }
    commandBuffers_.clear();

    uploadContext_.Shutdown();

    if (descriptorPool_ != VK_NULL_HANDLE) {
        descriptorManager_.DestroyPool(descriptorPool_);
        descriptorPool_ = VK_NULL_HANDLE;
    }
    if (frameSetLayout_ != VK_NULL_HANDLE) {
        descriptorManager_.DestroyLayout(frameSetLayout_);
        frameSetLayout_ = VK_NULL_HANDLE;
    }
    descriptorManager_.Shutdown();

    pipelineManager_.Shutdown();
    shaderManager_.Shutdown();

    renderPass_.Shutdown();
    swapchain_.Shutdown();

    frameManager_.Shutdown();
    VulkanAllocator::Get().Shutdown();
    context_.Shutdown();

    extent_ = {0, 0};
    imageCount_ = 0;
}

bool Renderer::BeginFrame() {
    if (!initialized_) return false;

    const double frameT0 = NowMs();
    // --- fence wait: measured on its own because an UNBOUNDED wait here is
    // the classic "the UI feels slow and nothing looks wrong" cause ---
    const double fenceT0 = NowMs();
    frameManager_.BeginFrame();
    lastTimings_.fenceWaitMs = NowMs() - fenceT0;
    uint32_t slot = frameManager_.GetCurrentFrameIndex();

    // Read the PREVIOUS frame's timestamps for this slot (the fence
    // we just waited guarantees the GPU is done).
    if (enableGpuTimestamps_ && slot < timestampsWritten_.size() && timestampsWritten_[slot]) {
        GpuTimings t{};
        uint64_t data[8] = {0, 0, 0, 0, 0, 0, 0, 0};
        vkGetQueryPoolResults(context_.GetDevice(), timestampPool_,
                                  slot * kTimestampsPerFrame, kTimestampsPerFrame,
                                  sizeof(data), data, sizeof(uint64_t),
                                  VK_QUERY_RESULT_64_BIT);
        t.frameGpuMs = static_cast<double>(data[5] - data[0])
                          * context_.GetReport().timestampPeriodNs / 1e6;
        t.pointGpuMs = static_cast<double>(data[2] - data[1])
                          * context_.GetReport().timestampPeriodNs / 1e6;
        t.surfaceGpuMs = static_cast<double>(data[4] - data[3])
                            * context_.GetReport().timestampPeriodNs / 1e6;
        t.valid = true;
        lastTimings_ = t;
    }

    VkExtent2D capsExtent = extent_;
    auto support = context_.QuerySwapchainSupport();
    if (support.capabilities.currentExtent.width !=
            (std::numeric_limits<uint32_t>::max)())
        capsExtent = support.capabilities.currentExtent;

    if (capsExtent.width == 0 || capsExtent.height == 0) {
        stats_.framesSkipped++;
        return false;
    }

    if (recreateRequested_) {
        if (!RecreateSwapchainInternal()) return false;
        recreateRequested_ = false;
        // RecreateSwapchainInternal() rebuilt the frame-sync vector from
        // scratch (Shutdown + Initialize) and reset the frame index to 0.
        // Both `slot` and any VulkanFrameSync& taken before this point now
        // dangle into the retired generation, so re-resolve them, and re-read
        // the surface extent the new swapchain was actually built with.
        slot = frameManager_.GetCurrentFrameIndex();
        capsExtent = extent_;
    }

    // Resolved AFTER the possible recreate: GetCurrentFrame() returns a
    // reference into frameManager_'s per-slot array, which a swapchain
    // recreation replaces wholesale.
    auto& frame = frameManager_.GetCurrentFrame();

    uint32_t imageIndex = 0;
    const double acquireT0 = NowMs();
    VkResult r = swapchain_.AcquireNextImage(frame.imageAvailable, &imageIndex);
    lastTimings_.acquireMs = NowMs() - acquireT0;
    if (r == VK_ERROR_OUT_OF_DATE_KHR) {
        recreateRequested_ = true;
        frameManager_.EndFrame();
        return false;
    }
    if (r == VK_SUBOPTIMAL_KHR) {
        recreateRequested_ = true;
    }
    if (r != VK_SUCCESS && r != VK_SUBOPTIMAL_KHR) {
        fprintf(stderr, "[Renderer] AcquireNextImage FAILED %d\n", r);
        frameManager_.EndFrame();
        return false;
    }
    frame.imageIndex = imageIndex;
    currentImageIndex_ = imageIndex;

    double aspect = static_cast<double>(capsExtent.width) /
                    static_cast<double>(capsExtent.height);
    const double uboT0 = NowMs();
    camera_.SetAspectRatio(aspect);
    UpdateFrameUbo(slot);
    lastTimings_.uboUpdateMs = NowMs() - uboT0;

    VkCommandBuffer cmd = commandBuffers_[imageIndex];
    const double beginT0 = NowMs();
    vkResetCommandBuffer(cmd, 0);
    VkCommandBufferBeginInfo beginInfo{};
    beginInfo.sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO;
    beginInfo.flags = VK_COMMAND_BUFFER_USAGE_ONE_TIME_SUBMIT_BIT;
    if (vkBeginCommandBuffer(cmd, &beginInfo) != VK_SUCCESS) return false;

    if (enableGpuTimestamps_) {
        // Queries must be reset before each use (VUID-vkCmdWriteTimestamp-None-00830).
        // This must happen outside a render pass, so it goes here before
        // vkCmdBeginRenderPass below.
        vkCmdResetQueryPool(cmd, timestampPool_, slot * kTimestampsPerFrame,
                             kTimestampsPerFrame);
        vkCmdWriteTimestamp(cmd, VK_PIPELINE_STAGE_TOP_OF_PIPE_BIT,
                                timestampPool_, slot * kTimestampsPerFrame + 0);
        if (slot < timestampsWritten_.size()) timestampsWritten_[slot] = true;
    }

    VkRenderPassBeginInfo rp{};
    rp.sType = VK_STRUCTURE_TYPE_RENDER_PASS_BEGIN_INFO;
    rp.renderPass = renderPass_.GetRenderPass();
    rp.framebuffer = framebuffers_[imageIndex];
    rp.renderArea.offset = {0, 0};
    rp.renderArea.extent = capsExtent;
    VkClearValue clearValues[2]{};
    clearValues[0].color.float32[0] = clearColor_[0];
    clearValues[0].color.float32[1] = clearColor_[1];
    clearValues[0].color.float32[2] = clearColor_[2];
    clearValues[0].color.float32[3] = clearColor_[3];
    clearValues[1].depthStencil = {1.0f, 0};
    rp.clearValueCount = 2;
    rp.pClearValues = clearValues;
    vkCmdBeginRenderPass(cmd, &rp, VK_SUBPASS_CONTENTS_INLINE);
    lastTimings_.beginFrameMs = NowMs() - beginT0;
    lastTimings_.frameCpuTotalMs = NowMs() - frameT0;

    lastCpuFrameStartMs_ = NowMs();
    return true;
}

void Renderer::EndFrame() {
    const double endT0 = NowMs();
    auto& frame = frameManager_.GetCurrentFrame();
    uint32_t slot = frameManager_.GetCurrentFrameIndex();
    uint32_t imageIndex = frame.imageIndex;
    VkCommandBuffer cmd = commandBuffers_[imageIndex];

    vkCmdEndRenderPass(cmd);
    if (enableGpuTimestamps_)
        vkCmdWriteTimestamp(cmd, VK_PIPELINE_STAGE_BOTTOM_OF_PIPE_BIT,
                                timestampPool_, slot * kTimestampsPerFrame + 5);

    if (vkEndCommandBuffer(cmd) != VK_SUCCESS) {
        fprintf(stderr, "[Renderer] end cmd FAILED\n");
        return;
    }

    frameManager_.ResetFence();

    // renderFinished is keyed by the ACQUIRED IMAGE, not the frame slot: a
    // per-frame one gets re-signalled while presentation may still be waiting
    // on it for an image that was presented but not re-acquired
    // (VUID-vkQueueSubmit-pSignalSemaphores-00067).
    VkSemaphore renderFinished = frameManager_.GetRenderFinished(imageIndex);

    VkPipelineStageFlags waitStages[] = {VK_PIPELINE_STAGE_COLOR_ATTACHMENT_OUTPUT_BIT};
    VkSubmitInfo si{};
    si.sType = VK_STRUCTURE_TYPE_SUBMIT_INFO;
    si.waitSemaphoreCount = 1;
    si.pWaitSemaphores = &frame.imageAvailable;
    si.pWaitDstStageMask = waitStages;
    si.commandBufferCount = 1;
    si.pCommandBuffers = &cmd;
    si.signalSemaphoreCount = 1;
    si.pSignalSemaphores = &renderFinished;
    // CPU submit cost: measured around vkQueueSubmit only, so it is a real
    // "how long did the CPU spend handing work to the driver" number and can
    // never be confused with the GPU time reported from the timestamp pool.
    const double submitT0 = NowMs();
    vkQueueSubmit(context_.GetGraphicsQueue(), 1, &si, frame.inFlightFence);
    const double submitT1 = NowMs();

    VkPresentInfoKHR pi{};
    pi.sType = VK_STRUCTURE_TYPE_PRESENT_INFO_KHR;
    pi.waitSemaphoreCount = 1;
    pi.pWaitSemaphores = &renderFinished;
    pi.swapchainCount = 1;
    VkSwapchainKHR sw = swapchain_.GetSwapchain();
    pi.pSwapchains = &sw;
    pi.pImageIndices = &imageIndex;
    const double presentT0 = NowMs();
    VkResult pr = vkQueuePresentKHR(context_.GetPresentQueue(), &pi);
    const double presentT1 = NowMs();
    if (pr == VK_ERROR_OUT_OF_DATE_KHR || pr == VK_SUBOPTIMAL_KHR)
        recreateRequested_ = true;

    lastTimings_.cpuSubmitMs = submitT1 - submitT0;
    lastTimings_.presentMs = presentT1 - presentT0;
    lastTimings_.endFrameMs = NowMs() - endT0;
    lastTimings_.frameCpuTotalMs = lastTimings_.endFrameMs + lastTimings_.beginFrameMs +
                                   lastTimings_.fenceWaitMs + lastTimings_.acquireMs +
                                   lastTimings_.uboUpdateMs + lastTimings_.commandRecordMs;

    stats_.framesRendered++;
    frameManager_.EndFrame();
}

void Renderer::WaitIdle() {
    if (initialized_) context_.CoreDevice().WaitIdle();
}

bool Renderer::RecreateSwapchain(uint32_t w, uint32_t h) {
    if (!initialized_) return false;
    recreateRequested_ = false;
    // BUG FOUND THIS ROUND: w/h were accepted but silently DISCARDED - the
    // internal path fell back to cfg_.width/cfg_.height (the size Initialize()
    // was called with, e.g. a 100x30 bootstrap rect) whenever the surface's
    // currentExtent reported the sentinel value, and even when currentExtent
    // WAS usable, cfg_ itself was never updated, so any later code that reads
    // cfg_.width/height directly kept seeing the stale creation-time size.
    // Feed the caller's real, just-measured widget size in as the values to
    // fall back to instead of the stale ones, and keep cfg_ in sync so it
    // never regresses to the bootstrap size again.
    cfg_.width = (w > 0) ? w : cfg_.width;
    cfg_.height = (h > 0) ? h : cfg_.height;
    return RecreateSwapchainInternal();
}

bool Renderer::RecreateSwapchainInternal() {
    // 1) Wait for GPU idle. Every submitted command buffer AND the previous
    //    frame's vkQueuePresentKHR wait on renderFinished must retire before
    //    anything below is destroyed.
    context_.CoreDevice().WaitIdle();

    // [VULKAN SYNC] state of the outgoing generation, logged BEFORE the
    // teardown so a post-mortem shows which objects are being replaced.
    fprintf(stderr, "[VULKAN SYNC]\n");
    {
        uint32_t idx = frameManager_.GetCurrentFrameIndex();
        const VulkanFrameSync& f = frameManager_.GetFrame(idx);
        fprintf(stderr, "  frame index:     %u\n", idx);
        fprintf(stderr, "  imageAvailable:  %p\n", (void*)(uintptr_t)f.imageAvailable);
        fprintf(stderr, "  fence:           %p\n", (void*)(uintptr_t)f.inFlightFence);
        fprintf(stderr, "  renderFinished:  %u x per swapchain image\n",
                frameManager_.GetRenderFinishedCount());
    }

    DestroyCaptureTarget();   // framebuffer/render pass is about to be rebuilt
    // The instant-shaded colour/depth targets alias the OLD extent (and, on a
    // format change, the old render pass), so they go with it. Recreated
    // lazily on the next instant frame by EnsureInstantTargets().
    DestroyInstantTargets();

    VkFormat oldColorFmt = swapchain_.GetImageFormat();
    VkExtent2D oldExtent = extent_;
    uint32_t oldImageCount = imageCount_;

    auto support = context_.QuerySwapchainSupport();
    auto surfFmt = ChooseSurfaceFormat(support.formats);
    VkExtent2D ext = {cfg_.width, cfg_.height};
    if (support.capabilities.currentExtent.width !=
            (std::numeric_limits<uint32_t>::max)()) {
        // The surface usually wins, because for a real window it is the only
        // authority on the drawable size. But a surface that has not been
        // resized/mapped yet (headless or hidden window) can still report the
        // tiny bootstrap rect it was created with, which silently overrides an
        // explicit caller resize. Measured here: cfg_=1400x900 discarded in
        // favour of currentExtent=100x30, leaving every offscreen capture at
        // 100x30 - far too small to resolve a 2.5 px point sprite.
        // So only accept the surface extent when it is at least as large as
        // what was explicitly requested; otherwise keep the request.
        const VkExtent2D cur = support.capabilities.currentExtent;
        if (cur.width >= ext.width && cur.height >= ext.height) {
            ext = cur;
        }
    }

    // SAME-SIZE EARLY-OUT. The Qt geometry mirror and the widget's own
    // resizeEvent both call nkv_resize() with the same dimensions whenever the
    // layout settles, and VTK's camera observer fires Modified events around the
    // same time - so this function was being entered 3-4 times per load with an
    // UNCHANGED extent. Each entry still did the full teardown: destroy
    // framebuffers, depth images, every semaphore, every fence, the command
    // pool and the swapchain, then rebuild them all. Doing that repeatedly while
    // frames referencing the retired objects are still in flight is what
    // produced the hard fault (NVIDIA "Unable to recover from a kernel
    // exception", Error code 3 subcode 7) when loading 123.las with
    // NAKSHA_VULKAN_MAIN_VIEWPORT=1. Nothing changes when the size is the same,
    // so return before touching anything.
    //
    // Compared against cfg_ (what was ASKED for), never against extent_: a
    // swapchain that clamped the previous request has already written the
    // clamped size into extent_, so comparing against it would wrongly report
    // "already correct" and strand the swapchain at the small size forever once
    // the window is finally shown at full size.
    if (cfg_.width == ext.width && cfg_.height == ext.height &&
        extent_.width == ext.width && extent_.height == ext.height &&
        !recreateRequested_ && framebuffers_.size() == imageCount_ &&
        !framebuffers_.empty() && framebuffers_[0] != VK_NULL_HANDLE) {
        fprintf(stderr, "[VULKAN RESIZE] already %ux%u - no-op, skipped\n",
                ext.width, ext.height);
        return true;
    }

    fprintf(stderr, "[VULKAN RESIZE]\n");
    fprintf(stderr, "  old extent: %ux%u\n", oldExtent.width, oldExtent.height);
    fprintf(stderr, "  new extent: %ux%u (cfg_=%ux%u, surface currentExtent=%ux%u)\n",
            ext.width, ext.height, cfg_.width, cfg_.height,
            support.capabilities.currentExtent.width, support.capabilities.currentExtent.height);

    bool formatChanged = (surfFmt.format != oldColorFmt);

    // A render pass is format-specific: if the surface handed back a different
    // colour format the private instant pass has to be rebuilt too. This is the
    // safe point to do it - WaitIdle already ran above, so nothing in flight
    // still references the old pipeline or render pass.
    if (formatChanged && instantPassReady_) {
        const bool wasActive = instantActive_;
        ShutdownInstantPass();
        if (!InitializeInstantPass()) ShutdownInstantPass();
        instantActive_ = wasActive;
        fprintf(stderr, "[INSTANT SHADED] rebuilt for colour format change %d -> %d\n",
                static_cast<int>(oldColorFmt), static_cast<int>(surfFmt.format));
    }

    // 4a) Destroy framebuffers, depth views and depth images. The depth
    // images were previously only resized-over here, leaking one VMA
    // allocation per resize; they alias the OLD extent so they must go.
    for (uint32_t i = 0; i < oldImageCount; ++i) {
        if (i < framebuffers_.size() && framebuffers_[i] != VK_NULL_HANDLE) {
            vkDestroyFramebuffer(context_.GetDevice(), framebuffers_[i], nullptr);
            framebuffers_[i] = VK_NULL_HANDLE;
        }
        if (i < depthViews_.size() && depthViews_[i] != VK_NULL_HANDLE) {
            vkDestroyImageView(context_.GetDevice(), depthViews_[i], nullptr);
            depthViews_[i] = VK_NULL_HANDLE;
        }
        if (i < depthImages_.size() && depthImages_[i].image != VK_NULL_HANDLE) {
            VulkanAllocator::Get().DestroyImage(depthImages_[i]);
            depthImages_[i] = {};
        }
    }

    // 4b) Rebuild the swapchain. VulkanSwapchain::Recreate() passes the old
    // handle as VkSwapchainCreateInfoKHR::oldSwapchain and destroys it only
    // after the replacement exists (7). On failure the previous swapchain is
    // restored and we rebuild against its dimensions instead of going black.
    if (!swapchain_.Recreate(ext.width, ext.height, support)) {
        ext = swapchain_.GetExtent();
        fprintf(stderr, "[VULKAN RESIZE] swapchain recreate FAILED - "
                        "falling back to %ux%u\n", ext.width, ext.height);
    } else {
        // The swapchain may clamp the request to its own min/maxImageExtent
        // (a HIDDEN or not-yet-mapped window reports a tiny currentExtent, and
        // the swapchain can only be as large as that surface). The framebuffers
        // and depth images below MUST match the swapchain images, so adopt its
        // size for THIS rebuild.
        //
        // This is deliberately not sticky: a clamped rebuild leaves
        // extent_ == the small size, so the NEXT request at the real size is
        // not a same-size no-op and is honoured then (that is what lets the
        // window reach 1400x900 once it is actually shown). Pinning the clamp
        // would strand every later capture at 100x30.
        const VkExtent2D actual = swapchain_.GetExtent();
        if (actual.width != ext.width || actual.height != ext.height) {
            fprintf(stderr, "[VULKAN RESIZE] swapchain clamped %ux%u -> %ux%u; "
                            "matching the attachments to the swapchain\n",
                    ext.width, ext.height, actual.width, actual.height);
            ext = actual;
        }
    }
    imageCount_ = swapchain_.GetImageCount();

    // 4c) Depth + framebuffers for the new extent.
    depthImages_.resize(imageCount_);
    depthViews_.resize(imageCount_);
    framebuffers_.resize(imageCount_);
    for (uint32_t i = 0; i < imageCount_; ++i) {
        // TRANSFER_SRC is required by nkv_capture_depth, and BOTH depth-image
        // creation sites must agree on it. CreateFrameResources (first setup)
        // and this recreate path previously differed; the mismatch is a real
        // GPU fault after a swapchain resize - observed as an NVIDIA "Unable to
        // recover from a kernel exception" (Error code 3, subcode 7) on load
        // with NAKSHA_VULKAN_MAIN_VIEWPORT=1, which resizes the swapchain while
        // the 2.96M-point cloud is resident.
        depthImages_[i] = VulkanAllocator::Get().CreateImage(
            depthFormat_, VkExtent2D{ext.width, ext.height},
            VK_IMAGE_USAGE_DEPTH_STENCIL_ATTACHMENT_BIT | VK_IMAGE_USAGE_TRANSFER_SRC_BIT,
            VMA_MEMORY_USAGE_GPU_ONLY);
        VkImageViewCreateInfo vi{VK_STRUCTURE_TYPE_IMAGE_VIEW_CREATE_INFO};
        vi.image = depthImages_[i].image;
        vi.viewType = VK_IMAGE_VIEW_TYPE_2D;
        vi.format = depthFormat_;
        vi.subresourceRange = {VK_IMAGE_ASPECT_DEPTH_BIT, 0, 1, 0, 1};
        vkCreateImageView(context_.GetDevice(), &vi, nullptr, &depthViews_[i]);
        VkFramebufferCreateInfo fbi{VK_STRUCTURE_TYPE_FRAMEBUFFER_CREATE_INFO};
        fbi.renderPass = renderPass_.GetRenderPass();
        VkImageView atts[2] = {swapchain_.GetImageView(i), depthViews_[i]};
        fbi.attachmentCount = 2;
        fbi.pAttachments = atts;
        fbi.width = ext.width;
        fbi.height = ext.height;
        fbi.layers = 1;
        vkCreateFramebuffer(context_.GetDevice(), &fbi, nullptr, &framebuffers_[i]);
    }

    // 5) Command buffers are allocated ONE PER SWAPCHAIN IMAGE (BeginFrame
    // indexes commandBuffers_[imageIndex]). If the image count changed the
    // old set is the wrong size and must be re-allocated from the pool.
    if (commandBuffers_.size() != imageCount_) {
        if (!commandBuffers_.empty()) {
            vkFreeCommandBuffers(context_.GetDevice(), frameCmdPool_,
                                 static_cast<uint32_t>(commandBuffers_.size()),
                                 commandBuffers_.data());
        }
        commandBuffers_.assign(imageCount_, VK_NULL_HANDLE);
        VkCommandBufferAllocateInfo ai{VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO};
        ai.commandPool = frameCmdPool_;
        ai.level = VK_COMMAND_BUFFER_LEVEL_PRIMARY;
        ai.commandBufferCount = imageCount_;
        if (vkAllocateCommandBuffers(context_.GetDevice(), &ai, commandBuffers_.data()) != VK_SUCCESS) {
            fprintf(stderr, "[VULKAN RESIZE] command buffer reallocation FAILED\n");
            return false;
        }
        fprintf(stderr, "[VULKAN RESIZE] command buffers reallocated: %u\n", imageCount_);
    }

    if (formatChanged) {
        renderPass_.Shutdown();
        RenderPassConfig rpc;
        rpc.colorFormat = swapchain_.GetImageFormat();
        rpc.depthFormat = depthFormat_;
        rpc.loadColorClear = true;
        rpc.storeColor = true;
        rpc.loadDepthClear = true;
        rpc.depthLoadOp = VK_ATTACHMENT_LOAD_OP_CLEAR;
        renderPass_.Initialize(context_.GetDevice(), rpc);
        for (auto& [id, cb] : rebuildCallbacks_) cb();
    }
    stats_.swapchainRecreates++;

    // 6) Recreate frame synchronization. The old semaphores were consumed by
    // the RETIRED swapchain's acquire/present, so re-using them on the new
    // one is what produced
    //   "vkQueueSubmit(): signal semaphore may still be in use by VkSwapchainKHR".
    // Fresh objects (fences created SIGNALED, frame index back to 0) give the
    // first frame after the resize a clean, self-consistent sync set.
    if (!frameManager_.Initialize(context_.GetDevice(), maxFramesInFlight_, imageCount_)) {
        fprintf(stderr, "[VULKAN RESIZE] frame sync recreation FAILED\n");
        return false;
    }

    // 7) Timestamp query pool: the previous generation's results are stale
    // and the queries must be back in the reset state before the first
    // vkCmdWriteTimestamp after the resize, otherwise validation reports
    // "vkCmdWriteTimestamp(): query N not reset".
    if (timestampPool_ && enableGpuTimestamps_) {
        vkResetQueryPool(context_.GetDevice(), timestampPool_,
                         0, kTimestampsPerFrame * maxFramesInFlight_);
    }
    timestampsWritten_.assign(maxFramesInFlight_, false);

    extent_ = ext;

    // [VULKAN SYNC RESET] the generation the next frame will use.
    fprintf(stderr, "[VULKAN SYNC RESET]\n");
    for (uint32_t s = 0; s < frameManager_.GetMaxFramesInFlight(); ++s) {
        const VulkanFrameSync& f = frameManager_.GetFrame(s);
        fprintf(stderr, "  slot %u: imageAvailable=%p fence=%p\n",
                s, (void*)(uintptr_t)f.imageAvailable, (void*)(uintptr_t)f.inFlightFence);
    }
    for (uint32_t i = 0; i < frameManager_.GetRenderFinishedCount(); ++i) {
        fprintf(stderr, "  image %u: renderFinished=%p\n", i,
                (void*)(uintptr_t)frameManager_.GetRenderFinished(i));
    }
    fprintf(stderr, "  new semaphores: %u x imageAvailable (per frame slot) + "
                    "%u x renderFinished (per swapchain image)\n",
            frameManager_.GetMaxFramesInFlight(), frameManager_.GetRenderFinishedCount());
    fprintf(stderr, "  new fences:     %u x inFlight (signaled)\n",
            frameManager_.GetMaxFramesInFlight());
    fprintf(stderr, "  frame index reset: %u\n", frameManager_.GetCurrentFrameIndex());

    return true;
}

namespace {

// Linear interpolation across a small RGB stop table, used only for the
// built-in default tables (Python normally overwrites them with the app's own
// LUTs, which are sampled from gui/pointcloud_display.py so the Vulkan view
// matches VTK exactly rather than approximating it).
void SampleStops(float* dst, const float* stops, int stopCount, float t) {
    if (stopCount <= 1) {
        dst[0] = stops[0]; dst[1] = stops[1]; dst[2] = stops[2]; dst[3] = 1.0f;
        return;
    }
    float scaled = t * static_cast<float>(stopCount - 1);
    int index = static_cast<int>(scaled);
    if (index < 0) index = 0;
    if (index > stopCount - 2) index = stopCount - 2;
    float f = scaled - static_cast<float>(index);
    const float* a = stops + index * 3;
    const float* b = stops + (index + 1) * 3;
    dst[0] = a[0] + (b[0] - a[0]) * f;
    dst[1] = a[1] + (b[1] - a[1]) * f;
    dst[2] = a[2] + (b[2] - a[2]) * f;
    dst[3] = 1.0f;
}

// kind: 0 = ASPRS-style class palette, 1 = elevation rainbow, 2 = intensity grey.
void FillDefaultLut(float* lut, int kind) {
    static const float kClass[][3] = {
        {0.35f, 0.35f, 0.35f},  //   0 never classified
        {0.60f, 0.60f, 0.60f},  //   1 unclassified
        {0.55f, 0.42f, 0.25f},  //   2 ground
        {0.20f, 0.65f, 0.25f},  //   3 low vegetation
        {0.10f, 0.50f, 0.20f},  //   4 medium vegetation
        {0.00f, 0.35f, 0.15f},  //   5 high vegetation
        {0.80f, 0.40f, 0.30f},  //   6 building
        {0.95f, 0.85f, 0.20f},  //   7 point of interest
        {0.15f, 0.35f, 0.75f},  //   8 water
        {0.70f, 0.70f, 0.80f},  //   9 bridge deck
        {0.85f, 0.75f, 0.85f},  //  10 power line
        {0.50f, 0.50f, 0.50f},  //  11 rail
    };
    // Same five stops as _nakshatech_rainbow_5color: blue, cyan, green, yellow, red.
    static const float kElev[] = {
        0.00f, 0.00f, 0.75f,
        0.00f, 0.80f, 0.95f,
        0.10f, 0.75f, 0.20f,
        0.98f, 0.90f, 0.10f,
        0.85f, 0.10f, 0.10f,
    };
    for (uint32_t i = 0; i < 256; ++i) {
        float t = static_cast<float>(i) / 255.0f;
        if (kind == 0) {
            int c = static_cast<int>(i);
            const float* rgb = kClass[c < 12 ? c : 11];
            lut[i * 4 + 0] = rgb[0];
            lut[i * 4 + 1] = rgb[1];
            lut[i * 4 + 2] = rgb[2];
            lut[i * 4 + 3] = 1.0f;
        } else if (kind == 1) {
            SampleStops(lut + i * 4, kElev, 5, t);
        } else {
            lut[i * 4 + 0] = t;
            lut[i * 4 + 1] = t;
            lut[i * 4 + 2] = t;
            lut[i * 4 + 3] = 1.0f;
        }
    }
}

void StoreLut(float* dst, const uint8_t* rgb) {
    if (!rgb) return;   // keep the current table
    const float scale = 1.0f / 255.0f;
    for (uint32_t i = 0; i < 256; ++i) {
        dst[i * 4 + 0] = static_cast<float>(rgb[i * 3 + 0]) * scale;
        dst[i * 4 + 1] = static_cast<float>(rgb[i * 3 + 1]) * scale;
        dst[i * 4 + 2] = static_cast<float>(rgb[i * 3 + 2]) * scale;
        dst[i * 4 + 3] = 1.0f;
    }
}

}  // namespace

void Renderer::SetPointLUTs(const uint8_t* classRgb, const uint8_t* elevationRgb,
                            const uint8_t* intensityRgb) {
    StoreLut(lutClass_, classRgb);
    StoreLut(lutElevation_, elevationRgb);
    StoreLut(lutIntensity_, intensityRgb);
    // Proof that a palette/visibility/weight edit re-tinted the GPU WITHOUT
    // touching the point buffer: this counter must move while
    // PointCloudRenderer::GetPositionUploadCount() stays put.
    if (classRgb || elevationRgb || intensityRgb) {
        lutUpdates_++;
    }
    // StoreLut() unconditionally writes alpha = 1, so a colour push would
    // otherwise silently re-show every class the user had unchecked. Re-fold
    // the visibility table into the alpha after every colour push; the two
    // entry points are then order-independent.
    for (uint32_t i = 0; i < 256; ++i)
        lutClass_[i * 4 + 3] = classVisible_[i] ? 1.0f : 0.0f;
}

// ---------------------------------------------------------------------------
// Class visibility - 256 bytes (see Renderer.hpp). Never touches a point
// buffer: only the palette alpha inside the frame UBO changes.
// ---------------------------------------------------------------------------
void Renderer::SetClassVisibility(const uint8_t* visible256) {
    if (!visible256) return;
    std::memcpy(classVisible_, visible256, sizeof(classVisible_));
    for (uint32_t i = 0; i < 256; ++i)
        lutClass_[i * 4 + 3] = classVisible_[i] ? 1.0f : 0.0f;
    // Counted on its OWN counter, not lutUpdates_: lutUpdates_ means "a colour
    // table was replaced" and is what a palette-parity test reads. Visibility
    // has its own instrument (nkv_get_class_visibility_update_count), and
    // double-counting here would make a palette test report a change it never
    // saw. Neither ever touches a point buffer.
    classVisibilityUpdates_++;
}

// ---------------------------------------------------------------------------
// Instant Shaded Class pass
// ---------------------------------------------------------------------------

namespace {

// Inverts a column-major float32 4x4 (exactly the layout GLSL `mat4` and
// FrameUbo::mvp use) with Gauss-Jordan + partial pivoting. Written this way on
// purpose: the cofactor formulas are easy to transpose by accident, and a
// transposed inverse is invisible in a still image but quietly wrong for every
// reconstructed normal it produces.
bool InvertColumnMajor4(const float in[16], float out[16]) {
    double a[4][8];
    for (int r = 0; r < 4; ++r) {
        for (int c = 0; c < 4; ++c) {
            a[r][c] = static_cast<double>(in[c * 4 + r]);   // element (r, c)
            a[r][4 + c] = (r == c) ? 1.0 : 0.0;
        }
    }
    for (int col = 0; col < 4; ++col) {
        int piv = col;
        for (int r = col + 1; r < 4; ++r)
            if (std::fabs(a[r][col]) > std::fabs(a[piv][col])) piv = r;
        if (std::fabs(a[piv][col]) < 1e-20) return false;
        if (piv != col)
            for (int c = 0; c < 8; ++c) std::swap(a[piv][c], a[col][c]);
        const double d = a[col][col];
        for (int c = 0; c < 8; ++c) a[col][c] /= d;
        for (int r = 0; r < 4; ++r) {
            if (r == col) continue;
            const double f = a[r][col];
            if (f == 0.0) continue;
            for (int c = 0; c < 8; ++c) a[r][c] -= f * a[col][c];
        }
    }
    for (int r = 0; r < 4; ++r)
        for (int c = 0; c < 4; ++c)
            out[c * 4 + r] = static_cast<float>(a[r][4 + c]);
    return true;
}

constexpr float kDegToRad = 3.14159265358979323846f / 180.0f;

}  // namespace

void Renderer::SetInstantShadedActive(bool on) {
    if (instantActive_ == on) return;
    instantActive_ = on;
    fprintf(stderr, "[INSTANT SHADED] %s (mode switch is a float write; no "
                    "upload, no mesh, no vkDeviceWaitIdle)\n",
            on ? "ENABLED" : "DISABLED");
}

void Renderer::SetInstantShading(float azimuthDeg, float elevationDeg, float ambient) {
    instantAzimuthDeg_ = azimuthDeg;
    // Keep elevation in the 0..90 band _compute_shading() assumes: below 0 the
    // light would come from under the terrain, above 90 from inside it, and
    // neither is expressible on the legacy path either.
    instantElevationDeg_ = std::max(0.0f, std::min(90.0f, elevationDeg));
    instantAmbient_ = std::max(0.0f, std::min(1.0f, ambient));
}

bool Renderer::InitializeInstantPass() {
    const VkDevice dev = context_.GetDevice();

    const std::string vsPath = cfg_.shaderDirectory + "/instant.vert.spv";
    const std::string fsPath = cfg_.shaderDirectory + "/instant.frag.spv";
    instantVs_ = shaderManager_.LoadSPIRV(vsPath, VK_SHADER_STAGE_VERTEX_BIT);
    instantFs_ = shaderManager_.LoadSPIRV(fsPath, VK_SHADER_STAGE_FRAGMENT_BIT);
    if (instantVs_.module == VK_NULL_HANDLE || instantFs_.module == VK_NULL_HANDLE) {
        fprintf(stderr, "[INSTANT SHADED] pass unavailable: missing %s\n",
                vsPath.c_str());
        return false;
    }

    // Same colour/depth FORMAT as renderPass_ (so the EXISTING point pipeline
    // stays compatible with this render pass and is reused verbatim - there is
    // deliberately no second point pipeline to keep in sync), but private final
    // layouts and a STOREd depth, because this pass's output is sampled.
    RenderPassConfig irc;
    irc.colorFormat = swapchain_.GetImageFormat();
    irc.depthFormat = depthFormat_;
    irc.loadColorClear = true;
    irc.storeColor = true;
    irc.loadDepthClear = true;
    irc.depthLoadOp = VK_ATTACHMENT_LOAD_OP_CLEAR;
    irc.storeDepth = true;
    irc.colorFinalLayout = VK_IMAGE_LAYOUT_COLOR_ATTACHMENT_OPTIMAL;
    irc.depthFinalLayout = VK_IMAGE_LAYOUT_DEPTH_STENCIL_ATTACHMENT_OPTIMAL;
    // Attachment 1 carries the STORED oct16x2 normal of the splat that won each
    // pixel. R16G16_SNORM is exactly the 4 bytes/point we store, so the splat
    // writes the representation through unchanged and the lighting pass decodes
    // it per pixel - no expansion, no filtering, no cross-surface bleeding.
    irc.color2Format = VK_FORMAT_R16G16_SNORM;
    irc.color2FinalLayout = VK_IMAGE_LAYOUT_COLOR_ATTACHMENT_OPTIMAL;
    if (!instantRenderPass_.Initialize(dev, irc)) {
        fprintf(stderr, "[INSTANT SHADED] render pass creation FAILED\n");
        return false;
    }
    instantPassColorFormat_ = irc.colorFormat;

    // set 1 = the two sampled targets. set 0 stays the shared Frame UBO.
    {
        std::vector<DescriptorBinding> bindings;
        bindings.push_back({0, VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER, 1,
                            VK_SHADER_STAGE_FRAGMENT_BIT});
        bindings.push_back({1, VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER, 1,
                            VK_SHADER_STAGE_FRAGMENT_BIT});
        // binding 2: the STORED oct16x2 normal target (production normal source).
        bindings.push_back({2, VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER, 1,
                            VK_SHADER_STAGE_FRAGMENT_BIT});
        instantSet1Layout_ = descriptorManager_.CreateLayout(bindings);
        if (instantSet1Layout_ == VK_NULL_HANDLE) return false;
    }
    {
        std::vector<VkDescriptorPoolSize> sizes;
        sizes.push_back({VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER, 6});
        instantPool_ = descriptorManager_.CreatePool(sizes, 1);
        if (instantPool_ == VK_NULL_HANDLE) return false;
        instantSet1_ = descriptorManager_.AllocateSet(instantPool_, instantSet1Layout_);
        if (instantSet1_ == VK_NULL_HANDLE) return false;
    }
    {
        // NEAREST for BOTH targets: the lighting pass must see exact texels -
        // linear-filtering depth would average two surfaces into a position
        // that exists nowhere, and filtering the colour would smear class
        // boundaries the crisp path is specifically there to keep crisp.
        VkSamplerCreateInfo si{VK_STRUCTURE_TYPE_SAMPLER_CREATE_INFO};
        si.magFilter = VK_FILTER_NEAREST;
        si.minFilter = VK_FILTER_NEAREST;
        si.mipmapMode = VK_SAMPLER_MIPMAP_MODE_NEAREST;
        si.addressModeU = VK_SAMPLER_ADDRESS_MODE_CLAMP_TO_EDGE;
        si.addressModeV = VK_SAMPLER_ADDRESS_MODE_CLAMP_TO_EDGE;
        si.addressModeW = VK_SAMPLER_ADDRESS_MODE_CLAMP_TO_EDGE;
        si.minLod = 0.0f;
        si.maxLod = 0.0f;
        if (vkCreateSampler(dev, &si, nullptr, &instantSampler_) != VK_SUCCESS)
            return false;
    }

    // push constants: 4 x vec4 + mat4 = 128 bytes, exactly the guaranteed
    // minimum minPushConstantsSize, fragment stage only (the fullscreen vertex
    // shader derives everything from gl_VertexIndex and needs no constants).
    VkPushConstantRange pcr{};
    pcr.stageFlags = VK_SHADER_STAGE_FRAGMENT_BIT;
    pcr.offset = 0;
    pcr.size = 16 + 16 + 16 + 16 + 64;
    instantPipelineLayout_ = pipelineManager_.CreatePipelineLayout(
        {frameSetLayout_, instantSet1Layout_}, {pcr});
    if (instantPipelineLayout_ == VK_NULL_HANDLE) return false;

    PipelineConfig cfg;
    cfg.SetDefaults();
    // SetDefaults() installs the 5-stream POINT vertex layout. This pipeline has
    // NO vertex input (gl_VertexIndex generates the triangle), so both counts
    // must go to zero - a declared-but-never-bound vertex binding is a
    // validation error on every draw.
    cfg.vertexBindings.clear();
    cfg.vertexAttributes.clear();
    cfg.vertexInput.vertexBindingDescriptionCount = 0;
    cfg.vertexInput.pVertexBindingDescriptions = nullptr;
    cfg.vertexInput.vertexAttributeDescriptionCount = 0;
    cfg.vertexInput.pVertexAttributeDescriptions = nullptr;
    cfg.inputAssembly.topology = VK_PRIMITIVE_TOPOLOGY_TRIANGLE_LIST;
    cfg.rasterizer.cullMode = VK_CULL_MODE_NONE;
    cfg.colorBlendAttachment.blendEnable = VK_FALSE;
    // The fullscreen triangle covers every pixel and the target is lit exactly
    // once, so there is nothing for the depth test to resolve.
    cfg.depthStencil.depthTestEnable = VK_FALSE;
    cfg.depthStencil.depthWriteEnable = VK_FALSE;
    instantPipeline_ = pipelineManager_.CreateGraphicsPipeline(
        instantPipelineLayout_, {instantVs_, instantFs_}, cfg,
        renderPass_.GetRenderPass());
    if (instantPipeline_ == VK_NULL_HANDLE) {
        fprintf(stderr, "[INSTANT SHADED] pipeline creation FAILED\n");
        return false;
    }

    instantPassReady_ = true;
    fprintf(stderr, "[INSTANT SHADED] pass ready (offscreen targets are created "
                    "on first use at %ux%u)\n", extent_.width, extent_.height);
    return true;
}

void Renderer::DestroyInstantTargets() {
    if (instantFramebuffer_ == VK_NULL_HANDLE &&
        instantColorImage_.image == VK_NULL_HANDLE &&
        instantDepthImage_.image == VK_NULL_HANDLE) {
        instantTargetsReady_ = false;
        return;
    }
    const VkDevice dev = context_.GetDevice();
    if (instantFramebuffer_ != VK_NULL_HANDLE) {
        vkDestroyFramebuffer(dev, instantFramebuffer_, nullptr);
        instantFramebuffer_ = VK_NULL_HANDLE;
    }
    // DestroyImage() also drops the view parked in .imageView.
    if (instantColorImage_.image != VK_NULL_HANDLE)
        VulkanAllocator::Get().DestroyImage(instantColorImage_);
    if (instantDepthImage_.image != VK_NULL_HANDLE)
        VulkanAllocator::Get().DestroyImage(instantDepthImage_);
    if (instantNormalImage_.image != VK_NULL_HANDLE)
        VulkanAllocator::Get().DestroyImage(instantNormalImage_);
    instantColorImage_ = {};
    instantDepthImage_ = {};
    instantNormalImage_ = {};
    instantTargetsReady_ = false;
}

bool Renderer::EnsureInstantTargets() {
    if (!instantPassReady_) return false;
    if (extent_.width == 0 || extent_.height == 0) return false;
    // A format change is handled at a safe point (RecreateSwapchainInternal,
    // after its WaitIdle). Reaching here with a mismatch means it was not -
    // refuse rather than create a framebuffer that does not match its render
    // pass, which is a validation error on every frame.
    if (instantPassColorFormat_ != swapchain_.GetImageFormat()) return false;
    if (instantTargetsReady_ && instantFramebuffer_ != VK_NULL_HANDLE &&
        instantColorImage_.extent.width == extent_.width &&
        instantColorImage_.extent.height == extent_.height &&
        instantColorImage_.format == swapchain_.GetImageFormat() &&
        instantDepthImage_.format == depthFormat_) {
        return true;
    }

    DestroyInstantTargets();
    const VkDevice dev = context_.GetDevice();

    instantColorImage_ = VulkanAllocator::Get().CreateImage(
        swapchain_.GetImageFormat(), extent_,
        VK_IMAGE_USAGE_COLOR_ATTACHMENT_BIT | VK_IMAGE_USAGE_SAMPLED_BIT,
        VMA_MEMORY_USAGE_GPU_ONLY);
    if (instantColorImage_.image == VK_NULL_HANDLE) return false;

    instantDepthImage_ = VulkanAllocator::Get().CreateImage(
        depthFormat_, extent_,
        VK_IMAGE_USAGE_DEPTH_STENCIL_ATTACHMENT_BIT | VK_IMAGE_USAGE_SAMPLED_BIT,
        VMA_MEMORY_USAGE_GPU_ONLY);
    if (instantDepthImage_.image == VK_NULL_HANDLE) {
        fprintf(stderr, "[INSTANT SHADED] depth target allocation FAILED\n");
        DestroyInstantTargets();
        return false;
    }

    // Attachment 1: the per-splat STORED oct16x2 normal.
    instantNormalImage_ = VulkanAllocator::Get().CreateImage(
        VK_FORMAT_R16G16_SNORM, extent_,
        VK_IMAGE_USAGE_COLOR_ATTACHMENT_BIT | VK_IMAGE_USAGE_SAMPLED_BIT,
        VMA_MEMORY_USAGE_GPU_ONLY);
    if (instantNormalImage_.image == VK_NULL_HANDLE) {
        fprintf(stderr, "[INSTANT SHADED] normal target allocation FAILED\n");
        DestroyInstantTargets();
        return false;
    }

    VkImageViewCreateInfo vi{VK_STRUCTURE_TYPE_IMAGE_VIEW_CREATE_INFO};
    vi.viewType = VK_IMAGE_VIEW_TYPE_2D;
    vi.image = instantColorImage_.image;
    vi.format = instantColorImage_.format;
    vi.subresourceRange = {VK_IMAGE_ASPECT_COLOR_BIT, 0, 1, 0, 1};
    if (vkCreateImageView(dev, &vi, nullptr, &instantColorImage_.imageView) != VK_SUCCESS) {
        DestroyInstantTargets();
        return false;
    }
    vi.image = instantDepthImage_.image;
    vi.format = instantDepthImage_.format;
    vi.subresourceRange = {VK_IMAGE_ASPECT_DEPTH_BIT, 0, 1, 0, 1};
    if (vkCreateImageView(dev, &vi, nullptr, &instantDepthImage_.imageView) != VK_SUCCESS) {
        DestroyInstantTargets();
        return false;
    }
    vi.image = instantNormalImage_.image;
    vi.format = instantNormalImage_.format;
    vi.subresourceRange = {VK_IMAGE_ASPECT_COLOR_BIT, 0, 1, 0, 1};
    if (vkCreateImageView(dev, &vi, nullptr, &instantNormalImage_.imageView) != VK_SUCCESS) {
        DestroyInstantTargets();
        return false;
    }

    VkFramebufferCreateInfo fbi{VK_STRUCTURE_TYPE_FRAMEBUFFER_CREATE_INFO};
    fbi.renderPass = instantRenderPass_.GetRenderPass();
    // Order must match the instant render pass: 0 colour, 1 normal, 2 depth.
    VkImageView atts[3] = {instantColorImage_.imageView,
                           instantNormalImage_.imageView,
                           instantDepthImage_.imageView};
    fbi.attachmentCount = 3;
    fbi.pAttachments = atts;
    fbi.width = extent_.width;
    fbi.height = extent_.height;
    fbi.layers = 1;
    if (vkCreateFramebuffer(dev, &fbi, nullptr, &instantFramebuffer_) != VK_SUCCESS) {
        fprintf(stderr, "[INSTANT SHADED] framebuffer creation FAILED\n");
        DestroyInstantTargets();
        return false;
    }

    // The set is allocated once and only RE-POINTED when the images change, so
    // a window resize is two descriptor writes - no reallocation, no churn.
    descriptorManager_.UpdateImage(instantSet1_, 0,
                                   instantColorImage_.imageView, instantSampler_);
    descriptorManager_.UpdateImage(instantSet1_, 1,
                                   instantDepthImage_.imageView, instantSampler_);
    descriptorManager_.UpdateImage(instantSet1_, 2,
                                   instantNormalImage_.imageView, instantSampler_);

    instantTargetsReady_ = true;
    instantTargetRebuilds_++;
    fprintf(stderr, "[INSTANT SHADED] targets %ux%u (rebuild #%llu)\n",
            extent_.width, extent_.height,
            static_cast<unsigned long long>(instantTargetRebuilds_));
    return true;
}

void Renderer::ShutdownInstantPass() {
    DestroyInstantTargets();
    const VkDevice dev = context_.GetDevice();
    if (instantPipeline_ != VK_NULL_HANDLE) {
        pipelineManager_.DestroyPipeline(instantPipeline_);
        instantPipeline_ = VK_NULL_HANDLE;
    }
    if (instantPipelineLayout_ != VK_NULL_HANDLE) {
        pipelineManager_.DestroyPipelineLayout(instantPipelineLayout_);
        instantPipelineLayout_ = VK_NULL_HANDLE;
    }
    if (instantSampler_ != VK_NULL_HANDLE) {
        vkDestroySampler(dev, instantSampler_, nullptr);
        instantSampler_ = VK_NULL_HANDLE;
    }
    if (instantSet1_ != VK_NULL_HANDLE) {
        descriptorManager_.FreeSets(instantPool_, {instantSet1_});
        instantSet1_ = VK_NULL_HANDLE;
    }
    if (instantPool_ != VK_NULL_HANDLE) {
        descriptorManager_.DestroyPool(instantPool_);
        instantPool_ = VK_NULL_HANDLE;
    }
    if (instantSet1Layout_ != VK_NULL_HANDLE) {
        descriptorManager_.DestroyLayout(instantSet1Layout_);
        instantSet1Layout_ = VK_NULL_HANDLE;
    }
    if (instantVs_.module != VK_NULL_HANDLE) {
        shaderManager_.DestroyShaderModule(instantVs_.module);
        instantVs_ = {};
    }
    if (instantFs_.module != VK_NULL_HANDLE) {
        shaderManager_.DestroyShaderModule(instantFs_.module);
        instantFs_ = {};
    }
    instantRenderPass_.Shutdown();
    instantPassReady_ = false;
    instantPassColorFormat_ = VK_FORMAT_UNDEFINED;
    instantActive_ = false;
}

void Renderer::RecordInstantShadedPass(VkCommandBuffer cmd, uint32_t frameSlot,
                                       PointCloudRenderer& pointCloud) {
    if (!initialized_ || !instantPassReady_) return;
    if (!EnsureInstantTargets()) return;

    const VkExtent2D ext = extent_;

    // ---- 1) Close the render pass BeginFrame() opened. Nothing has been
    // drawn into it yet, so this only costs a clear we are about to redo.
    vkCmdEndRenderPass(cmd);

    // ---- 2) SPLAT PASS: the resident points into the offscreen target -----
    VkRenderPassBeginInfo rb{};
    rb.sType = VK_STRUCTURE_TYPE_RENDER_PASS_BEGIN_INFO;
    rb.renderPass = instantRenderPass_.GetRenderPass();
    rb.framebuffer = instantFramebuffer_;
    rb.renderArea.offset = {0, 0};
    rb.renderArea.extent = ext;
    // THREE clear values: the instant pass has three attachments (0 colour,
    // 1 stored oct16 normal, 2 depth) and all three are CLEAR. pClearValues is
    // indexed by attachment number, so passing only two makes the driver read a
    // third one off the end of the array - which is what the first validation
    // run reported as a depth clear of 2.2e22.
    VkClearValue clears[3]{};
    clears[0].color.float32[0] = clearColor_[0];
    clears[0].color.float32[1] = clearColor_[1];
    clears[0].color.float32[2] = clearColor_[2];
    clears[0].color.float32[3] = clearColor_[3];
    // The normal attachment clears to oct (0,0) = +Z (flat, up-facing), so a
    // pixel covered by depth but missing a normal reads as flat ground.
    clears[1].color.float32[0] = 0.0f;
    clears[1].color.float32[1] = 0.0f;
    clears[1].color.float32[2] = 0.0f;
    clears[1].color.float32[3] = 0.0f;
    clears[2].depthStencil = {1.0f, 0};
    rb.clearValueCount = 3;
    rb.pClearValues = clears;
    vkCmdBeginRenderPass(cmd, &rb, VK_SUBPASS_CONTENTS_INLINE);
    // The SAME PointCloudRenderer::Record() every other point mode uses. The
    // only differences are which framebuffer is bound and the display-mode
    // float in its push constants (mode 4): this is what makes the switch a
    // state change instead of an upload.
    pointCloud.RecordSplat(cmd, frameSlot);
    vkCmdEndRenderPass(cmd);

    // ---- 3) Make both targets readable by the lighting pass ---------------
    // The render pass deliberately ends in *_ATTACHMENT_OPTIMAL (see
    // InitializeInstantPass), so THIS barrier both transitions the layouts and
    // publishes the writes to the fragment shader. A finalLayout of
    // SHADER_READ_ONLY would transition the layout without a memory
    // dependency, and the lighting pass could read the previous frame's data -
    // a race that only shows up as flicker under load.
    VkImageMemoryBarrier barriers[3]{};
    barriers[0].sType = VK_STRUCTURE_TYPE_IMAGE_MEMORY_BARRIER;
    barriers[0].srcAccessMask = VK_ACCESS_COLOR_ATTACHMENT_WRITE_BIT;
    barriers[0].dstAccessMask = VK_ACCESS_SHADER_READ_BIT;
    barriers[0].oldLayout = VK_IMAGE_LAYOUT_COLOR_ATTACHMENT_OPTIMAL;
    barriers[0].newLayout = VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL;
    barriers[0].srcQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED;
    barriers[0].dstQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED;
    barriers[0].image = instantColorImage_.image;
    barriers[0].subresourceRange = {VK_IMAGE_ASPECT_COLOR_BIT, 0, 1, 0, 1};
    barriers[1] = barriers[0];
    barriers[1].srcAccessMask = VK_ACCESS_DEPTH_STENCIL_ATTACHMENT_WRITE_BIT;
    barriers[1].oldLayout = VK_IMAGE_LAYOUT_DEPTH_STENCIL_ATTACHMENT_OPTIMAL;
    barriers[1].image = instantDepthImage_.image;
    barriers[1].subresourceRange = {VK_IMAGE_ASPECT_DEPTH_BIT, 0, 1, 0, 1};
    // The STORED oct16 normal attachment goes to shader-read too - it is the
    // production normal source and is read every lit pixel.
    barriers[2] = barriers[0];
    barriers[2].image = instantNormalImage_.image;
    barriers[2].subresourceRange = {VK_IMAGE_ASPECT_COLOR_BIT, 0, 1, 0, 1};
    vkCmdPipelineBarrier(
        cmd,
        VK_PIPELINE_STAGE_COLOR_ATTACHMENT_OUTPUT_BIT |
            VK_PIPELINE_STAGE_EARLY_FRAGMENT_TESTS_BIT |
            VK_PIPELINE_STAGE_LATE_FRAGMENT_TESTS_BIT,
        VK_PIPELINE_STAGE_FRAGMENT_SHADER_BIT,
        0, 0, nullptr, 0, nullptr, 3, barriers);

    // ---- 4) Reopen the MAIN render pass and light the target --------------
    // Same framebuffer/render pass BeginFrame() used, so EndFrame() ends it
    // and submits exactly as it always has - no frame-loop contract changes.
    VkRenderPassBeginInfo mb{};
    mb.sType = VK_STRUCTURE_TYPE_RENDER_PASS_BEGIN_INFO;
    mb.renderPass = renderPass_.GetRenderPass();
    mb.framebuffer = framebuffers_[currentImageIndex_];
    mb.renderArea.offset = {0, 0};
    mb.renderArea.extent = ext;
    VkClearValue mclear[2]{};
    mclear[0].color.float32[0] = clearColor_[0];
    mclear[0].color.float32[1] = clearColor_[1];
    mclear[0].color.float32[2] = clearColor_[2];
    mclear[0].color.float32[3] = clearColor_[3];
    mclear[1].depthStencil = {1.0f, 0};
    mb.clearValueCount = 2;
    mb.pClearValues = mclear;
    vkCmdBeginRenderPass(cmd, &mb, VK_SUBPASS_CONTENTS_INLINE);

    VkViewport vp{0.0f, 0.0f, static_cast<float>(ext.width),
                     static_cast<float>(ext.height), 0.0f, 1.0f};
    VkRect2D sc{{0, 0}, {ext.width, ext.height}};
    vkCmdSetViewport(cmd, 0, 1, &vp);
    vkCmdSetScissor(cmd, 0, 1, &sc);

    vkCmdBindPipeline(cmd, VK_PIPELINE_BIND_POINT_GRAPHICS, instantPipeline_);
    VkDescriptorSet sets[2] = {frameSets_[frameManager_.GetCurrentFrameIndex()],
                               instantSet1_};
    vkCmdBindDescriptorSets(cmd, VK_PIPELINE_BIND_POINT_GRAPHICS,
                            instantPipelineLayout_, 0, 2, sets, 0, nullptr);

    // Layout must match instant.frag's LightingParams block byte for byte
    // (4 x vec4 + mat4 = 128 bytes, std430).
    struct LightingPush {
        float sun[4];
        float params[4];
        float camera[4];
        float background[4];
        float invMvp[16];
    } lp{};

    // Mirror of gui/shading_display.py::_compute_shading - the SAME hillshade
    // the legacy CPU/TIN path applies, so the two paths converge on one look.
    const float zenith = (90.0f - instantElevationDeg_) * kDegToRad;
    const float azMath = (360.0f - instantAzimuthDeg_ + 90.0f) * kDegToRad;
    lp.sun[0] = std::sin(zenith) * std::cos(azMath);
    lp.sun[1] = std::sin(zenith) * std::sin(azMath);
    lp.sun[2] = std::cos(zenith);
    lp.sun[3] = instantAmbient_;

    lp.params[0] = static_cast<float>(instantDebugStage_);
    lp.params[1] = static_cast<float>(instantNormalRadius_);
    lp.params[2] = 1.0f / static_cast<float>(ext.width);
    lp.params[3] = 1.0f / static_cast<float>(ext.height);

    float cam[3] = {};
    camera_.GetPositionRender(origin_, cam);
    lp.camera[0] = cam[0];
    lp.camera[1] = cam[1];
    lp.camera[2] = cam[2];
    lp.camera[3] = static_cast<float>(instantNormalSource_);

    lp.background[0] = clearColor_[0];
    lp.background[1] = clearColor_[1];
    lp.background[2] = clearColor_[2];
    lp.background[3] = clearColor_[3];

    // invMvp is what turns a depth texel back into a render-space position.
    // GetLastMvp() reads the UBO BeginFrame() already refreshed this frame, so
    // this IS the matrix the splats were projected with this frame.
    float mvp[16] = {};
    GetLastMvp(mvp);
    if (!InvertColumnMajor4(mvp, lp.invMvp)) {
        // Singular/degenerate camera: identity keeps the pass drawing (with
        // wrong normals) instead of spraying NaN across the viewport.
        std::memset(lp.invMvp, 0, sizeof(lp.invMvp));
        lp.invMvp[0] = lp.invMvp[5] = lp.invMvp[10] = lp.invMvp[15] = 1.0f;
    }

    // One-time self-check on the FIRST instant frame. A 4x4 inverse that was
    // accidentally transposed still compiles, still validates, and still shows
    // a plausible picture - it just produces wrong normals. Round-tripping
    // probes through the actual mvp/invMvp pair in use here is the only way to
    // catch that without reading a pixel back.
    //
    // The ASSERTION is in NDC space. The world-space error is reported only:
    // with a far plane of 1e6 the depth axis alone contributes
    //     float32_eps / (2 / (far - near))  ~  0.03 world units
    // which is the conditioning of the projection, not an error in the
    // inverse - asserting on it would be asserting against float32.
    if (!instantSelfTestDone_) {
        instantSelfTestDone_ = true;
        const float probes[3][3] = {{0.f, 0.f, 0.f}, {10.f, -5.f, 3.f}, {-7.f, 12.f, -2.f}};
        float worstNdc = 0.0f;
        float worstWorld = 0.0f;
        for (const auto& p : probes) {
            float c[4] = {};
            for (int r = 0; r < 4; ++r)
                c[r] = mvp[0 * 4 + r] * p[0] + mvp[1 * 4 + r] * p[1] +
                       mvp[2 * 4 + r] * p[2] + mvp[3 * 4 + r];
            if (std::fabs(c[3]) < 1e-9f) continue;
            const float ndc[3] = {c[0] / c[3], c[1] / c[3], c[2] / c[3]};

            float q[4] = {};
            for (int r = 0; r < 4; ++r)
                q[r] = lp.invMvp[0 * 4 + r] * ndc[0] + lp.invMvp[1 * 4 + r] * ndc[1] +
                       lp.invMvp[2 * 4 + r] * ndc[2] + lp.invMvp[3 * 4 + r];
            if (std::fabs(q[3]) < 1e-9f) continue;
            const float back[3] = {q[0] / q[3], q[1] / q[3], q[2] / q[3]};
            worstWorld = std::max(worstWorld,
                                  std::fabs(back[0] - p[0]) + std::fabs(back[1] - p[1]) +
                                      std::fabs(back[2] - p[2]));

            float c2[4] = {};
            for (int r = 0; r < 4; ++r)
                c2[r] = mvp[0 * 4 + r] * back[0] + mvp[1 * 4 + r] * back[1] +
                        mvp[2 * 4 + r] * back[2] + mvp[3 * 4 + r];
            if (std::fabs(c2[3]) < 1e-9f) continue;
            const float ndc2[3] = {c2[0] / c2[3], c2[1] / c2[3], c2[2] / c2[3]};
            worstNdc = std::max(worstNdc, std::max(
                std::fabs(ndc2[0] - ndc[0]),
                std::max(std::fabs(ndc2[1] - ndc[1]), std::fabs(ndc2[2] - ndc[2]))));
        }
        fprintf(stderr,
                "[INSTANT SHADED] invMvp NDC round-trip worst = %g %s "
                "(world-space residual %g, projection conditioning)\n",
                static_cast<double>(worstNdc),
                (worstNdc < 1e-4f) ? "(OK)" : "(FAILED - normals would be wrong)",
                static_cast<double>(worstWorld));
    }

    vkCmdPushConstants(cmd, instantPipelineLayout_, VK_SHADER_STAGE_FRAGMENT_BIT,
                       0, sizeof(lp), &lp);
    vkCmdDraw(cmd, 3, 1, 0, 0);
    instantFrames_++;
}

void Renderer::UpdateFrameUbo(uint32_t slot) {
    if (slot >= maxFramesInFlight_) return;
    FrameUbo ubo{};
    camera_.GetViewProjectionFloat(origin_, ubo.mvp);
    float rp[3] = {};
    camera_.GetPositionRender(origin_, rp);
    ubo.cameraPos[0] = rp[0]; ubo.cameraPos[1] = rp[1]; ubo.cameraPos[2] = rp[2];
    ubo.cameraPos[3] = 0.0f;
    ubo.origin[0] = static_cast<float>(origin_.x);
    ubo.origin[1] = static_cast<float>(origin_.y);
    ubo.origin[2] = static_cast<float>(origin_.z);
    ubo.origin[3] = 0.0f;
    // Colouring tables ride in the same UBO: std140 vec4 arrays line up
    // one-for-one with the float[256*4] members. A 12 KiB memcpy per frame is
    // noise next to the draw, and it is what lets a display-mode switch or a
    // palette edit be a push-constant/LUT refresh instead of a per-point
    // colour re-bake.
    std::memcpy(ubo.classPalette, lutClass_, sizeof(ubo.classPalette));
    std::memcpy(ubo.elevationLut, lutElevation_, sizeof(ubo.elevationLut));
    std::memcpy(ubo.intensityLut, lutIntensity_, sizeof(ubo.intensityLut));
    std::memcpy(frameUboBuffers_[slot].mappedData, &ubo, sizeof(ubo));
    // Host-visible memory is not guaranteed to be HOST_COHERENT, and VMA only
    // flushes automatically on vmaUnmapMemory - these buffers stay mapped for
    // the lifetime of the renderer, so an explicit flush is required or the
    // GPU may keep reading the previous camera.
    if (frameUboBuffers_[slot].mappedData) frameUboBuffers_[slot].FlushMapped();
}

void Renderer::WriteTimestamp(VkCommandBuffer cmd, uint32_t slot, uint32_t subSlot) {
    // Offscreen capture uses a single-time command buffer that does not reset
    // the query pool first - see Renderer::captureActive_.
    if (!enableGpuTimestamps_ || captureActive_) return;
    vkCmdWriteTimestamp(cmd, VK_PIPELINE_STAGE_VERTEX_SHADER_BIT,
                            timestampPool_, slot * kTimestampsPerFrame + subSlot);
}

// Sub-renderer resources
VulkanContext& Renderer::GetContext() { return context_; }
Camera& Renderer::GetCamera() { return camera_; }
const Camera& Renderer::GetCamera() const { return camera_; }
RenderOrigin& Renderer::GetOrigin() { return origin_; }
const RenderOrigin& Renderer::GetOrigin() const { return origin_; }
void Renderer::SetOrigin(double x, double y, double z) { origin_.Set(x, y, z); }
VkRenderPass Renderer::GetRenderPass() const { return renderPass_.GetRenderPass(); }
VkDescriptorSetLayout Renderer::GetFrameSetLayout() const { return frameSetLayout_; }
UploadContext& Renderer::GetUploadContext() { return uploadContext_; }

uint32_t Renderer::CurrentFrameSlot() const { return frameManager_.GetCurrentFrameIndex(); }
VkCommandBuffer Renderer::CurrentCommandBuffer() const {
    // Must be the buffer BeginFrame() began and EndFrame() will submit, i.e.
    // the one belonging to the acquired swapchain image - NOT the in-flight
    // frame slot's own index (see currentImageIndex_ comment in the header).
    if (currentImageIndex_ >= commandBuffers_.size()) return VK_NULL_HANDLE;
    return commandBuffers_[currentImageIndex_];
}
VkExtent2D Renderer::GetExtent() const { return extent_; }

VulkanPipelineManager& Renderer::Pipelines() { return pipelineManager_; }
VulkanShaderManager& Renderer::Shaders() { return shaderManager_; }
VulkanDescriptorManager& Renderer::Descriptors() { return descriptorManager_; }

const GpuTimings& Renderer::LastGpuTimings() const { return lastTimings_; }
double Renderer::LastCpuFrameMs() const { return NowMs() - lastCpuFrameStartMs_; }

void Renderer::SetClearColor(float r, float g, float b, float a) {
    clearColor_[0] = r;
    clearColor_[1] = g;
    clearColor_[2] = b;
    clearColor_[3] = a;
}

void Renderer::GetClearColor(float out[4]) const {
    if (!out) return;
    for (int i = 0; i < 4; ++i) out[i] = clearColor_[i];
}

void Renderer::GetLastMvp(float out[16]) const {
    if (!out) return;
    for (int i = 0; i < 16; ++i) out[i] = 0.0f;
    const uint32_t slot = frameManager_.GetCurrentFrameIndex();
    if (slot >= frameUboBuffers_.size()) return;
    const FrameUbo* ubo = static_cast<const FrameUbo*>(frameUboBuffers_[slot].mappedData);
    if (!ubo) return;
    for (int i = 0; i < 16; ++i) out[i] = ubo->mvp[i];
}

uint64_t Renderer::RegisterSwapchainRebuildCallback(RebuildCallback cb) {
    uint64_t id = nextCallbackId_++;
    rebuildCallbacks_.emplace_back(id, std::move(cb));
    return id;
}
void Renderer::UnregisterSwapchainRebuildCallback(uint64_t id) {
    rebuildCallbacks_.erase(
        std::remove_if(rebuildCallbacks_.begin(), rebuildCallbacks_.end(),
                       [id](const std::pair<uint64_t, RebuildCallback>& p) {
                           return p.first == id;
                       }),
        rebuildCallbacks_.end());
}

const FrameStatistics& Renderer::GetFrameStatistics() const { return stats_; }
void Renderer::PrintDiagnostics() const { context_.PrintDiagnostics(); }

void Renderer::CreateFrameResources(uint32_t slot) {
    if (slot >= maxFramesInFlight_) return;
    VkDescriptorSetLayout layouts[] = {frameSetLayout_};
    VkDescriptorSetAllocateInfo ai{VK_STRUCTURE_TYPE_DESCRIPTOR_SET_ALLOCATE_INFO};
    ai.descriptorPool = descriptorPool_;
    ai.descriptorSetCount = 1;
    ai.pSetLayouts = layouts;
    if (vkAllocateDescriptorSets(context_.GetDevice(), &ai, &frameSets_[slot]) != VK_SUCCESS)
        fprintf(stderr, "[Renderer] descriptor set FAILED slot=%u\n", slot);
    VkDescriptorBufferInfo bufInfo{};
    bufInfo.buffer = frameUboBuffers_[slot].buffer;
    bufInfo.offset = 0;
    bufInfo.range = sizeof(FrameUbo);
    descriptorManager_.UpdateBuffer(frameSets_[slot], 0,
                                         frameUboBuffers_[slot].buffer,
                                         sizeof(FrameUbo),
                                         VK_DESCRIPTOR_TYPE_UNIFORM_BUFFER);
}

void Renderer::DestroyFrameResources(uint32_t slot) {
    if (slot >= maxFramesInFlight_) return;
    if (frameSets_[slot] != VK_NULL_HANDLE) {
        descriptorManager_.FreeSets(descriptorPool_, {frameSets_[slot]});
        frameSets_[slot] = VK_NULL_HANDLE;
    }
}

void Renderer::DestroyCaptureTarget() {
    if (captureFramebuffer_ != VK_NULL_HANDLE) {
        vkDestroyFramebuffer(context_.GetDevice(), captureFramebuffer_, nullptr);
        captureFramebuffer_ = VK_NULL_HANDLE;
    }
    if (captureView_ != VK_NULL_HANDLE) {
        vkDestroyImageView(context_.GetDevice(), captureView_, nullptr);
        captureView_ = VK_NULL_HANDLE;
    }
    if (captureImage_.image != VK_NULL_HANDLE) {
        VulkanAllocator::Get().DestroyImage(captureImage_);
        captureImage_ = {};
    }
}

// Offscreen pixel readback. Windows screen grabs (QScreen.grabWindow AND
// PrintWindow) go through GDI, which reports a flat black image for a Vulkan
// surface, so this is the ONLY reliable way to see what the engine actually
// drew - it is what the visual-parity checks compare against VTK.
//
// It re-renders the SAME scene with the SAME frame-slot UBO (i.e. the camera
// last pushed through nkv_set_camera_lookat) into a private colour image that
// shares the swapchain's format/extent, so the point and surface pipelines are
// used verbatim - no second pipeline set for a different colour format. The
// depth attachment is depthViews_[0]: same format and size, and the render
// pass declares its initial layout for every frame anyway.
//
// Blocking by design (vkDeviceWaitIdle + vkQueueWaitIdle): a diagnostic /
// parity-test path, never a per-frame call.
bool Renderer::CaptureOffscreenRGBA8(PointCloudRenderer* pointCloud, SurfaceRenderer* surface,
                                     std::vector<uint8_t>& outRGBA, uint32_t& outW, uint32_t& outH) {
    outRGBA.clear();
    outW = outH = 0;
    if (!initialized_ || extent_.width == 0 || extent_.height == 0) return false;
    if (recreateRequested_) return false;   // a resize is pending; try again next frame

    // This records into its OWN single-time command buffer, which does not
    // run the per-frame vkCmdResetQueryPool - so point/surface Record() calls
    // must not write timestamps from here or validation reports
    // "vkCmdWriteTimestamp(): query N not reset". Restored on every exit.
    captureActive_ = true;
    struct CaptureActiveGuard {
        bool& flag;
        ~CaptureActiveGuard() { flag = false; }
    } captureGuard{captureActive_};

    VulkanAllocator& alloc = VulkanAllocator::Get();
    VkDevice dev = context_.GetDevice();
    context_.CoreDevice().WaitIdle();

    const VkFormat fmt = swapchain_.GetImageFormat();
    if (captureImage_.image == VK_NULL_HANDLE || captureImage_.format != fmt ||
        captureImage_.extent.width != extent_.width ||
        captureImage_.extent.height != extent_.height || captureFramebuffer_ == VK_NULL_HANDLE) {
        DestroyCaptureTarget();
        captureImage_ = alloc.CreateImage(
            fmt, extent_,
            VK_IMAGE_USAGE_COLOR_ATTACHMENT_BIT | VK_IMAGE_USAGE_TRANSFER_SRC_BIT,
            VMA_MEMORY_USAGE_GPU_ONLY);
        if (captureImage_.image == VK_NULL_HANDLE) {
            fprintf(stderr, "[Renderer] capture image create FAILED (%ux%u)\n",
                    extent_.width, extent_.height);
            return false;
        }
        VkImageViewCreateInfo vi{};
        vi.sType = VK_STRUCTURE_TYPE_IMAGE_VIEW_CREATE_INFO;
        vi.image = captureImage_.image;
        vi.viewType = VK_IMAGE_VIEW_TYPE_2D;
        vi.format = fmt;
        vi.subresourceRange = {VK_IMAGE_ASPECT_COLOR_BIT, 0, 1, 0, 1};
        if (vkCreateImageView(dev, &vi, nullptr, &captureView_) != VK_SUCCESS) {
            fprintf(stderr, "[Renderer] capture image view FAILED\n");
            DestroyCaptureTarget();
            return false;
        }
        if (depthViews_.empty()) {
            DestroyCaptureTarget();
            return false;
        }
        VkImageView atts[2] = {captureView_, depthViews_[0]};
        VkFramebufferCreateInfo fbi{};
        fbi.sType = VK_STRUCTURE_TYPE_FRAMEBUFFER_CREATE_INFO;
        fbi.renderPass = renderPass_.GetRenderPass();
        fbi.attachmentCount = 2;
        fbi.pAttachments = atts;
        fbi.width = extent_.width;
        fbi.height = extent_.height;
        fbi.layers = 1;
        if (vkCreateFramebuffer(dev, &fbi, nullptr, &captureFramebuffer_) != VK_SUCCESS) {
            fprintf(stderr, "[Renderer] capture framebuffer FAILED\n");
            DestroyCaptureTarget();
            return false;
        }
    }

    const uint32_t w = extent_.width, h = extent_.height;
    const std::size_t pixelCount = static_cast<std::size_t>(w) * h;
    GPUBuffer readback = alloc.CreateBuffer(
        static_cast<VkDeviceSize>(pixelCount) * 4, VK_BUFFER_USAGE_TRANSFER_DST_BIT,
        VMA_MEMORY_USAGE_GPU_TO_CPU,
        VMA_ALLOCATION_CREATE_HOST_ACCESS_RANDOM_BIT | VMA_ALLOCATION_CREATE_MAPPED_BIT);
    if (!readback.IsValid() || readback.mappedData == nullptr) {
        fprintf(stderr, "[Renderer] capture readback buffer FAILED (%ux%u)\n", w, h);
        alloc.DestroyBuffer(readback);
        return false;
    }

    // The capture must show the camera the live view has, so refresh the slot
    // UBO from camera_ before recording (the same call BeginFrame makes).
    const uint32_t slot = frameManager_.GetCurrentFrameIndex();
    UpdateFrameUbo(slot);

    VkCommandBuffer cmd = alloc.BeginSingleTimeCommands();
    if (cmd == VK_NULL_HANDLE) {
        alloc.DestroyBuffer(readback);
        return false;
    }

    VkImageMemoryBarrier bar{};
    bar.sType = VK_STRUCTURE_TYPE_IMAGE_MEMORY_BARRIER;
    bar.image = captureImage_.image;
    bar.subresourceRange = {VK_IMAGE_ASPECT_COLOR_BIT, 0, 1, 0, 1};
    bar.srcQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED;
    bar.dstQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED;
    bar.oldLayout = VK_IMAGE_LAYOUT_UNDEFINED;
    bar.newLayout = VK_IMAGE_LAYOUT_COLOR_ATTACHMENT_OPTIMAL;
    bar.srcAccessMask = 0;
    bar.dstAccessMask = VK_ACCESS_COLOR_ATTACHMENT_WRITE_BIT;
    vkCmdPipelineBarrier(cmd, VK_PIPELINE_STAGE_TOP_OF_PIPE_BIT,
                         VK_PIPELINE_STAGE_COLOR_ATTACHMENT_OUTPUT_BIT, 0,
                         0, nullptr, 0, nullptr, 1, &bar);

    VkRenderPassBeginInfo rp{};
    rp.sType = VK_STRUCTURE_TYPE_RENDER_PASS_BEGIN_INFO;
    rp.renderPass = renderPass_.GetRenderPass();
    rp.framebuffer = captureFramebuffer_;
    rp.renderArea.offset = {0, 0};
    rp.renderArea.extent = extent_;
    VkClearValue clears[2]{};
    clears[0].color.float32[0] = clearColor_[0];
    clears[0].color.float32[1] = clearColor_[1];
    clears[0].color.float32[2] = clearColor_[2];
    clears[0].color.float32[3] = clearColor_[3];
    clears[1].depthStencil = {1.0f, 0};
    rp.clearValueCount = 2;
    rp.pClearValues = clears;
    vkCmdBeginRenderPass(cmd, &rp, VK_SUBPASS_CONTENTS_INLINE);
    if (pointCloud && pointCloud->IsLoaded()) pointCloud->Record(cmd, slot);
    if (surface && surface->IsLoaded()) surface->Record(cmd, slot);
    vkCmdEndRenderPass(cmd);

    // BUG FOUND AND FIXED THIS ROUND (confirmed via validation layers): this
    // capture framebuffer uses the SAME VkRenderPass as the live swapchain
    // framebuffers (VulkanRenderPass.cpp hardcodes the color attachment's
    // finalLayout to VK_IMAGE_LAYOUT_PRESENT_SRC_KHR, correct for the real
    // presentation framebuffers). vkCmdEndRenderPass() therefore ALWAYS
    // transitions captureImage_ to PRESENT_SRC_KHR, matching that
    // finalLayout - never to COLOR_ATTACHMENT_OPTIMAL. The barrier below
    // used to claim oldLayout=COLOR_ATTACHMENT_OPTIMAL, which the
    // validation layer flagged as a real mismatch against the image's
    // actual tracked layout ("cannot transition ... from
    // VK_IMAGE_LAYOUT_COLOR_ATTACHMENT_OPTIMAL when the previous known
    // layout is VK_IMAGE_LAYOUT_PRESENT_SRC_KHR"). A barrier with a false
    // oldLayout does not reliably flush/transition the image for the
    // subsequent copy, which is a real, plausible cause of the capture
    // silently reading empty/garbage content.
    bar.oldLayout = VK_IMAGE_LAYOUT_PRESENT_SRC_KHR;
    bar.newLayout = VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL;
    bar.srcAccessMask = VK_ACCESS_COLOR_ATTACHMENT_WRITE_BIT;
    bar.dstAccessMask = VK_ACCESS_TRANSFER_READ_BIT;
    vkCmdPipelineBarrier(cmd, VK_PIPELINE_STAGE_COLOR_ATTACHMENT_OUTPUT_BIT,
                         VK_PIPELINE_STAGE_TRANSFER_BIT, 0, 0, nullptr, 0, nullptr, 1, &bar);

    VkBufferImageCopy copy{};
    copy.imageSubresource = {VK_IMAGE_ASPECT_COLOR_BIT, 0, 0, 1};
    copy.imageExtent = {w, h, 1};
    vkCmdCopyImageToBuffer(cmd, captureImage_.image, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL,
                           readback.buffer, 1, &copy);
    alloc.EndSingleTimeCommands(cmd);   // blocks: submits and waits (cmd not freed)

    readback.InvalidateMapped();
    const uint8_t* src = static_cast<const uint8_t*>(readback.mappedData);
    const bool bgra = (fmt == VK_FORMAT_B8G8R8A8_UNORM || fmt == VK_FORMAT_B8G8R8A8_SRGB);
    outRGBA.resize(pixelCount * 4);
    if (bgra) {
        for (std::size_t i = 0; i < pixelCount; ++i) {
            outRGBA[i * 4 + 0] = src[i * 4 + 2];
            outRGBA[i * 4 + 1] = src[i * 4 + 1];
            outRGBA[i * 4 + 2] = src[i * 4 + 0];
            outRGBA[i * 4 + 3] = src[i * 4 + 3];
        }
    } else {
        std::memcpy(outRGBA.data(), src, pixelCount * 4);
    }
    alloc.DestroyBuffer(readback);
    outW = w;
    outH = h;
    return true;
}

// Depth readback for the same offscreen capture. See the declaration in
// Renderer.hpp for why the parity gate needs it: depth is the ground truth for
// "which face owns this pixel", which colour alone cannot establish.
bool Renderer::CaptureOffscreenDepth32F(PointCloudRenderer* pointCloud, SurfaceRenderer* surface,
                                       std::vector<float>& outDepth, uint32_t& outW, uint32_t& outH) {
    outDepth.clear();
    outW = outH = 0;
    if (!initialized_ || extent_.width == 0 || extent_.height == 0) return false;
    if (recreateRequested_) return false;
    if (depthImages_.empty() || depthImages_[0].image == VK_NULL_HANDLE) return false;
    // A non-float depth format (e.g. D24_UNORM_S8_UINT) has no direct float
    // copy; report it rather than returning garbage.
    if (depthFormat_ != VK_FORMAT_D32_SFLOAT) {
        fprintf(stderr, "[Renderer] depth readback needs D32_SFLOAT, have %d\n",
                static_cast<int>(depthFormat_));
        return false;
    }

    captureActive_ = true;
    struct CaptureActiveGuard {
        bool& flag;
        ~CaptureActiveGuard() { flag = false; }
    } captureGuard{captureActive_};

    VulkanAllocator& alloc = VulkanAllocator::Get();
    VkDevice dev = context_.GetDevice();
    context_.CoreDevice().WaitIdle();

    if (captureImage_.image == VK_NULL_HANDLE || captureImage_.format != swapchain_.GetImageFormat() ||
        captureImage_.extent.width != extent_.width || captureImage_.extent.height != extent_.height ||
        captureFramebuffer_ == VK_NULL_HANDLE) {
        DestroyCaptureTarget();
        const VkFormat fmt = swapchain_.GetImageFormat();
        captureImage_ = alloc.CreateImage(
            fmt, extent_,
            VK_IMAGE_USAGE_COLOR_ATTACHMENT_BIT | VK_IMAGE_USAGE_TRANSFER_SRC_BIT,
            VMA_MEMORY_USAGE_GPU_ONLY);
        if (captureImage_.image == VK_NULL_HANDLE) return false;
        VkImageViewCreateInfo vi{};
        vi.sType = VK_STRUCTURE_TYPE_IMAGE_VIEW_CREATE_INFO;
        vi.image = captureImage_.image;
        vi.viewType = VK_IMAGE_VIEW_TYPE_2D;
        vi.format = fmt;
        vi.subresourceRange = {VK_IMAGE_ASPECT_COLOR_BIT, 0, 1, 0, 1};
        if (vkCreateImageView(dev, &vi, nullptr, &captureView_) != VK_SUCCESS) {
            DestroyCaptureTarget(); return false;
        }
        if (depthViews_.empty()) { DestroyCaptureTarget(); return false; }
        VkImageView atts[2] = {captureView_, depthViews_[0]};
        VkFramebufferCreateInfo fbi{};
        fbi.sType = VK_STRUCTURE_TYPE_FRAMEBUFFER_CREATE_INFO;
        fbi.renderPass = renderPass_.GetRenderPass();
        fbi.attachmentCount = 2;
        fbi.pAttachments = atts;
        fbi.width = extent_.width;
        fbi.height = extent_.height;
        fbi.layers = 1;
        if (vkCreateFramebuffer(dev, &fbi, nullptr, &captureFramebuffer_) != VK_SUCCESS) {
            DestroyCaptureTarget(); return false;
        }
    }

    const uint32_t w = extent_.width, h = extent_.height;
    const std::size_t pixelCount = static_cast<std::size_t>(w) * h;
    GPUBuffer readback = alloc.CreateBuffer(
        static_cast<VkDeviceSize>(pixelCount) * sizeof(float), VK_BUFFER_USAGE_TRANSFER_DST_BIT,
        VMA_MEMORY_USAGE_GPU_TO_CPU,
        VMA_ALLOCATION_CREATE_HOST_ACCESS_RANDOM_BIT | VMA_ALLOCATION_CREATE_MAPPED_BIT);
    if (!readback.IsValid() || readback.mappedData == nullptr) {
        alloc.DestroyBuffer(readback);
        return false;
    }

    const uint32_t slot = frameManager_.GetCurrentFrameIndex();
    UpdateFrameUbo(slot);

    VkCommandBuffer cmd = alloc.BeginSingleTimeCommands();
    if (cmd == VK_NULL_HANDLE) { alloc.DestroyBuffer(readback); return false; }

    VkImageMemoryBarrier bar{};
    bar.sType = VK_STRUCTURE_TYPE_IMAGE_MEMORY_BARRIER;
    bar.image = captureImage_.image;
    bar.subresourceRange = {VK_IMAGE_ASPECT_COLOR_BIT, 0, 1, 0, 1};
    bar.srcQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED;
    bar.dstQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED;
    bar.oldLayout = VK_IMAGE_LAYOUT_UNDEFINED;
    bar.newLayout = VK_IMAGE_LAYOUT_COLOR_ATTACHMENT_OPTIMAL;
    bar.srcAccessMask = 0;
    bar.dstAccessMask = VK_ACCESS_COLOR_ATTACHMENT_WRITE_BIT;
    vkCmdPipelineBarrier(cmd, VK_PIPELINE_STAGE_TOP_OF_PIPE_BIT,
                         VK_PIPELINE_STAGE_COLOR_ATTACHMENT_OUTPUT_BIT, 0,
                         0, nullptr, 0, nullptr, 1, &bar);

    VkRenderPassBeginInfo rp{};
    rp.sType = VK_STRUCTURE_TYPE_RENDER_PASS_BEGIN_INFO;
    rp.renderPass = renderPass_.GetRenderPass();
    rp.framebuffer = captureFramebuffer_;
    rp.renderArea.offset = {0, 0};
    rp.renderArea.extent = extent_;
    VkClearValue clears[2]{};
    clears[0].color.float32[0] = clearColor_[0];
    clears[0].color.float32[1] = clearColor_[1];
    clears[0].color.float32[2] = clearColor_[2];
    clears[0].color.float32[3] = clearColor_[3];
    clears[1].depthStencil = {1.0f, 0};
    rp.clearValueCount = 2;
    rp.pClearValues = clears;
    vkCmdBeginRenderPass(cmd, &rp, VK_SUBPASS_CONTENTS_INLINE);
    if (pointCloud && pointCloud->IsLoaded()) pointCloud->Record(cmd, slot);
    if (surface && surface->IsLoaded()) surface->Record(cmd, slot);
    vkCmdEndRenderPass(cmd);

    // depthImages_[0] left the render pass in DEPTH_STENCIL_ATTACHMENT_OPTIMAL
    // (its declared finalLayout in VulkanRenderPass.cpp), so THAT is the correct
    // oldLayout here - claiming ATTACHMENT_OPTIMAL would be a false layout and
    // the copy would read garbage.
    bar.image = depthImages_[0].image;
    bar.subresourceRange = {VK_IMAGE_ASPECT_DEPTH_BIT, 0, 1, 0, 1};
    bar.oldLayout = VK_IMAGE_LAYOUT_DEPTH_STENCIL_ATTACHMENT_OPTIMAL;
    bar.newLayout = VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL;
    bar.srcAccessMask = VK_ACCESS_DEPTH_STENCIL_ATTACHMENT_WRITE_BIT;
    bar.dstAccessMask = VK_ACCESS_TRANSFER_READ_BIT;
    vkCmdPipelineBarrier(cmd, VK_PIPELINE_STAGE_LATE_FRAGMENT_TESTS_BIT,
                         VK_PIPELINE_STAGE_TRANSFER_BIT, 0, 0, nullptr, 0, nullptr, 1, &bar);

    VkBufferImageCopy copy{};
    copy.imageSubresource = {VK_IMAGE_ASPECT_DEPTH_BIT, 0, 0, 1};
    copy.imageExtent = {w, h, 1};
    vkCmdCopyImageToBuffer(cmd, depthImages_[0].image, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL,
                           readback.buffer, 1, &copy);

    // Restore the declared finalLayout so the next frame starts from a known state.
    bar.oldLayout = VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL;
    bar.newLayout = VK_IMAGE_LAYOUT_DEPTH_STENCIL_ATTACHMENT_OPTIMAL;
    bar.srcAccessMask = VK_ACCESS_TRANSFER_READ_BIT;
    bar.dstAccessMask = VK_ACCESS_DEPTH_STENCIL_ATTACHMENT_WRITE_BIT;
    vkCmdPipelineBarrier(cmd, VK_PIPELINE_STAGE_TRANSFER_BIT,
                         VK_PIPELINE_STAGE_LATE_FRAGMENT_TESTS_BIT, 0, 0, nullptr, 0, nullptr, 1, &bar);

    alloc.EndSingleTimeCommands(cmd);

    readback.InvalidateMapped();
    const float* src = static_cast<const float*>(readback.mappedData);
    outDepth.assign(src, src + pixelCount);
    alloc.DestroyBuffer(readback);
    outW = w;
    outH = h;
    return true;
}

} // namespace naksha


