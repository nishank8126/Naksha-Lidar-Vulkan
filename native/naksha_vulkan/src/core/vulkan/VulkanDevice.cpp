#include "naksha/core/vulkan/VulkanDevice.h"
#include <set>
#include <vector>

namespace naksha {
namespace vulkan {

VulkanDevice::~VulkanDevice() { Shutdown(); }

bool VulkanDevice::Initialize(VkInstance instance, VkSurfaceKHR surface,
                               const VulkanDeviceConfig& config) {
    physicalDevice_.Choose(instance, surface);
    if (physicalDevice_.GetDevice() == VK_NULL_HANDLE) return false;

    auto indices = physicalDevice_.GetQueueFamilies();
    std::vector<VkDeviceQueueCreateInfo> queueInfos;
    std::set<uint32_t> uniqueFamilies = {
        static_cast<uint32_t>(indices.graphicsFamily),
        static_cast<uint32_t>(indices.presentFamily)
    };
    if (indices.computeFamily >= 0) uniqueFamilies.insert(indices.computeFamily);

    float queuePriority = 1.0f;
    for (uint32_t family : uniqueFamilies) {
        VkDeviceQueueCreateInfo queueInfo{};
        queueInfo.sType = VK_STRUCTURE_TYPE_DEVICE_QUEUE_CREATE_INFO;
        queueInfo.queueFamilyIndex = family;
        queueInfo.queueCount = 1;
        queueInfo.pQueuePriorities = &queuePriority;
        queueInfos.push_back(queueInfo);
    }

    std::vector<const char*> deviceExtensions = {VK_KHR_SWAPCHAIN_EXTENSION_NAME};
    for (auto& ext : config.requiredExtensions) deviceExtensions.push_back(ext);

    VkPhysicalDeviceFeatures supportedFeatures{};
    vkGetPhysicalDeviceFeatures(physicalDevice_.GetDevice(), &supportedFeatures);

    VkPhysicalDeviceFeatures2 deviceFeatures{};
    deviceFeatures.sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_FEATURES_2;
    VkPhysicalDeviceFeatures features{};
    // Only ever enable features the physical device actually supports;
    // enabling an unsupported feature fails device creation outright.
    features.samplerAnisotropy =
        (config.enableAnisotropy && supportedFeatures.samplerAnisotropy) ? VK_TRUE : VK_FALSE;
    // Naksha fix: original code forced wideLines = VK_TRUE unconditionally.
    // The point renderer does not need wide lines; gate it on support.
    features.wideLines = supportedFeatures.wideLines ? VK_TRUE : VK_FALSE;
    features.largePoints = supportedFeatures.largePoints ? VK_TRUE : VK_FALSE;
    // Fragment PrimitiveId in the indexed, flat-cell Surface pipeline.
    features.geometryShader = supportedFeatures.geometryShader;
    // Needed by the SurfaceRenderer wireframe pipelines (VK_POLYGON_MODE_LINE).
    features.fillModeNonSolid = supportedFeatures.fillModeNonSolid ? VK_TRUE : VK_FALSE;
    deviceFeatures.features = features;

    // hostQueryReset lets the swapchain-recreation path reset the timestamp
    // query pool from the host (vkResetQueryPool) instead of having to spin
    // up a throwaway command buffer. Without it every resize produced
    //   "vkResetQueryPool(): hostQueryReset feature was not enabled"
    // (VUID-vkResetQueryPool-None-02665). Query it first: enabling an
    // unsupported feature would fail device creation outright.
    VkPhysicalDeviceHostQueryResetFeatures hostQueryReset{
        VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_HOST_QUERY_RESET_FEATURES};
    VkPhysicalDeviceHostQueryResetFeatures hostQueryResetSupported{
        VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_HOST_QUERY_RESET_FEATURES};
    VkPhysicalDeviceFeatures2 querySupport{
        VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_FEATURES_2};
    querySupport.pNext = &hostQueryResetSupported;
    vkGetPhysicalDeviceFeatures2(physicalDevice_.GetDevice(), &querySupport);
    hostQueryReset.hostQueryReset = hostQueryResetSupported.hostQueryReset;
    hostQueryReset.pNext = deviceFeatures.pNext;
    deviceFeatures.pNext = &hostQueryReset;

    // The built-in shaders declare SPIR-V capability DemoteToHelperInvocation
    // (GLSL discard). The device must enable shaderDemoteToHelperInvocation or
    // every shader-module load raises
    //   VUID-VkShaderModuleCreateInfo-pCode-08740
    // (a validation ERROR reported at vkCreateShaderModule time). This is a
    // device feature, not a shader change.
    VkPhysicalDeviceVulkan13Features vulkan13{
        VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_VULKAN_1_3_FEATURES};
    VkPhysicalDeviceVulkan13Features vulkan13Supported{
        VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_VULKAN_1_3_FEATURES};
    querySupport.pNext = &vulkan13Supported;
    vkGetPhysicalDeviceFeatures2(physicalDevice_.GetDevice(), &querySupport);
    vulkan13.shaderDemoteToHelperInvocation =
        vulkan13Supported.shaderDemoteToHelperInvocation;
    vulkan13.pNext = deviceFeatures.pNext;
    deviceFeatures.pNext = &vulkan13;

    fprintf(stderr, "[DEVICE FEATURES]\n");
    fprintf(stderr, "  largePoints:       supported=%s enabled=%s\n",
            supportedFeatures.largePoints ? "YES" : "NO", features.largePoints ? "YES" : "NO");
    fprintf(stderr, "  wideLines:         supported=%s enabled=%s\n",
            supportedFeatures.wideLines ? "YES" : "NO", features.wideLines ? "YES" : "NO");
    fprintf(stderr, "  fillModeNonSolid:  supported=%s enabled=%s\n",
            supportedFeatures.fillModeNonSolid ? "YES" : "NO",
            features.fillModeNonSolid ? "YES" : "NO");
    fprintf(stderr, "  samplerAnisotropy: supported=%s enabled=%s\n",
            supportedFeatures.samplerAnisotropy ? "YES" : "NO",
            features.samplerAnisotropy ? "YES" : "NO");
    fprintf(stderr, "  hostQueryReset:    supported=%s enabled=%s\n",
            hostQueryResetSupported.hostQueryReset ? "YES" : "NO",
            hostQueryReset.hostQueryReset ? "YES" : "NO");
    fprintf(stderr, "  shaderDemote:      supported=%s enabled=%s\n",
            vulkan13Supported.shaderDemoteToHelperInvocation ? "YES" : "NO",
            vulkan13.shaderDemoteToHelperInvocation ? "YES" : "NO");

    VkDeviceCreateInfo createInfo{};
    createInfo.sType = VK_STRUCTURE_TYPE_DEVICE_CREATE_INFO;
    createInfo.pNext = &deviceFeatures;
    createInfo.queueCreateInfoCount = static_cast<uint32_t>(queueInfos.size());
    createInfo.pQueueCreateInfos = queueInfos.data();
    createInfo.enabledExtensionCount = static_cast<uint32_t>(deviceExtensions.size());
    createInfo.ppEnabledExtensionNames = deviceExtensions.data();
    createInfo.pEnabledFeatures = nullptr;

    fprintf(stderr, "[DEVICE] creating logical device...\n");
    if (vkCreateDevice(physicalDevice_.GetDevice(), &createInfo, nullptr, &device_) != VK_SUCCESS) {
        fprintf(stderr, "[DEVICE] vkCreateDevice FAILED\n");
        return false;
    }
    fprintf(stderr, "[DEVICE] vkCreateDevice OK\n");
    volkLoadDevice(device_);
    fprintf(stderr, "[DEVICE] volkLoadDevice OK\n");

    enabledFeatures_ = features;
    vkGetDeviceQueue(device_, indices.graphicsFamily, 0, &graphicsQueue_);
    vkGetDeviceQueue(device_, indices.presentFamily, 0, &presentQueue_);
    if (indices.computeFamily >= 0)
        vkGetDeviceQueue(device_, indices.computeFamily, 0, &computeQueue_);
    else
        computeQueue_ = graphicsQueue_;
    if (indices.transferFamily >= 0)
        vkGetDeviceQueue(device_, indices.transferFamily, 0, &transferQueue_);
    else
        transferQueue_ = graphicsQueue_;

    return true;
}

void VulkanDevice::Shutdown() {
    if (device_) {
        vkDestroyDevice(device_, nullptr);
        device_ = VK_NULL_HANDLE;
    }
}

} // namespace vulkan
} // namespace naksha
