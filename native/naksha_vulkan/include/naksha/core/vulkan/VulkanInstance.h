#pragma once
#include "naksha/core/vulkan/VulkanHeaders.h"
#include <atomic>
#include <cstdint>

namespace naksha {
namespace vulkan {

struct VulkanInstanceConfig {
    bool enableValidation = true;
    std::string appName = "NakshaPointEngine";
    uint32_t appVersion = VK_MAKE_VERSION(1, 0, 0);
    std::vector<std::string> requiredExtensions;
    std::vector<std::string> requiredLayers;
};

class VulkanInstance {
public:
    VulkanInstance() = default;
    ~VulkanInstance();

    VulkanInstance(const VulkanInstance&) = delete;
    VulkanInstance& operator=(const VulkanInstance&) = delete;

    bool Initialize(const VulkanInstanceConfig& config);
    void Shutdown();

    VkInstance GetInstance() const { return instance_; }
    VkDebugUtilsMessengerEXT GetDebugMessenger() const { return debugMessenger_; }

    // Validation-layer message counters (populated by DebugCallback when
    // validation is enabled). ResetValidationCounters() clears them.
    static int GetValidationErrorCount();
    static int GetValidationWarningCount();
    static void ResetValidationCounters();

private:
    VkInstance instance_ = VK_NULL_HANDLE;
    VkDebugUtilsMessengerEXT debugMessenger_ = VK_NULL_HANDLE;
    bool validationEnabled_ = false;

    static VKAPI_ATTR VkBool32 VKAPI_CALL DebugCallback(
        VkDebugUtilsMessageSeverityFlagBitsEXT severity,
        VkDebugUtilsMessageTypeFlagsEXT type,
        const VkDebugUtilsMessengerCallbackDataEXT* callbackData,
        void* userData);

    static inline std::atomic<int> s_errorCount_{0};
    static inline std::atomic<int> s_warningCount_{0};
};

} // namespace vulkan
} // namespace naksha
