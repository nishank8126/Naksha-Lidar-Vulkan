#include "naksha/VulkanContext.hpp"
#include <cstdio>
#include <algorithm>
#include <windows.h>

namespace naksha {

static std::string ToUtf8(const std::wstring& s) {
    if (s.empty()) return {};
    int n = WideCharToMultiByte(CP_UTF8, 0, s.data(), static_cast<int>(s.size()),
                                nullptr, 0, nullptr, nullptr);
    if (n <= 0) return {};
    std::string out(n, '\0');
    WideCharToMultiByte(CP_UTF8, 0, s.data(), static_cast<int>(s.size()),
                        &out[0], n, nullptr, nullptr);
    return out;
}

static void DecodeApiVersion(uint32_t v, uint32_t& major, uint32_t& minor, uint32_t& patch) {
    major = VK_VERSION_MAJOR(v);
    minor = VK_VERSION_MINOR(v);
    patch = VK_VERSION_PATCH(v);
}

bool VulkanContext::Initialize(const ContextConfig& config) {
    fprintf(stderr, "[VulkanContext] Initializing...\n");

    vulkan::VulkanInstanceConfig icfg;
    icfg.enableValidation = config.enableValidation;
    icfg.appName = config.appName;
    icfg.appVersion = config.appVersion;
    icfg.requiredExtensions = {
        VK_KHR_SURFACE_EXTENSION_NAME,
        VK_KHR_WIN32_SURFACE_EXTENSION_NAME
    };
    if (config.enableValidation) {
        icfg.requiredExtensions.push_back(VK_EXT_DEBUG_UTILS_EXTENSION_NAME);
        icfg.requiredLayers = {"VK_LAYER_KHRONOS_validation"};
    }
    if (!instance_.Initialize(icfg)) {
        fprintf(stderr, "[VulkanContext] instance FAILED\n");
        return false;
    }
    fprintf(stderr, "[VulkanContext] instance OK\n");

    if (config.nativeWindowHandle) {
        if (!CreateWin32Surface(config.nativeWindowHandle)) {
            fprintf(stderr, "[VulkanContext] Win32 surface FAILED\n");
            return false;
        }
        ownsSurface_ = true;
    } else if (config.externalSurface != VK_NULL_HANDLE) {
        surface_ = config.externalSurface;
        ownsSurface_ = false;
    } else {
        fprintf(stderr, "[VulkanContext] no surface source provided\n");
        return false;
    }

    vulkan::VulkanDeviceConfig dcfg;
    dcfg.enableAnisotropy = true;
    dcfg.maxAnisotropy = 16.0f;
    if (!device_.Initialize(instance_.GetInstance(), surface_, dcfg)) {
        fprintf(stderr, "[VulkanContext] device FAILED\n");
        return false;
    }
    fprintf(stderr, "[VulkanContext] device OK\n");

    FillReport();
    fprintf(stderr, "[VulkanContext] FillReport OK\n");
    PrintDiagnostics();
    fprintf(stderr, "[VulkanContext] PrintDiagnostics OK\n");
    initialized_ = true;
    return true;
}

void VulkanContext::Shutdown() {
    if (!initialized_) return;
    initialized_ = false;
    PrintDiagnostics();

    // device_/instance_ destroy themselves via their own destructors when
    // this VulkanContext is eventually destroyed, but surface_ is a raw
    // VkSurfaceKHR with no owning object, and this function never destroyed
    // it - that's why vkDestroyInstance() reported "1 leaked objects"
    // (the VkSurfaceKHR) on every shutdown. Must run before the instance
    // is gone, so it goes here rather than relying on destruction order.
    if (ownsSurface_ && surface_ != VK_NULL_HANDLE && instance_.GetInstance() != VK_NULL_HANDLE) {
        vkDestroySurfaceKHR(instance_.GetInstance(), surface_, nullptr);
    }
    surface_ = VK_NULL_HANDLE;
    ownsSurface_ = false;

    device_.Shutdown();
    instance_.Shutdown();
}

bool VulkanContext::CreateWin32Surface(void* hwnd) {
    VkWin32SurfaceCreateInfoKHR ci{VK_STRUCTURE_TYPE_WIN32_SURFACE_CREATE_INFO_KHR};
    ci.hinstance = GetModuleHandleA(nullptr);
    ci.hwnd = reinterpret_cast<HWND>(hwnd);
    VkResult r = vkCreateWin32SurfaceKHR(instance_.GetInstance(), &ci, nullptr, &surface_);
    if (r != VK_SUCCESS) {
        fprintf(stderr, "[VulkanContext] vkCreateWin32SurfaceKHR FAILED %d\n", r);
        surface_ = VK_NULL_HANDLE;
        return false;
    }
    return true;
}

void VulkanContext::FillReport() {
    VkPhysicalDeviceProperties props{};
    vkGetPhysicalDeviceProperties(device_.GetPhysicalDevice(), &props);
    report_.deviceName = props.deviceName;
    report_.vendorId = props.vendorID;
    report_.deviceId = props.deviceID;
    report_.apiVersion = props.apiVersion;
    report_.driverVersion = props.driverVersion;
    report_.deviceType = props.deviceType;

    VkPhysicalDeviceMemoryProperties memProps{};
    vkGetPhysicalDeviceMemoryProperties(device_.GetPhysicalDevice(), &memProps);
    report_.vramBytes = 0;
    for (uint32_t i = 0; i < memProps.memoryHeapCount; ++i) {
        if (memProps.memoryHeaps[i].flags & VK_MEMORY_HEAP_DEVICE_LOCAL_BIT) {
            report_.vramBytes = std::max(report_.vramBytes, memProps.memoryHeaps[i].size);
        }
    }

    auto qf = device_.GetQueueFamilies();
    report_.graphicsFamily = static_cast<uint32_t>(qf.graphicsFamily);
    report_.presentFamily = static_cast<uint32_t>(qf.presentFamily);
    report_.graphicsDedicated = (qf.graphicsFamily != qf.presentFamily);

    VkPhysicalDeviceFeatures feats = device_.GetEnabledFeatures();
    report_.fillModeNonSolid = feats.fillModeNonSolid;
    report_.largePoints = feats.largePoints;
    report_.wideLines = feats.wideLines;
    report_.samplerAnisotropy = feats.samplerAnisotropy;

    report_.timestampPeriodNs = static_cast<double>(props.limits.timestampPeriod);
    report_.minUniformBufferOffsetAlignment = props.limits.minUniformBufferOffsetAlignment;
    report_.nonCoherentAtomSize = props.limits.nonCoherentAtomSize;
    report_.maxPointSize = props.limits.pointSizeRange[1];
}

VkInstance VulkanContext::GetInstance() const { return instance_.GetInstance(); }
VkPhysicalDevice VulkanContext::GetPhysicalDevice() const { return device_.GetPhysicalDevice(); }
VkDevice VulkanContext::GetDevice() const { return device_.GetDevice(); }
VkSurfaceKHR VulkanContext::GetSurface() const { return surface_; }
VkQueue VulkanContext::GetGraphicsQueue() const { return device_.GetGraphicsQueue(); }
VkQueue VulkanContext::GetPresentQueue() const { return device_.GetPresentQueue(); }
QueueFamilyIndices VulkanContext::GetQueueFamilies() const { return device_.GetQueueFamilies(); }
SwapchainSupportDetails VulkanContext::QuerySwapchainSupport() const {
    return device_.GetPhysicalDeviceInfo().GetSwapchainSupport(surface_);
}

int VulkanContext::GetValidationWarningCount() const {
    return VulkanInstance::GetValidationWarningCount();
}
int VulkanContext::GetValidationErrorCount() const {
    return VulkanInstance::GetValidationErrorCount();
}

void VulkanContext::PrintDiagnostics() const {
    uint32_t maj, min, patch;
    DecodeApiVersion(report_.apiVersion, maj, min, patch);
    fprintf(stderr,
            "[Device] %s\n"
            "  vendor %04x device %04x type %d api %u.%u.%u driver %u\n"
            "  vram %llu MB | timestampPeriod %.3f ns | validBits %u\n"
            "  minUBOAlign %llu nonCoherentAtom %llu maxPointSize %.1f\n"
            "  fillModeNonSolid %s largePoints %s wideLines %s anisotropy %s\n",
            report_.deviceName.c_str(), report_.vendorId, report_.deviceId,
            report_.deviceType, maj, min, patch, report_.driverVersion,
            static_cast<unsigned long long>(report_.vramBytes / (1024 * 1024)),
            report_.timestampPeriodNs, report_.timestampValidBits,
            static_cast<unsigned long long>(report_.minUniformBufferOffsetAlignment),
            static_cast<unsigned long long>(report_.nonCoherentAtomSize),
            report_.maxPointSize,
            report_.fillModeNonSolid ? "YES" : "NO",
            report_.largePoints ? "YES" : "NO",
            report_.wideLines ? "YES" : "NO",
            report_.samplerAnisotropy ? "YES" : "NO");
    if (report_.timestampValidBits == 0) {
        fprintf(stderr, "[VulkanContext] WARNING: GPU timestamps NOT available\n");
    }
}

} // namespace naksha
