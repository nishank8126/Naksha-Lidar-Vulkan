#pragma once
#include "naksha/core/vulkan/VulkanHeaders.h"

namespace naksha {
namespace vulkan {

struct DescriptorBinding {
    uint32_t binding = 0;
    VkDescriptorType type = VK_DESCRIPTOR_TYPE_UNIFORM_BUFFER;
    uint32_t count = 1;
    VkShaderStageFlags stageFlags = VK_SHADER_STAGE_VERTEX_BIT;
};

class VulkanDescriptorManager {
public:
    ~VulkanDescriptorManager();

    bool Initialize(VkDevice device, uint32_t maxSets = 256);
    void Shutdown();

    VkDescriptorSetLayout CreateLayout(const std::vector<DescriptorBinding>& bindings);
    void DestroyLayout(VkDescriptorSetLayout layout);

    VkDescriptorPool CreatePool(const std::vector<VkDescriptorPoolSize>& poolSizes,
                                uint32_t maxSets);
    void DestroyPool(VkDescriptorPool pool);

    VkDescriptorSet AllocateSet(VkDescriptorPool pool, VkDescriptorSetLayout layout);
    void UpdateBuffer(VkDescriptorSet set, uint32_t binding,
                      VkBuffer buffer, VkDeviceSize size,
                      VkDescriptorType type = VK_DESCRIPTOR_TYPE_UNIFORM_BUFFER);
    void UpdateImage(VkDescriptorSet set, uint32_t binding,
                     VkImageView imageView, VkSampler sampler,
                     VkDescriptorType type = VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER);

    void FreeSets(VkDescriptorPool pool, const std::vector<VkDescriptorSet>& sets);

private:
    VkDevice device_ = VK_NULL_HANDLE;
    uint32_t maxSets_ = 256;
};

} // namespace vulkan
} // namespace naksha
