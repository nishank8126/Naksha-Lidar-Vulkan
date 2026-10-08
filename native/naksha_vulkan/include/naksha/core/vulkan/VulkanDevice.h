#pragma once
#include "naksha/core/vulkan/VulkanHeaders.h"
#include "naksha/core/vulkan/VulkanPhysicalDevice.h"

namespace naksha {
namespace vulkan {

struct VulkanDeviceConfig {
    bool enableAnisotropy = true;
    float maxAnisotropy = 16.0f;
    std::vector<const char*> requiredExtensions;
};

class VulkanDevice {
public:
    ~VulkanDevice();

    bool Initialize(VkInstance instance, VkSurfaceKHR surface,
                    const VulkanDeviceConfig& config = {});
    void Shutdown();

    VkDevice GetDevice() const { return device_; }
    VkPhysicalDevice GetPhysicalDevice() const { return physicalDevice_.GetDevice(); }
    VkQueue GetGraphicsQueue() const { return graphicsQueue_; }
    VkQueue GetPresentQueue() const { return presentQueue_; }
    VkQueue GetComputeQueue() const { return computeQueue_; }
    VkQueue GetTransferQueue() const { return transferQueue_; }
    QueueFamilyIndices GetQueueFamilies() const { return physicalDevice_.GetQueueFamilies(); }
    const VulkanPhysicalDevice& GetPhysicalDeviceInfo() const { return physicalDevice_; }

    // Features actually enabled by device creation (subset of supported).
    const VkPhysicalDeviceFeatures& GetEnabledFeatures() const { return enabledFeatures_; }

    void WaitIdle() const { vkDeviceWaitIdle(device_); }

private:
    VkDevice device_ = VK_NULL_HANDLE;
    VkQueue graphicsQueue_ = VK_NULL_HANDLE;
    VkQueue presentQueue_ = VK_NULL_HANDLE;
    VkQueue computeQueue_ = VK_NULL_HANDLE;
    VkQueue transferQueue_ = VK_NULL_HANDLE;
    VulkanPhysicalDevice physicalDevice_;
    VkPhysicalDeviceFeatures enabledFeatures_{};
};

} // namespace vulkan
} // namespace naksha
