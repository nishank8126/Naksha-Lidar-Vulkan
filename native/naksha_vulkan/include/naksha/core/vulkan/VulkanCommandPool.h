#pragma once
#include "naksha/core/vulkan/VulkanHeaders.h"

namespace naksha {
namespace vulkan {

class VulkanCommandPool {
public:
    ~VulkanCommandPool();

    bool Initialize(VkDevice device, uint32_t queueFamilyIndex,
                    bool allowIndividualReset = true);
    void Shutdown();

    VkCommandPool GetPool() const { return pool_; }

    VkCommandBuffer AllocateCommandBuffer(bool primary = true);
    void FreeCommandBuffer(VkCommandBuffer commandBuffer);
    void Reset();

private:
    VkDevice device_ = VK_NULL_HANDLE;
    VkCommandPool pool_ = VK_NULL_HANDLE;
};

} // namespace vulkan
} // namespace naksha
