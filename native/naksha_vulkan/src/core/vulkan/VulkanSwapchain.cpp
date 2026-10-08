#include "naksha/core/vulkan/VulkanSwapchain.h"
#include <algorithm>
#include <cstdint>
#include <cstdio>
#include <limits>

namespace naksha {
namespace vulkan {

VulkanSwapchain::~VulkanSwapchain() { Shutdown(); }

bool VulkanSwapchain::Initialize(VkDevice device, VkPhysicalDevice physicalDevice,
                                   VkSurfaceKHR surface, uint32_t width, uint32_t height,
                                   const SwapchainSupportDetails& support,
                                   const QueueFamilyIndices& indices) {
    device_ = device;
    physicalDevice_ = physicalDevice;
    surface_ = surface;
    indices_ = indices;
    vkGetDeviceQueue(device_, static_cast<uint32_t>(indices_.presentFamily), 0, &presentQueue_);

    CreateSwapchain(width, height, support, VK_NULL_HANDLE);
    return swapchain_ != VK_NULL_HANDLE;
}

bool VulkanSwapchain::CreateSwapchain(uint32_t width, uint32_t height,
                                        const SwapchainSupportDetails& support,
                                        VkSwapchainKHR oldSwapchain) {
    auto chooseFormat = [](const std::vector<VkSurfaceFormatKHR>& f) {
        for (auto& fmt : f) {
            if (fmt.format == VK_FORMAT_B8G8R8A8_SRGB &&
                fmt.colorSpace == VK_COLOR_SPACE_SRGB_NONLINEAR_KHR)
                return fmt;
        }
        return f[0];
    };

    auto choosePresentMode = [](const std::vector<VkPresentModeKHR>& m) {
        for (auto& mode : m) {
            if (mode == VK_PRESENT_MODE_MAILBOX_KHR) return mode;
        }
        return VK_PRESENT_MODE_FIFO_KHR;
    };

    auto chooseExtent = [&](const VkSurfaceCapabilitiesKHR& caps, uint32_t w, uint32_t h) {
        if (caps.currentExtent.width != (std::numeric_limits<uint32_t>::max)()) {
            return caps.currentExtent;
        }
        VkExtent2D ext = {w, h};
        ext.width = std::clamp(ext.width, caps.minImageExtent.width, caps.maxImageExtent.width);
        ext.height = std::clamp(ext.height, caps.minImageExtent.height, caps.maxImageExtent.height);
        return ext;
    };

    auto surfaceFormat = chooseFormat(support.formats);
    auto presentMode = choosePresentMode(support.presentModes);
    VkExtent2D newExtent = chooseExtent(support.capabilities, width, height);
    VkFormat newFormat = surfaceFormat.format;

    uint32_t imageCount = support.capabilities.minImageCount + 1;
    if (support.capabilities.maxImageCount > 0)
        imageCount = std::min(imageCount, support.capabilities.maxImageCount);

    VkSwapchainCreateInfoKHR createInfo{};
    createInfo.sType = VK_STRUCTURE_TYPE_SWAPCHAIN_CREATE_INFO_KHR;
    createInfo.surface = surface_;
    createInfo.minImageCount = imageCount;
    createInfo.imageFormat = newFormat;
    createInfo.imageColorSpace = surfaceFormat.colorSpace;
    createInfo.imageExtent = newExtent;
    createInfo.imageArrayLayers = 1;
    createInfo.imageUsage = VK_IMAGE_USAGE_COLOR_ATTACHMENT_BIT | VK_IMAGE_USAGE_TRANSFER_SRC_BIT;

    uint32_t queueFamilyIndices[] = {
        static_cast<uint32_t>(indices_.graphicsFamily),
        static_cast<uint32_t>(indices_.presentFamily)
    };

    if (indices_.graphicsFamily != indices_.presentFamily) {
        createInfo.imageSharingMode = VK_SHARING_MODE_CONCURRENT;
        createInfo.queueFamilyIndexCount = 2;
        createInfo.pQueueFamilyIndices = queueFamilyIndices;
    } else {
        createInfo.imageSharingMode = VK_SHARING_MODE_EXCLUSIVE;
    }

    createInfo.preTransform = support.capabilities.currentTransform;
    createInfo.compositeAlpha = VK_COMPOSITE_ALPHA_OPAQUE_BIT_KHR;
    createInfo.presentMode = presentMode;
    createInfo.clipped = VK_TRUE;
    // Chain the outgoing swapchain: the presentation engine retires the old
    // presentable images as part of this call. VK_NULL_HANDLE only on the
    // very first create.
    createInfo.oldSwapchain = oldSwapchain;

    VkSwapchainKHR fresh = VK_NULL_HANDLE;
    if (vkCreateSwapchainKHR(device_, &createInfo, nullptr, &fresh) != VK_SUCCESS) {
        fprintf(stderr, "[VulkanSwapchain] vkCreateSwapchainKHR FAILED "
                        "(extent=%ux%u format=%d presentMode=%d)\n",
                newExtent.width, newExtent.height, static_cast<int>(newFormat),
                static_cast<int>(presentMode));
        // Leave every member untouched: the caller still owns a working
        // swapchain (if there was one) and must not be left half-updated.
        return false;
    }

    uint32_t actualCount = 0;
    vkGetSwapchainImagesKHR(device_, fresh, &actualCount, nullptr);
    std::vector<VkImage> freshImages(actualCount);
    vkGetSwapchainImagesKHR(device_, fresh, &actualCount, freshImages.data());

    // ---- Commit only now that creation succeeded ----
    swapchain_ = fresh;
    extent_ = newExtent;
    imageFormat_ = newFormat;
    images_ = std::move(freshImages);

    fprintf(stderr, "[VulkanSwapchain] created %ux%u images=%u format=%d presentMode=%d oldSwapchain=%p\n",
            extent_.width, extent_.height, actualCount, static_cast<int>(imageFormat_),
            static_cast<int>(presentMode), (void*)(uintptr_t)oldSwapchain);

    CreateImageViews();
    return true;
}

void VulkanSwapchain::Shutdown() {
    CleanupSwapchain();
}

bool VulkanSwapchain::Recreate(uint32_t width, uint32_t height,
                                 const SwapchainSupportDetails& support) {
    // 2) Keep the outgoing swapchain handle: it is handed to
    //    VkSwapchainCreateInfoKHR::oldSwapchain below and destroyed only
    //    AFTER the replacement exists (step 7). Destroying it first - which
    //    is what this function used to do - retires every presentable image
    //    before the presentation engine has a successor to flip to, which is
    //    what left the Qt viewport black after every resize.
    VkSwapchainKHR oldSwapchain = swapchain_;

    // The old image views alias the OLD swapchain's images, so they must be
    // released before that swapchain is retired.
    for (auto iv : imageViews_) vkDestroyImageView(device_, iv, nullptr);
    imageViews_.clear();

    std::vector<VkImage> oldImages = std::move(images_);
    images_.clear();

    // 3) Create the replacement, chained to the old handle.
    if (CreateSwapchain(width, height, support, oldSwapchain)) {
        // 7) New swapchain is live -> now, and only now, retire the old one.
        if (oldSwapchain != VK_NULL_HANDLE)
            vkDestroySwapchainKHR(device_, oldSwapchain, nullptr);
        fprintf(stderr, "[VulkanSwapchain] old swapchain %p retired\n",
                (void*)(uintptr_t)oldSwapchain);
        return true;
    }

    // Creation failed: put the previous swapchain back in service (handle,
    // images and views) so the viewport keeps presenting at the old size
    // instead of going permanently black.
    images_ = std::move(oldImages);
    CreateImageViews();
    fprintf(stderr, "[VulkanSwapchain] Recreate FAILED - keeping old swapchain %p\n",
            (void*)(uintptr_t)oldSwapchain);
    return false;
}

void VulkanSwapchain::CleanupSwapchain() {
    for (auto iv : imageViews_) vkDestroyImageView(device_, iv, nullptr);
    imageViews_.clear();
    images_.clear();

    if (swapchain_) {
        vkDestroySwapchainKHR(device_, swapchain_, nullptr);
        swapchain_ = VK_NULL_HANDLE;
    }
}

void VulkanSwapchain::CreateImageViews() {
    imageViews_.resize(images_.size());
    for (size_t i = 0; i < images_.size(); ++i) {
        VkImageViewCreateInfo createInfo{};
        createInfo.sType = VK_STRUCTURE_TYPE_IMAGE_VIEW_CREATE_INFO;
        createInfo.image = images_[i];
        createInfo.viewType = VK_IMAGE_VIEW_TYPE_2D;
        createInfo.format = imageFormat_;
        createInfo.components.r = VK_COMPONENT_SWIZZLE_IDENTITY;
        createInfo.components.g = VK_COMPONENT_SWIZZLE_IDENTITY;
        createInfo.components.b = VK_COMPONENT_SWIZZLE_IDENTITY;
        createInfo.components.a = VK_COMPONENT_SWIZZLE_IDENTITY;
        createInfo.subresourceRange.aspectMask = VK_IMAGE_ASPECT_COLOR_BIT;
        createInfo.subresourceRange.levelCount = 1;
        createInfo.subresourceRange.layerCount = 1;
        if (vkCreateImageView(device_, &createInfo, nullptr, &imageViews_[i]) != VK_SUCCESS) {
            fprintf(stderr, "[VulkanSwapchain] vkCreateImageView FAILED %zu\n", i);
        }
    }
}

VkResult VulkanSwapchain::AcquireNextImage(VkSemaphore signalSemaphore, uint32_t* imageIndex) {
    return vkAcquireNextImageKHR(device_, swapchain_, UINT64_MAX, signalSemaphore,
                                  VK_NULL_HANDLE, imageIndex);
}

VkResult VulkanSwapchain::Present(VkSemaphore waitSemaphore, uint32_t imageIndex) {
    VkPresentInfoKHR presentInfo{};
    presentInfo.sType = VK_STRUCTURE_TYPE_PRESENT_INFO_KHR;
    presentInfo.waitSemaphoreCount = 1;
    presentInfo.pWaitSemaphores = &waitSemaphore;
    presentInfo.swapchainCount = 1;
    presentInfo.pSwapchains = &swapchain_;
    presentInfo.pImageIndices = &imageIndex;
    return vkQueuePresentKHR(presentQueue_, &presentInfo);
}

} // namespace vulkan
} // namespace naksha
