#pragma once
#include "naksha/core/vulkan/VulkanHeaders.h"

namespace naksha {
namespace vulkan {

struct RenderPassConfig {
    VkFormat colorFormat = VK_FORMAT_UNDEFINED;
    VkFormat depthFormat = VK_FORMAT_UNDEFINED;
    VkSampleCountFlagBits colorSamples = VK_SAMPLE_COUNT_1_BIT;
    VkSampleCountFlagBits depthSamples = VK_SAMPLE_COUNT_1_BIT;
    bool loadColorClear = true;
    bool storeColor = true;
    bool loadDepthClear = true;
    bool storeDepth = false;
    VkAttachmentLoadOp depthLoadOp = VK_ATTACHMENT_LOAD_OP_CLEAR;
    // Final layouts. The defaults are the historic behaviour this engine was
    // built on: the colour attachment is the swapchain and goes straight to
    // PRESENT_SRC_KHR, depth stays in DEPTH_STENCIL_ATTACHMENT_OPTIMAL.
    //
    // An OFFSCREEN target that a later pass samples must NOT use those (an
    // image that is not a swapchain image cannot end in PRESENT_SRC, and
    // neither layout is readable by a shader). Such a pass overrides both to
    // the plain *_ATTACHMENT_OPTIMAL layouts and the recording code then
    // issues an explicit vkCmdPipelineBarrier to SHADER_READ_ONLY_OPTIMAL -
    // deliberately NOT finalLayout = SHADER_READ_ONLY, because a render-pass
    // final layout transition carries no shader-read memory dependency by
    // itself and would be a data race (the classic "undefined colour while
    // panning" bug).
    VkImageLayout colorFinalLayout = VK_IMAGE_LAYOUT_PRESENT_SRC_KHR;
    VkImageLayout depthFinalLayout = VK_IMAGE_LAYOUT_DEPTH_STENCIL_ATTACHMENT_OPTIMAL;
    // Optional SECOND colour attachment (location 1).
    //
    // VK_FORMAT_UNDEFINED means "none" and reproduces the historic single-
    // attachment render pass exactly, so every existing pipeline stays
    // compatible. It exists for the Instant Shaded SPLAT pass, which writes the
    // per-splat oct16x2 normal to attachment 1 alongside the class colour on
    // attachment 0. The lighting pass then reads that normal per pixel with a
    // NEAREST sampler, which is what keeps class colours and normals crisp -
    // there is no interpolation anywhere in the path.
    //
    // Layout transition rules mirror the primary attachment.
    VkFormat color2Format = VK_FORMAT_UNDEFINED;
    VkImageLayout color2FinalLayout = VK_IMAGE_LAYOUT_COLOR_ATTACHMENT_OPTIMAL;
};

class VulkanRenderPass {
public:
    ~VulkanRenderPass();

    bool Initialize(VkDevice device, const RenderPassConfig& config);
    void Shutdown();

    VkRenderPass GetRenderPass() const { return renderPass_; }

private:
    VkDevice device_ = VK_NULL_HANDLE;
    VkRenderPass renderPass_ = VK_NULL_HANDLE;
};

} // namespace vulkan
} // namespace naksha
