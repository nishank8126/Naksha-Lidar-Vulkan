#include "naksha/core/vulkan/VulkanInstance.h"

namespace naksha {
namespace vulkan {

VulkanInstance::~VulkanInstance() { Shutdown(); }

bool VulkanInstance::Initialize(const VulkanInstanceConfig& config) {
    if (volkInitialize() != VK_SUCCESS) {
        return false;
    }

    validationEnabled_ = config.enableValidation;

    VkApplicationInfo appInfo{};
    appInfo.sType = VK_STRUCTURE_TYPE_APPLICATION_INFO;
    appInfo.pApplicationName = config.appName.c_str();
    appInfo.applicationVersion = config.appVersion;
    appInfo.pEngineName = "NakshaPointEngine";
    appInfo.engineVersion = VK_MAKE_VERSION(1, 0, 0);
    appInfo.apiVersion = VK_API_VERSION_1_3;

    std::vector<const char*> extensions;
    for (auto& ext : config.requiredExtensions) extensions.push_back(ext.c_str());
    std::vector<const char*> layers;
    for (auto& layer : config.requiredLayers) layers.push_back(layer.c_str());

    if (validationEnabled_) {
        extensions.push_back(VK_EXT_DEBUG_UTILS_EXTENSION_NAME);
        layers.push_back("VK_LAYER_KHRONOS_validation");
    }

    VkInstanceCreateInfo createInfo{};
    createInfo.sType = VK_STRUCTURE_TYPE_INSTANCE_CREATE_INFO;
    createInfo.pApplicationInfo = &appInfo;
    createInfo.enabledExtensionCount = static_cast<uint32_t>(extensions.size());
    createInfo.ppEnabledExtensionNames = extensions.data();
    createInfo.enabledLayerCount = static_cast<uint32_t>(layers.size());
    createInfo.ppEnabledLayerNames = layers.data();

    if (vkCreateInstance(&createInfo, nullptr, &instance_) != VK_SUCCESS) {
        return false;
    }
    volkLoadInstance(instance_);

    if (validationEnabled_) {
        auto createDebugFunc = (PFN_vkCreateDebugUtilsMessengerEXT)
            vkGetInstanceProcAddr(instance_, "vkCreateDebugUtilsMessengerEXT");
        if (createDebugFunc) {
            VkDebugUtilsMessengerCreateInfoEXT debugInfo{};
            debugInfo.sType = VK_STRUCTURE_TYPE_DEBUG_UTILS_MESSENGER_CREATE_INFO_EXT;
            debugInfo.messageSeverity =
                VK_DEBUG_UTILS_MESSAGE_SEVERITY_WARNING_BIT_EXT |
                VK_DEBUG_UTILS_MESSAGE_SEVERITY_ERROR_BIT_EXT;
            debugInfo.messageType =
                VK_DEBUG_UTILS_MESSAGE_TYPE_GENERAL_BIT_EXT |
                VK_DEBUG_UTILS_MESSAGE_TYPE_VALIDATION_BIT_EXT |
                VK_DEBUG_UTILS_MESSAGE_TYPE_PERFORMANCE_BIT_EXT;
            debugInfo.pfnUserCallback = DebugCallback;
            createDebugFunc(instance_, &debugInfo, nullptr, &debugMessenger_);
        }
    }
    return true;
}

void VulkanInstance::Shutdown() {
    if (debugMessenger_) {
        auto func = (PFN_vkDestroyDebugUtilsMessengerEXT)
            vkGetInstanceProcAddr(instance_, "vkDestroyDebugUtilsMessengerEXT");
        if (func) func(instance_, debugMessenger_, nullptr);
        debugMessenger_ = VK_NULL_HANDLE;
    }
    if (instance_) {
        vkDestroyInstance(instance_, nullptr);
        instance_ = VK_NULL_HANDLE;
    }
}

VKAPI_ATTR VkBool32 VKAPI_CALL VulkanInstance::DebugCallback(
    VkDebugUtilsMessageSeverityFlagBitsEXT severity,
    VkDebugUtilsMessageTypeFlagsEXT type,
    const VkDebugUtilsMessengerCallbackDataEXT* callbackData,
    void* userData) {
    (void)type; (void)userData;
    if (severity & VK_DEBUG_UTILS_MESSAGE_SEVERITY_ERROR_BIT_EXT)
        s_errorCount_++;
    else if (severity & VK_DEBUG_UTILS_MESSAGE_SEVERITY_WARNING_BIT_EXT)
        s_warningCount_++;
    if (severity >= VK_DEBUG_UTILS_MESSAGE_SEVERITY_WARNING_BIT_EXT) {
        // Tag the severity: the layer does NOT put "Validation Error: ..." in
        // pMessage for every VUID, so a log parser cannot otherwise tell an
        // error from a warning. Tests count "[Vulkan Validation][ERROR] lines".
        const char* tag = (severity & VK_DEBUG_UTILS_MESSAGE_SEVERITY_ERROR_BIT_EXT)
                              ? "[ERROR]"
                              : "[WARN]";
        fprintf(stderr, "[Vulkan Validation]%s %s\n", tag, callbackData->pMessage);
    }
    return VK_FALSE;
}

int VulkanInstance::GetValidationErrorCount() { return s_errorCount_.load(); }
int VulkanInstance::GetValidationWarningCount() { return s_warningCount_.load(); }
void VulkanInstance::ResetValidationCounters() { s_errorCount_ = 0; s_warningCount_ = 0; }

} // namespace vulkan
} // namespace naksha
