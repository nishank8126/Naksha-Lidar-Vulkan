#include "naksha/core/vulkan/VulkanFrameManager.h"
#include <vector>

namespace naksha {
namespace vulkan {

VulkanFrameManager::~VulkanFrameManager() { Shutdown(); }

bool VulkanFrameManager::Initialize(VkDevice device, uint32_t maxFramesInFlight,
                                     uint32_t swapchainImageCount) {
    // Re-initialization happens on swapchain recreation. The previous
    // generation of semaphores/fences MUST be retired first: those objects
    // are still referenced by the old swapchain's acquire/present operations,
    // and re-using them on the new swapchain is what triggers
    //   "vkQueueSubmit(): signal semaphore may still be in use by VkSwapchainKHR"
    // after a resize. Brand new objects carry no such association.
    if (device_ != VK_NULL_HANDLE) Shutdown();

    device_ = device;
    maxFramesInFlight_ = maxFramesInFlight;
    // renderFinished is keyed by swapchain image (see the header): size the
    // vector from the image count the caller just rebuilt the swapchain with.
    swapchainImageCount_ = (swapchainImageCount > 0) ? swapchainImageCount
                                                     : maxFramesInFlight;
    // A swapchain recreate always restarts the frame loop at slot 0 so the
    // first frame after the resize waits on a freshly-created (signaled)
    // fence instead of slot N's fence, whose last signal may have been
    // consumed by the retired swapchain.
    currentFrame_ = 0;
    frames_.resize(maxFramesInFlight);
    imageRenderFinished_.resize(swapchainImageCount_);
    return CreateSyncObjects();
}

void VulkanFrameManager::Shutdown() {
    for (auto& frame : frames_) {
        if (frame.imageAvailable) vkDestroySemaphore(device_, frame.imageAvailable, nullptr);
        if (frame.inFlightFence) vkDestroyFence(device_, frame.inFlightFence, nullptr);
        frame = {};
    }
    frames_.clear();
    for (auto& sem : imageRenderFinished_) {
        if (sem) vkDestroySemaphore(device_, sem, nullptr);
        sem = VK_NULL_HANDLE;
    }
    imageRenderFinished_.clear();
    swapchainImageCount_ = 0;
    currentFrame_ = 0;
    // NOT reset: frameCounter_ is a monotonic retirement clock, not a frame
    // index. Zeroing it here would make every already-submitted frame look
    // un-retired and would let a stale resource be destroyed while in flight.
    device_ = VK_NULL_HANDLE;
}

bool VulkanFrameManager::CreateSyncObjects() {
    VkSemaphoreCreateInfo semaphoreInfo{};
    semaphoreInfo.sType = VK_STRUCTURE_TYPE_SEMAPHORE_CREATE_INFO;

    VkFenceCreateInfo fenceInfo{};
    fenceInfo.sType = VK_STRUCTURE_TYPE_FENCE_CREATE_INFO;
    fenceInfo.flags = VK_FENCE_CREATE_SIGNALED_BIT;

    for (auto& frame : frames_) {
        // Defensive: never leak/overwrite a live handle if this is called
        // without a preceding Shutdown().
        if (frame.imageAvailable) vkDestroySemaphore(device_, frame.imageAvailable, nullptr);
        if (frame.inFlightFence) vkDestroyFence(device_, frame.inFlightFence, nullptr);
        frame = {};

        if (vkCreateSemaphore(device_, &semaphoreInfo, nullptr, &frame.imageAvailable) != VK_SUCCESS)
            return false;
        if (vkCreateFence(device_, &fenceInfo, nullptr, &frame.inFlightFence) != VK_SUCCESS)
            return false;
    }
    for (auto& sem : imageRenderFinished_) {
        if (sem) vkDestroySemaphore(device_, sem, nullptr);
        sem = VK_NULL_HANDLE;
        if (vkCreateSemaphore(device_, &semaphoreInfo, nullptr, &sem) != VK_SUCCESS)
            return false;
    }
    return true;
}

void VulkanFrameManager::BeginFrame() {
    // Wait only. The fence is NOT reset here: a frame may be skipped after
    // this point (e.g. swapchain OUT_OF_DATE), and resetting eagerly would
    // leave the fence unsignaled with no submit to re-signal it -> deadlock.
    auto& frame = frames_[currentFrame_];
    vkWaitForFences(device_, 1, &frame.inFlightFence, VK_TRUE, UINT64_MAX);
    frame.frameInFlight = true;
}

void VulkanFrameManager::ResetFence() {
    // Called immediately before the submit that re-signals this frame's fence.
    auto& frame = frames_[currentFrame_];
    vkResetFences(device_, 1, &frame.inFlightFence);
}

void VulkanFrameManager::EndFrame() {
    frames_[currentFrame_].frameInFlight = false;
    frameCounter_++;
    currentFrame_ = (currentFrame_ + 1) % maxFramesInFlight_;
}

} // namespace vulkan
} // namespace naksha
