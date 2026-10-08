#include "naksha/core/vulkan/VulkanPhysicalDevice.h"
#include <set>
#include <algorithm>

namespace naksha {
namespace vulkan {

VkPhysicalDevice VulkanPhysicalDevice::Choose(VkInstance instance, VkSurfaceKHR surface) {
    uint32_t deviceCount = 0;
    vkEnumeratePhysicalDevices(instance, &deviceCount, nullptr);
    if (deviceCount == 0) return VK_NULL_HANDLE;

    std::vector<VkPhysicalDevice> devices(deviceCount);
    vkEnumeratePhysicalDevices(instance, &deviceCount, devices.data());

    VkPhysicalDevice best = VK_NULL_HANDLE;
    int bestScore = -1;

    for (auto& dev : devices) {
        int score = ScoreDevice(dev, surface);
        if (score > bestScore) {
            bestScore = score;
            best = dev;
        }
    }

    if (best != VK_NULL_HANDLE) {
        physicalDevice_ = best;
        queueFamilies_ = FindQueueFamilies(best, surface);

        VkPhysicalDeviceProperties props;
        vkGetPhysicalDeviceProperties(best, &props);
        deviceName_ = props.deviceName;

        uint32_t extCount = 0;
        vkEnumerateDeviceExtensionProperties(best, nullptr, &extCount, nullptr);
        std::vector<VkExtensionProperties> exts(extCount);
        vkEnumerateDeviceExtensionProperties(best, nullptr, &extCount, exts.data());
        for (auto& e : exts) supportedExtensions_.push_back(e.extensionName);
    }
    return best;
}

int VulkanPhysicalDevice::ScoreDevice(VkPhysicalDevice device, VkSurfaceKHR surface) const {
    auto families = FindQueueFamilies(device, surface);
    if (!families.IsComplete()) return -1;

    VkPhysicalDeviceProperties props;
    vkGetPhysicalDeviceProperties(device, &props);

    int score = 0;
    if (props.deviceType == VK_PHYSICAL_DEVICE_TYPE_DISCRETE_GPU) score += 10000;
    score += static_cast<int>(props.limits.maxImageDimension2D);

    uint32_t extCount = 0;
    vkEnumerateDeviceExtensionProperties(device, nullptr, &extCount, nullptr);
    std::vector<VkExtensionProperties> exts(extCount);
    vkEnumerateDeviceExtensionProperties(device, nullptr, &extCount, exts.data());
    std::set<std::string> required = {VK_KHR_SWAPCHAIN_EXTENSION_NAME};
    for (auto& e : exts) required.erase(e.extensionName);
    if (!required.empty()) return -1;

    return score;
}

QueueFamilyIndices VulkanPhysicalDevice::FindQueueFamilies(
    VkPhysicalDevice device, VkSurfaceKHR surface) const {
    QueueFamilyIndices indices;
    uint32_t count = 0;
    vkGetPhysicalDeviceQueueFamilyProperties(device, &count, nullptr);
    std::vector<VkQueueFamilyProperties> families(count);
    vkGetPhysicalDeviceQueueFamilyProperties(device, &count, families.data());

    for (uint32_t i = 0; i < count; ++i) {
        if (families[i].queueFlags & VK_QUEUE_GRAPHICS_BIT) indices.graphicsFamily = i;
        if (families[i].queueFlags & VK_QUEUE_COMPUTE_BIT) indices.computeFamily = i;
        if ((families[i].queueFlags & VK_QUEUE_TRANSFER_BIT) &&
            !(families[i].queueFlags & VK_QUEUE_GRAPHICS_BIT)) {
            indices.transferFamily = i;
        }
        VkBool32 presentSupport = false;
        vkGetPhysicalDeviceSurfaceSupportKHR(device, i, surface, &presentSupport);
        if (presentSupport) indices.presentFamily = i;
        if (indices.IsComplete()) break;
    }
    if (indices.transferFamily < 0) indices.transferFamily = indices.graphicsFamily;
    return indices;
}

SwapchainSupportDetails VulkanPhysicalDevice::GetSwapchainSupport(
    VkSurfaceKHR surface) const {
    SwapchainSupportDetails details;
    vkGetPhysicalDeviceSurfaceCapabilitiesKHR(physicalDevice_, surface, &details.capabilities);
    uint32_t formatCount;
    vkGetPhysicalDeviceSurfaceFormatsKHR(physicalDevice_, surface, &formatCount, nullptr);
    if (formatCount) {
        details.formats.resize(formatCount);
        vkGetPhysicalDeviceSurfaceFormatsKHR(physicalDevice_, surface, &formatCount,
                                             details.formats.data());
    }
    uint32_t modeCount;
    vkGetPhysicalDeviceSurfacePresentModesKHR(physicalDevice_, surface, &modeCount, nullptr);
    if (modeCount) {
        details.presentModes.resize(modeCount);
        vkGetPhysicalDeviceSurfacePresentModesKHR(physicalDevice_, surface, &modeCount,
                                                  details.presentModes.data());
    }
    return details;
}

VkPhysicalDeviceProperties VulkanPhysicalDevice::GetProperties() const {
    VkPhysicalDeviceProperties props;
    vkGetPhysicalDeviceProperties(physicalDevice_, &props);
    return props;
}

VkPhysicalDeviceMemoryProperties VulkanPhysicalDevice::GetMemoryProperties() const {
    VkPhysicalDeviceMemoryProperties memProps;
    vkGetPhysicalDeviceMemoryProperties(physicalDevice_, &memProps);
    return memProps;
}

uint64_t VulkanPhysicalDevice::GetVRAMSize() const {
    auto memProps = GetMemoryProperties();
    uint64_t total = 0;
    for (uint32_t i = 0; i < memProps.memoryHeapCount; ++i) {
        if (memProps.memoryHeaps[i].flags & VK_MEMORY_HEAP_DEVICE_LOCAL_BIT) {
            total += memProps.memoryHeaps[i].size;
        }
    }
    return total;
}

bool VulkanPhysicalDevice::HasExtension(const char* extension) const {
    return std::find(supportedExtensions_.begin(), supportedExtensions_.end(), extension) !=
           supportedExtensions_.end();
}

} // namespace vulkan
} // namespace naksha
