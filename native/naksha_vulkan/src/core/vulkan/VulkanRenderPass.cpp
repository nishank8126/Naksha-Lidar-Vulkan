#include "naksha/core/vulkan/VulkanRenderPass.h"

namespace naksha {
namespace vulkan {

VulkanRenderPass::~VulkanRenderPass() { Shutdown(); }

bool VulkanRenderPass::Initialize(VkDevice device, const RenderPassConfig& config) {
    device_ = device;

    VkAttachmentDescription colorAttachment{};
    colorAttachment.format = config.colorFormat;
    colorAttachment.samples = config.colorSamples;
    colorAttachment.loadOp = config.loadColorClear ? VK_ATTACHMENT_LOAD_OP_CLEAR
                                                    : VK_ATTACHMENT_LOAD_OP_LOAD;
    colorAttachment.storeOp = config.storeColor ? VK_ATTACHMENT_STORE_OP_STORE
                                                 : VK_ATTACHMENT_STORE_OP_DONT_CARE;
    colorAttachment.stencilLoadOp = VK_ATTACHMENT_LOAD_OP_DONT_CARE;
    colorAttachment.stencilStoreOp = VK_ATTACHMENT_STORE_OP_DONT_CARE;
    colorAttachment.initialLayout = VK_IMAGE_LAYOUT_UNDEFINED;
    colorAttachment.finalLayout = config.colorFinalLayout;

    VkAttachmentDescription depthAttachment{};
    depthAttachment.format = config.depthFormat;
    depthAttachment.samples = config.depthSamples;
    depthAttachment.loadOp = config.depthLoadOp;
    depthAttachment.storeOp = config.storeDepth ? VK_ATTACHMENT_STORE_OP_STORE
                                                 : VK_ATTACHMENT_STORE_OP_DONT_CARE;
    depthAttachment.stencilLoadOp = VK_ATTACHMENT_LOAD_OP_DONT_CARE;
    depthAttachment.stencilStoreOp = VK_ATTACHMENT_STORE_OP_DONT_CARE;
    depthAttachment.initialLayout = VK_IMAGE_LAYOUT_UNDEFINED;
    depthAttachment.finalLayout = config.depthFinalLayout;

    VkAttachmentReference colorRef{};
    colorRef.attachment = 0;
    colorRef.layout = VK_IMAGE_LAYOUT_COLOR_ATTACHMENT_OPTIMAL;

    // Optional second colour attachment at location 1 (Instant Shaded splat).
    const bool hasColor2 = config.color2Format != VK_FORMAT_UNDEFINED;
    VkAttachmentDescription color2Attachment{};
    VkAttachmentReference color2Ref{};
    if (hasColor2) {
        color2Attachment.format = config.color2Format;
        color2Attachment.samples = config.colorSamples;
        color2Attachment.loadOp = VK_ATTACHMENT_LOAD_OP_CLEAR;
        color2Attachment.storeOp = VK_ATTACHMENT_STORE_OP_STORE;
        color2Attachment.stencilLoadOp = VK_ATTACHMENT_LOAD_OP_DONT_CARE;
        color2Attachment.stencilStoreOp = VK_ATTACHMENT_STORE_OP_DONT_CARE;
        color2Attachment.initialLayout = VK_IMAGE_LAYOUT_UNDEFINED;
        color2Attachment.finalLayout = config.color2FinalLayout;
        color2Ref.attachment = 1;
        color2Ref.layout = VK_IMAGE_LAYOUT_COLOR_ATTACHMENT_OPTIMAL;
    }

    VkAttachmentReference depthRef{};
    depthRef.attachment = hasColor2 ? 2 : 1;
    depthRef.layout = VK_IMAGE_LAYOUT_DEPTH_STENCIL_ATTACHMENT_OPTIMAL;

    VkSubpassDescription subpass{};
    subpass.pipelineBindPoint = VK_PIPELINE_BIND_POINT_GRAPHICS;
    subpass.colorAttachmentCount = hasColor2 ? 2u : 1u;
    // MUST outlive the vkCreateRenderPass call below: the subpass keeps this
    // pointer, so a block-scoped array leaves it dangling and the driver reads
    // garbage (which is exactly how the first build of the 2-attachment instant
    // pass crashed with an access violation).
    VkAttachmentReference colorRefs[2] = {colorRef, color2Ref};
    subpass.pColorAttachments = hasColor2 ? colorRefs : &colorRef;
    subpass.pDepthStencilAttachment = &depthRef;

    VkSubpassDependency dependency{};
    dependency.srcSubpass = VK_SUBPASS_EXTERNAL;
    dependency.dstSubpass = 0;
    dependency.srcStageMask = VK_PIPELINE_STAGE_COLOR_ATTACHMENT_OUTPUT_BIT |
                               VK_PIPELINE_STAGE_EARLY_FRAGMENT_TESTS_BIT;
    dependency.srcAccessMask = 0;
    dependency.dstStageMask = VK_PIPELINE_STAGE_COLOR_ATTACHMENT_OUTPUT_BIT |
                               VK_PIPELINE_STAGE_EARLY_FRAGMENT_TESTS_BIT;
    dependency.dstAccessMask = VK_ACCESS_COLOR_ATTACHMENT_WRITE_BIT |
                                VK_ACCESS_DEPTH_STENCIL_ATTACHMENT_WRITE_BIT;

    std::vector<VkAttachmentDescription> attachments = {colorAttachment};
    if (hasColor2) attachments.push_back(color2Attachment);
    attachments.push_back(depthAttachment);

    VkRenderPassCreateInfo renderPassInfo{};
    renderPassInfo.sType = VK_STRUCTURE_TYPE_RENDER_PASS_CREATE_INFO;
    renderPassInfo.attachmentCount = static_cast<uint32_t>(attachments.size());
    renderPassInfo.pAttachments = attachments.data();
    renderPassInfo.subpassCount = 1;
    renderPassInfo.pSubpasses = &subpass;
    renderPassInfo.dependencyCount = 1;
    renderPassInfo.pDependencies = &dependency;

    return vkCreateRenderPass(device_, &renderPassInfo, nullptr, &renderPass_) == VK_SUCCESS;
}

void VulkanRenderPass::Shutdown() {
    if (renderPass_) {
        vkDestroyRenderPass(device_, renderPass_, nullptr);
        renderPass_ = VK_NULL_HANDLE;
    }
}

} // namespace vulkan
} // namespace naksha
