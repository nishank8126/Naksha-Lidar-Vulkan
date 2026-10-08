#include "naksha/core/vulkan/VulkanDescriptorManager.h"

namespace naksha {
namespace vulkan {

VulkanDescriptorManager::~VulkanDescriptorManager() { Shutdown(); }

bool VulkanDescriptorManager::Initialize(VkDevice device, uint32_t maxSets) {
    device_ = device;
    maxSets_ = maxSets;
    return true;
}

void VulkanDescriptorManager::Shutdown() {
    device_ = VK_NULL_HANDLE;
}

VkDescriptorSetLayout VulkanDescriptorManager::CreateLayout(
    const std::vector<DescriptorBinding>& bindings) {
    std::vector<VkDescriptorSetLayoutBinding> layoutBindings;
    for (auto& b : bindings) {
        VkDescriptorSetLayoutBinding lb{};
        lb.binding = b.binding;
        lb.descriptorType = b.type;
        lb.descriptorCount = b.count;
        lb.stageFlags = b.stageFlags;
        layoutBindings.push_back(lb);
    }

    VkDescriptorSetLayoutCreateInfo createInfo{};
    createInfo.sType = VK_STRUCTURE_TYPE_DESCRIPTOR_SET_LAYOUT_CREATE_INFO;
    createInfo.bindingCount = static_cast<uint32_t>(layoutBindings.size());
    createInfo.pBindings = layoutBindings.data();

    VkDescriptorSetLayout layout;
    vkCreateDescriptorSetLayout(device_, &createInfo, nullptr, &layout);
    return layout;
}

void VulkanDescriptorManager::DestroyLayout(VkDescriptorSetLayout layout) {
    if (layout) vkDestroyDescriptorSetLayout(device_, layout, nullptr);
}

VkDescriptorPool VulkanDescriptorManager::CreatePool(
    const std::vector<VkDescriptorPoolSize>& poolSizes, uint32_t maxSets) {
    VkDescriptorPoolCreateInfo poolInfo{};
    poolInfo.sType = VK_STRUCTURE_TYPE_DESCRIPTOR_POOL_CREATE_INFO;
    // Renderer::DestroyFrameResources() calls FreeSets() on individual sets
    // (e.g. during swapchain recreation), which requires this flag
    // (VUID-vkFreeDescriptorSets-descriptorPool-00312).
    poolInfo.flags = VK_DESCRIPTOR_POOL_CREATE_FREE_DESCRIPTOR_SET_BIT;
    poolInfo.poolSizeCount = static_cast<uint32_t>(poolSizes.size());
    poolInfo.pPoolSizes = poolSizes.data();
    poolInfo.maxSets = maxSets;

    VkDescriptorPool pool;
    vkCreateDescriptorPool(device_, &poolInfo, nullptr, &pool);
    return pool;
}

void VulkanDescriptorManager::DestroyPool(VkDescriptorPool pool) {
    if (pool) vkDestroyDescriptorPool(device_, pool, nullptr);
}

VkDescriptorSet VulkanDescriptorManager::AllocateSet(VkDescriptorPool pool,
                                                       VkDescriptorSetLayout layout) {
    VkDescriptorSetAllocateInfo allocInfo{};
    allocInfo.sType = VK_STRUCTURE_TYPE_DESCRIPTOR_SET_ALLOCATE_INFO;
    allocInfo.descriptorPool = pool;
    allocInfo.descriptorSetCount = 1;
    allocInfo.pSetLayouts = &layout;

    VkDescriptorSet set;
    vkAllocateDescriptorSets(device_, &allocInfo, &set);
    return set;
}

void VulkanDescriptorManager::UpdateBuffer(VkDescriptorSet set, uint32_t binding,
                                            VkBuffer buffer, VkDeviceSize size,
                                            VkDescriptorType type) {
    VkDescriptorBufferInfo bufferInfo{};
    bufferInfo.buffer = buffer;
    bufferInfo.offset = 0;
    bufferInfo.range = size;

    VkWriteDescriptorSet write{};
    write.sType = VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET;
    write.dstSet = set;
    write.dstBinding = binding;
    write.dstArrayElement = 0;
    write.descriptorType = type;
    write.descriptorCount = 1;
    write.pBufferInfo = &bufferInfo;
    vkUpdateDescriptorSets(device_, 1, &write, 0, nullptr);
}

void VulkanDescriptorManager::UpdateImage(VkDescriptorSet set, uint32_t binding,
                                           VkImageView imageView, VkSampler sampler,
                                           VkDescriptorType type) {
    VkDescriptorImageInfo imageInfo{};
    imageInfo.imageLayout = VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL;
    imageInfo.imageView = imageView;
    imageInfo.sampler = sampler;

    VkWriteDescriptorSet write{};
    write.sType = VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET;
    write.dstSet = set;
    write.dstBinding = binding;
    write.dstArrayElement = 0;
    write.descriptorType = type;
    write.descriptorCount = 1;
    write.pImageInfo = &imageInfo;
    vkUpdateDescriptorSets(device_, 1, &write, 0, nullptr);
}

void VulkanDescriptorManager::FreeSets(VkDescriptorPool pool,
                                        const std::vector<VkDescriptorSet>& sets) {
    if (!sets.empty())
        vkFreeDescriptorSets(device_, pool, static_cast<uint32_t>(sets.size()), sets.data());
}

} // namespace vulkan
} // namespace naksha
