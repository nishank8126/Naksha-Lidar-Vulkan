#pragma once
#include "naksha/core/vulkan/VulkanHeaders.h"

namespace naksha {
namespace vulkan {

struct ShaderModule {
    VkShaderModule module = VK_NULL_HANDLE;
    VkShaderStageFlagBits stage = VK_SHADER_STAGE_VERTEX_BIT;
    std::string entryPoint = "main";
};

struct PipelineConfig {
    VkPipelineVertexInputStateCreateInfo vertexInput = {};
    VkPipelineInputAssemblyStateCreateInfo inputAssembly = {};
    VkPipelineRasterizationStateCreateInfo rasterizer = {};
    VkPipelineMultisampleStateCreateInfo multisampling = {};
    VkPipelineDepthStencilStateCreateInfo depthStencil = {};
    VkPipelineColorBlendAttachmentState colorBlendAttachment = {};
    VkPipelineColorBlendStateCreateInfo colorBlending = {};
    VkPipelineDynamicStateCreateInfo dynamicState = {};
    std::vector<VkDynamicState> dynamicStates;
    std::vector<VkVertexInputBindingDescription> vertexBindings;
    std::vector<VkVertexInputAttributeDescription> vertexAttributes;

    VkFormat colorFormat = VK_FORMAT_UNDEFINED;
    VkFormat depthFormat = VK_FORMAT_UNDEFINED;
    VkSampleCountFlagBits sampleCount = VK_SAMPLE_COUNT_1_BIT;

    void SetDefaults();
};

class VulkanPipelineManager {
public:
    ~VulkanPipelineManager();

    bool Initialize(VkDevice device);
    void Shutdown();

    VkPipelineLayout CreatePipelineLayout(
        const std::vector<VkDescriptorSetLayout>& setLayouts,
        const std::vector<VkPushConstantRange>& pushConstants = {});

    VkPipeline CreateGraphicsPipeline(
        VkPipelineLayout layout,
        const std::vector<ShaderModule>& shaders,
        const PipelineConfig& config,
        VkRenderPass renderPass,
        uint32_t subpass = 0);

    VkPipeline CreateComputePipeline(
        VkPipelineLayout layout,
        const ShaderModule& computeShader);

    void DestroyPipeline(VkPipeline pipeline);
    void DestroyPipelineLayout(VkPipelineLayout layout);

private:
    VkDevice device_ = VK_NULL_HANDLE;
};

} // namespace vulkan
} // namespace naksha
