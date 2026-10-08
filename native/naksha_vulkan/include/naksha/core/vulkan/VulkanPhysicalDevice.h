#pragma once
#include "naksha/core/vulkan/VulkanHeaders.h"

namespace naksha {
namespace vulkan {

struct QueueFamilyIndices {
    int32_t graphicsFamily = -1;
    int32_t presentFamily = -1;
    int32_t computeFamily = -1;
    int32_t transferFamily = -1;

    bool IsComplete() const {
        return graphicsFamily >= 0 && presentFamily >= 0;
    }
    bool HasCompute() const { return computeFamily >= 0; }
    bool HasTransfer() const { return transferFamily >= 0; }
};

struct SwapchainSupportDetails {
    VkSurfaceCapabilitiesKHR capabilities = {};
    std::vector<VkSurfaceFormatKHR> formats;
    std::vector<VkPresentModeKHR> presentModes;
};

class VulkanPhysicalDevice {
public:
    VkPhysicalDevice Choose(VkInstance instance, VkSurfaceKHR surface);
    VkPhysicalDevice GetDevice() const { return physicalDevice_; }
    QueueFamilyIndices GetQueueFamilies() const { return queueFamilies_; }
    SwapchainSupportDetails GetSwapchainSupport(VkSurfaceKHR surface) const;
    VkPhysicalDeviceProperties GetProperties() const;
    VkPhysicalDeviceMemoryProperties GetMemoryProperties() const;
    const std::string& GetDeviceName() const { return deviceName_; }
    uint64_t GetVRAMSize() const;

    bool HasExtension(const char* extension) const;

private:
    VkPhysicalDevice physicalDevice_ = VK_NULL_HANDLE;
    QueueFamilyIndices queueFamilies_;
    std::string deviceName_;
    std::vector<const char*> supportedExtensions_;

    int ScoreDevice(VkPhysicalDevice device, VkSurfaceKHR surface) const;
    QueueFamilyIndices FindQueueFamilies(VkPhysicalDevice device, VkSurfaceKHR surface) const;
};

} // namespace vulkan
} // namespace naksha
