#pragma once
#include "naksha/core/vulkan/VulkanHeaders.h"

namespace naksha {
namespace vulkan {

struct VulkanFrameSync {
    VkSemaphore imageAvailable = VK_NULL_HANDLE;
    VkFence inFlightFence = VK_NULL_HANDLE;
    VkCommandBuffer commandBuffer = VK_NULL_HANDLE;
    uint32_t imageIndex = 0;
    bool frameInFlight = false;
};

class VulkanFrameManager {
public:
    ~VulkanFrameManager();

    // Initialize() ALWAYS starts from a clean slate: it retires any sync
    // objects a previous Initialize() left behind, recreates imageAvailable
    // semaphores and inFlight fences (fences created SIGNALED, so the first
    // BeginFrame() after a swapchain recreation never blocks on a fence whose
    // signal operation belonged to the old swapchain), recreates the
    // PER-SWAPCHAIN-IMAGE renderFinished semaphores and resets the frame
    // index to 0.
    bool Initialize(VkDevice device, uint32_t maxFramesInFlight = 2,
                    uint32_t swapchainImageCount = 0);
    void Shutdown();

    VulkanFrameSync& GetCurrentFrame() { return frames_[currentFrame_]; }
    const VulkanFrameSync& GetFrame(uint32_t index) const { return frames_[index]; }
    uint32_t GetCurrentFrameIndex() const { return currentFrame_; }
    uint32_t GetMaxFramesInFlight() const { return maxFramesInFlight_; }

    // renderFinished is PER SWAPCHAIN IMAGE, not per frame slot. A per-frame
    // one gets re-signalled while the presentation engine may still be
    // waiting on it for a different image that was presented but not yet
    // re-acquired - validation reports exactly that as
    //   "vkQueueSubmit(): pSignalSemaphores[0] ... may still be in use by
    //    VkSwapchainKHR" (VUID-vkQueueSubmit-pSignalSemaphores-00067).
    // Keying it by acquired image index keeps every signal paired with the
    // image whose present consumed it.
    VkSemaphore GetRenderFinished(uint32_t imageIndex) const {
        if (imageRenderFinished_.empty()) return VK_NULL_HANDLE;
        return imageRenderFinished_[imageIndex % imageRenderFinished_.size()];
    }
    uint32_t GetRenderFinishedCount() const {
        return static_cast<uint32_t>(imageRenderFinished_.size());
    }

    // Waits on the current frame's fence ONLY. It deliberately does NOT reset
    // the fence: the fence is reset by ResetFence() immediately before the
    // submit that will re-signal it. This guarantees a fence is never left
    // unsignaled when a frame is skipped (e.g. VK_ERROR_OUT_OF_DATE_KHR).
    void BeginFrame();
    void ResetFence();
    void EndFrame();

    // ---- Deferred resource retirement (no vkDeviceWaitIdle) --------------
    //
    // EndFrame() advances frameCounter_ by one every submitted frame. Because
    // BeginFrame() blocks on slot (currentFrame_ % maxFramesInFlight_)'s fence,
    // all work submitted in frame F has provably completed by the time the
    // frame loop has moved N frames past F, where N = maxFramesInFlight_. A
    // resource that was last submitted in frame F can therefore be destroyed
    // once IsFrameRetired(F) is true, with no global device idle.
    uint64_t GetFrameCounter() const { return frameCounter_; }
    bool IsFrameRetired(uint64_t frameIndex) const {
        return frameIndex != kNeverSubmitted &&
               frameCounter_ > frameIndex + maxFramesInFlight_;
    }
    static constexpr uint64_t kNeverSubmitted = ~uint64_t(0);

    void SetCommandBuffer(uint32_t frameIndex, VkCommandBuffer buffer) {
        frames_[frameIndex].commandBuffer = buffer;
    }

private:
    VkDevice device_ = VK_NULL_HANDLE;
    uint32_t maxFramesInFlight_ = 2;
    uint32_t currentFrame_ = 0;
    uint32_t swapchainImageCount_ = 0;
    // Monotonic count of SUBMITTED frames (advanced in EndFrame). Used by
    // IsFrameRetired() to retire resources without a device-wide idle.
    uint64_t frameCounter_ = 0;
    std::vector<VulkanFrameSync> frames_;
    // One renderFinished semaphore per swapchain image (see GetRenderFinished).
    std::vector<VkSemaphore> imageRenderFinished_;

    bool CreateSyncObjects();
};

} // namespace vulkan
} // namespace naksha
