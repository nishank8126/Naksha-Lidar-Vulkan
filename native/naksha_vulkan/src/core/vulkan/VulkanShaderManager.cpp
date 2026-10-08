#include "naksha/core/vulkan/VulkanShaderManager.h"
#include <cstdio>
#include <fstream>

namespace naksha {
namespace vulkan {

VulkanShaderManager::~VulkanShaderManager() { Shutdown(); }

bool VulkanShaderManager::Initialize(VkDevice device) {
    device_ = device;
    return true;
}

void VulkanShaderManager::Shutdown() {
    device_ = VK_NULL_HANDLE;
}

VkShaderModule VulkanShaderManager::CreateShaderModule(const uint32_t* code, size_t size) {
    if (!code || size == 0) {
        fprintf(stderr, "VulkanShaderManager: refusing to create a shader module from empty SPIR-V\n");
        return VK_NULL_HANDLE;
    }

    VkShaderModuleCreateInfo createInfo{};
    createInfo.sType = VK_STRUCTURE_TYPE_SHADER_MODULE_CREATE_INFO;
    createInfo.codeSize = size;
    createInfo.pCode = code;

    VkShaderModule module = VK_NULL_HANDLE;
    if (vkCreateShaderModule(device_, &createInfo, nullptr, &module) != VK_SUCCESS) {
        fprintf(stderr, "VulkanShaderManager: vkCreateShaderModule failed\n");
        return VK_NULL_HANDLE;
    }
    return module;
}

VkShaderModule VulkanShaderManager::CreateShaderModule(const std::vector<uint32_t>& spirv) {
    return CreateShaderModule(spirv.data(), spirv.size() * sizeof(uint32_t));
}

void VulkanShaderManager::DestroyShaderModule(VkShaderModule module) {
    if (module) vkDestroyShaderModule(device_, module, nullptr);
}

std::vector<uint32_t> VulkanShaderManager::ReadSPIRVFile(const std::string& filepath) {
    std::ifstream file(filepath, std::ios::ate | std::ios::binary);
    if (!file.is_open()) {
        fprintf(stderr, "VulkanShaderManager: could not open SPIR-V file '%s'\n", filepath.c_str());
        return {};
    }

    size_t fileSize = static_cast<size_t>(file.tellg());
    std::vector<uint32_t> buffer(fileSize / sizeof(uint32_t));
    file.seekg(0);
    file.read(reinterpret_cast<char*>(buffer.data()), fileSize);
    return buffer;
}

ShaderModule VulkanShaderManager::LoadSPIRV(const std::string& filepath,
                                              VkShaderStageFlagBits stage) {
    auto spirv = ReadSPIRVFile(filepath);
    ShaderModule shader{};
    shader.stage = stage;
    shader.module = CreateShaderModule(spirv);
    return shader;
}

std::vector<ShaderModule> VulkanShaderManager::LoadSPIRVFiles(
    const std::vector<std::pair<std::string, VkShaderStageFlagBits>>& files) {
    std::vector<ShaderModule> modules;
    for (auto& [path, stage] : files) {
        modules.push_back(LoadSPIRV(path, stage));
    }
    return modules;
}

} // namespace vulkan
} // namespace naksha
