#pragma once
#include "naksha/core/vulkan/VulkanHeaders.h"
#include "naksha/core/vulkan/VulkanPhysicalDevice.h"

namespace naksha {
namespace vulkan {

class VulkanSwapchain {
public:
    ~VulkanSwapchain();

    bool Initialize(VkDevice device, VkPhysicalDevice physicalDevice,
                    VkSurfaceKHR surface, uint32_t width, uint32_t height,
                    const SwapchainSupportDetails& support,
                    const QueueFamilyIndices& indices);
    void Shutdown();
    // Full authoritative rebuild. The CURRENT swapchain handle is handed to
    // vkCreateSwapchainKHR as VkSwapchainCreateInfoKHR::oldSwapchain so the
    // presentation engine can retire its presentable images atomically, and
    // the old handle is destroyed ONLY after the new swapchain exists (never
    // before - destroying it first drops every pending present and leaves the
    // surface black). On creation failure the previous swapchain and its
    // image views are restored so the app keeps presenting instead of going
    // permanently black.
    // The caller (Renderer::RecreateSwapchain) is responsible for waiting for
    // in-flight work beforehand and for rebuilding framebuffers/frame sync
    // afterwards.
    // NOTE: surface capabilities must be re-queried by the caller because
    // currentExtent changes with the window size.
    // Returns false when the replacement swapchain could not be created
    // (previous swapchain restored and still presenting).
    bool Recreate(uint32_t width, uint32_t height,
                  const SwapchainSupportDetails& support);

    VkResult AcquireNextImage(VkSemaphore signalSemaphore, uint32_t* imageIndex);
    VkResult Present(VkSemaphore waitSemaphore, uint32_t imageIndex);

    VkSwapchainKHR GetSwapchain() const { return swapchain_; }
    VkFormat GetImageFormat() const { return imageFormat_; }
    VkExtent2D GetExtent() const { return extent_; }
    VkImageView GetImageView(uint32_t index) const { return imageViews_[index]; }
    uint32_t GetImageCount() const { return static_cast<uint32_t>(images_.size()); }
    VkImage GetImage(uint32_t index) const { return images_[index]; }

private:
    VkDevice device_ = VK_NULL_HANDLE;
    VkPhysicalDevice physicalDevice_ = VK_NULL_HANDLE;
    VkSurfaceKHR surface_ = VK_NULL_HANDLE;
    VkSwapchainKHR swapchain_ = VK_NULL_HANDLE;
    VkQueue presentQueue_ = VK_NULL_HANDLE;

    VkFormat imageFormat_ = VK_FORMAT_UNDEFINED;
    VkExtent2D extent_ = {0, 0};

    std::vector<VkImage> images_;
    std::vector<VkImageView> imageViews_;

    QueueFamilyIndices indices_ = {};

    // Creates a swapchain and commits it (handle, extent, format, images,
    // views) only on success. `oldSwapchain` is passed straight through to
    // VkSwapchainCreateInfoKHR::oldSwapchain (VK_NULL_HANDLE on first create).
    // Returns false without touching any member state on failure.
    bool CreateSwapchain(uint32_t width, uint32_t height,
                         const SwapchainSupportDetails& support,
                         VkSwapchainKHR oldSwapchain = VK_NULL_HANDLE);
    void CreateImageViews();
    void CleanupSwapchain();
};

} // namespace vulkan
} // namespace naksha
