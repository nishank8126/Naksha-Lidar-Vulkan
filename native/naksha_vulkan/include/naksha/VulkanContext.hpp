#pragma once
#include <string>
#include <vector>

#include "naksha/core/vulkan/VulkanHeaders.h"
#include "naksha/core/vulkan/VulkanInstance.h"
#include "naksha/core/vulkan/VulkanDevice.h"
#include "naksha/core/vulkan/VulkanPhysicalDevice.h"

namespace naksha {
using namespace vulkan;

struct ContextConfig {
    bool enableValidation = false;
    // Pick one surface source:
    void* nativeWindowHandle = nullptr;   // HWND -> engine creates Win32 surface
    VkSurfaceKHR externalSurface = VK_NULL_HANDLE;  // or supply one (Qt later)
    std::string appName = "NakshaVulkanEngine";
    uint32_t appVersion = VK_MAKE_VERSION(1, 0, 0);
};

// Everything a benchmark/report needs to know about the device, captured at
// Initialize() time (spec section: device report).
struct DeviceReport {
    std::string deviceName;
    uint32_t vendorId = 0;
    uint32_t deviceId = 0;
    uint32_t apiVersion = 0;       // raw uint32 (decode with VK_API_VERSION_MAJOR...)
    uint32_t driverVersion = 0;
    VkPhysicalDeviceType deviceType = VK_PHYSICAL_DEVICE_TYPE_OTHER;
    uint64_t vramBytes = 0;        // largest DEVICE_LOCAL heap
    uint32_t graphicsFamily = 0;
    uint32_t presentFamily = 0;
    bool graphicsDedicated = false;
    uint64_t timestampPeriodNs = 0;      // nanoseconds per timestamp tick
    uint32_t timestampValidBits = 0;
    uint64_t minUniformBufferOffsetAlignment = 0;
    uint64_t nonCoherentAtomSize = 0;
    float maxPointSize = 0.0f;           // limits.pointSizeRange[1]
    // Features SUPPORTED by the device (what the renderer is allowed to use):
    VkBool32 fillModeNonSolid = VK_FALSE;
    VkBool32 largePoints = VK_FALSE;
    VkBool32 wideLines = VK_FALSE;
    VkBool32 samplerAnisotropy = VK_FALSE;
};

// ---------------------------------------------------------------------------
// Owns instance + platform surface + logical device + queue handles and the
// device report. The first phase of Renderer::Initialize().
// ---------------------------------------------------------------------------
class VulkanContext {
public:
    bool Initialize(const ContextConfig& config);
    void Shutdown();
    bool IsInitialized() const { return initialized_; }

    VkInstance GetInstance() const;
    VkPhysicalDevice GetPhysicalDevice() const;
    VkDevice GetDevice() const;
    VkSurfaceKHR GetSurface() const;
    VkQueue GetGraphicsQueue() const;
    VkQueue GetPresentQueue() const;
    QueueFamilyIndices GetQueueFamilies() const;
    SwapchainSupportDetails QuerySwapchainSupport() const;

    const DeviceReport& GetReport() const { return report_; }

    vulkan::VulkanInstance& CoreInstance() { return instance_; }
    vulkan::VulkanDevice& CoreDevice() { return device_; }

    // Validation-layer diagnostics (0/0 when validation is disabled).
    int GetValidationErrorCount() const;
    int GetValidationWarningCount() const;

    // Human-readable device + limits + validation summary (stdout).
    void PrintDiagnostics() const;

private:
    vulkan::VulkanInstance instance_;
    vulkan::VulkanDevice device_;
    VkSurfaceKHR surface_ = VK_NULL_HANDLE;
    bool ownsSurface_ = false;
    bool initialized_ = false;
    DeviceReport report_{};

    bool CreateWin32Surface(void* hwnd);
    void FillReport();
};

} // namespace naksha
