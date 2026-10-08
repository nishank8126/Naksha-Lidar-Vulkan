#include "naksha/core/vulkan/VulkanCommandPool.h"

namespace naksha {
namespace vulkan {

VulkanCommandPool::~VulkanCommandPool() { Shutdown(); }

bool VulkanCommandPool::Initialize(VkDevice device, uint32_t queueFamilyIndex,
                                    bool allowIndividualReset) {
    device_ = device;

    VkCommandPoolCreateInfo poolInfo{};
    poolInfo.sType = VK_STRUCTURE_TYPE_COMMAND_POOL_CREATE_INFO;
    poolInfo.flags = allowIndividualReset ? VK_COMMAND_POOL_CREATE_RESET_COMMAND_BUFFER_BIT : 0;
    poolInfo.queueFamilyIndex = queueFamilyIndex;

    return vkCreateCommandPool(device_, &poolInfo, nullptr, &pool_) == VK_SUCCESS;
}

void VulkanCommandPool::Shutdown() {
    if (pool_) {
        vkDestroyCommandPool(device_, pool_, nullptr);
        pool_ = VK_NULL_HANDLE;
    }
}

VkCommandBuffer VulkanCommandPool::AllocateCommandBuffer(bool primary) {
    VkCommandBufferAllocateInfo allocInfo{};
    allocInfo.sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO;
    allocInfo.commandPool = pool_;
    allocInfo.level = primary ? VK_COMMAND_BUFFER_LEVEL_PRIMARY
                               : VK_COMMAND_BUFFER_LEVEL_SECONDARY;
    allocInfo.commandBufferCount = 1;

    VkCommandBuffer buffer;
    vkAllocateCommandBuffers(device_, &allocInfo, &buffer);
    return buffer;
}

void VulkanCommandPool::FreeCommandBuffer(VkCommandBuffer commandBuffer) {
    vkFreeCommandBuffers(device_, pool_, 1, &commandBuffer);
}

void VulkanCommandPool::Reset() {
    vkResetCommandPool(device_, pool_, 0);
}

} // namespace vulkan
} // namespace naksha
