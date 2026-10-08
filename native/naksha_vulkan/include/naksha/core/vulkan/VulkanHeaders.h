#pragma once

// VK_USE_PLATFORM_WIN32_KHR is defined project-wide via the volk CMake
// target (see CMakeLists.txt) so every TU that links volk agrees on it.
#include <volk.h>

#define VMA_STATIC_VULKAN_FUNCTIONS 0
#define VMA_DYNAMIC_VULKAN_FUNCTIONS 1
#include <vk_mem_alloc.h>

// NOTE (Naksha extraction): SDL3 includes removed. The core never touches a
// window system; instance extensions are supplied externally through
// VulkanInstanceConfig::requiredExtensions, and surfaces arrive as VkSurfaceKHR.

#include <vector>
#include <string>
#include <memory>
#include <functional>
#include <unordered_map>
#include <cassert>
#include <stdexcept>
#include <algorithm>
#include <cstring>

namespace naksha {
namespace vulkan {

inline uint32_t FindMemoryType(VkPhysicalDevice physicalDevice,
                               uint32_t typeFilter,
                               VkMemoryPropertyFlags properties) {
    VkPhysicalDeviceMemoryProperties memProperties;
    vkGetPhysicalDeviceMemoryProperties(physicalDevice, &memProperties);
    for (uint32_t i = 0; i < memProperties.memoryTypeCount; ++i) {
        if ((typeFilter & (1 << i)) &&
            (memProperties.memoryTypes[i].propertyFlags & properties) == properties) {
            return i;
        }
    }
    throw std::runtime_error("Vulkan: failed to find suitable memory type");
}

inline VkFormat ChooseDepthFormat(VkPhysicalDevice physicalDevice) {
    std::vector<VkFormat> candidates = {
        VK_FORMAT_D32_SFLOAT,
        VK_FORMAT_D32_SFLOAT_S8_UINT,
        VK_FORMAT_D24_UNORM_S8_UINT
    };
    for (VkFormat format : candidates) {
        VkFormatProperties props;
        vkGetPhysicalDeviceFormatProperties(physicalDevice, format, &props);
        if (props.optimalTilingFeatures & VK_FORMAT_FEATURE_DEPTH_STENCIL_ATTACHMENT_BIT) {
            return format;
        }
    }
    return VK_FORMAT_D32_SFLOAT;
}

} // namespace vulkan
} // namespace naksha
