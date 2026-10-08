#include "naksha/core/vulkan/VulkanAllocator.h"

#include <cstdio>

namespace naksha {
namespace vulkan {

VulkanAllocator& VulkanAllocator::Get() {
    static VulkanAllocator instance;
    return instance;
}

void VulkanAllocator::Initialize(VkInstance instance, VkPhysicalDevice physicalDevice,
                                  VkDevice device, VkQueue graphicsQueue,
                                  uint32_t graphicsFamilyIndex,
                                  const VmaVulkanFunctions& vulkanFunctions) {
    device_ = device;
    graphicsQueue_ = graphicsQueue;

    VkCommandPoolCreateInfo poolInfo{};
    poolInfo.sType = VK_STRUCTURE_TYPE_COMMAND_POOL_CREATE_INFO;
    poolInfo.flags = VK_COMMAND_POOL_CREATE_RESET_COMMAND_BUFFER_BIT;
    poolInfo.queueFamilyIndex = graphicsFamilyIndex;
    vkCreateCommandPool(device_, &poolInfo, nullptr, &commandPool_);

    VmaAllocatorCreateInfo allocatorInfo{};
    // Nothing in this renderer calls vkGetBufferDeviceAddress, and the
    // logical device was never created with the bufferDeviceAddress feature
    // enabled - so this flag just told VMA to request device-address-capable
    // memory it has no right to ask for, tripping
    // VUID-VkMemoryAllocateInfo-flags-03331 on every buffer allocation and
    // making allocation success/failure driver-dependent undefined behavior.
    // That's why some buffers (e.g. point cloud vertex buffers) happened to
    // survive it while others (surface mesh vertex/index buffers) silently
    // failed to allocate, leaving CreateBuffer() to return an invalid handle
    // that UploadMesh() then discarded without a visible error.
    allocatorInfo.flags = 0;
    allocatorInfo.vulkanApiVersion = VK_API_VERSION_1_3;
    allocatorInfo.physicalDevice = physicalDevice;
    allocatorInfo.device = device;
    allocatorInfo.pVulkanFunctions = &vulkanFunctions;
    allocatorInfo.instance = instance;

    vmaCreateAllocator(&allocatorInfo, &allocator_);
}

void VulkanAllocator::Shutdown() {
    if (allocator_) {
        vmaDestroyAllocator(allocator_);
        allocator_ = VK_NULL_HANDLE;
    }
    if (commandPool_) {
        vkDestroyCommandPool(device_, commandPool_, nullptr);
        commandPool_ = VK_NULL_HANDLE;
    }
    graphicsQueue_ = VK_NULL_HANDLE;
    device_ = VK_NULL_HANDLE;
}

GPUBuffer VulkanAllocator::CreateBuffer(VkDeviceSize size, VkBufferUsageFlags usage,
                                         VmaMemoryUsage memoryUsage,
                                         VmaAllocationCreateFlags flags) {
    GPUBuffer buffer{};
    buffer.size = size;

    VkBufferCreateInfo bufferInfo{};
    bufferInfo.sType = VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO;
    bufferInfo.size = size;
    bufferInfo.usage = usage;
    bufferInfo.sharingMode = VK_SHARING_MODE_EXCLUSIVE;

    VmaAllocationCreateInfo allocInfo{};
    allocInfo.usage = memoryUsage;
    allocInfo.flags = flags;

    VkResult result = vmaCreateBuffer(allocator_, &bufferInfo, &allocInfo, &buffer.buffer,
                    &buffer.allocation, &buffer.allocationInfo);
    if (result != VK_SUCCESS) {
        fprintf(stderr, "[VulkanAllocator] CreateBuffer FAILED: size=%llu usage=0x%x "
                         "memoryUsage=%d VkResult=%d\n",
                static_cast<unsigned long long>(size), usage, static_cast<int>(memoryUsage),
                static_cast<int>(result));
        return GPUBuffer{};
    }
    buffer.mappedData = buffer.allocationInfo.pMappedData;

    stats_.totalAllocated += size;
    stats_.bufferCount++;
    return buffer;
}

void VulkanAllocator::DestroyBuffer(GPUBuffer& buffer) {
    if (buffer.IsValid()) {
        vmaDestroyBuffer(allocator_, buffer.buffer, buffer.allocation);
        stats_.totalAllocated -= buffer.size;
        stats_.bufferCount--;
        buffer = {};
    }
}

GPUImage VulkanAllocator::CreateImage(VkFormat format, VkExtent2D extent,
                                       VkImageUsageFlags usage, VmaMemoryUsage memoryUsage) {
    GPUImage image{};
    image.format = format;
    image.extent = extent;

    VkImageCreateInfo imageInfo{};
    imageInfo.sType = VK_STRUCTURE_TYPE_IMAGE_CREATE_INFO;
    imageInfo.imageType = VK_IMAGE_TYPE_2D;
    imageInfo.extent = {extent.width, extent.height, 1};
    imageInfo.mipLevels = 1;
    imageInfo.arrayLayers = 1;
    imageInfo.format = format;
    imageInfo.tiling = VK_IMAGE_TILING_OPTIMAL;
    imageInfo.initialLayout = VK_IMAGE_LAYOUT_UNDEFINED;
    imageInfo.usage = usage;
    imageInfo.samples = VK_SAMPLE_COUNT_1_BIT;

    VmaAllocationCreateInfo allocInfo{};
    allocInfo.usage = memoryUsage;

    vmaCreateImage(allocator_, &imageInfo, &allocInfo, &image.image, &image.allocation, nullptr);
    stats_.imageCount++;
    return image;
}

void VulkanAllocator::DestroyImage(GPUImage& image) {
    if (image.image != VK_NULL_HANDLE) {
        if (image.imageView != VK_NULL_HANDLE)
            vkDestroyImageView(device_, image.imageView, nullptr);
        vmaDestroyImage(allocator_, image.image, image.allocation);
        stats_.imageCount--;
        image = {};
    }
}

void VulkanAllocator::CopyBuffer(VkBuffer srcBuffer, VkBuffer dstBuffer, VkDeviceSize size) {
    VkCommandBuffer cmd = BeginSingleTimeCommands();

    VkBufferCopy copyRegion{};
    copyRegion.size = size;
    vkCmdCopyBuffer(cmd, srcBuffer, dstBuffer, 1, &copyRegion);

    EndSingleTimeCommands(cmd);
}

VulkanAllocator::Stats VulkanAllocator::GetStats() const {
    Stats result = stats_;
    VmaTotalStatistics vmaStats;
    vmaCalculateStatistics(allocator_, &vmaStats);
    result.totalUsed = vmaStats.total.statistics.blockBytes;
    result.allocationCount = vmaStats.total.unusedRangeCount;
    return result;
}

VkCommandBuffer VulkanAllocator::BeginSingleTimeCommands() {
    VkCommandBufferAllocateInfo allocInfo{};
    allocInfo.sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO;
    allocInfo.level = VK_COMMAND_BUFFER_LEVEL_PRIMARY;
    allocInfo.commandPool = commandPool_;
    allocInfo.commandBufferCount = 1;

    VkCommandBuffer cmd = VK_NULL_HANDLE;
    VkResult allocResult = vkAllocateCommandBuffers(device_, &allocInfo, &cmd);
    if (allocResult != VK_SUCCESS) {
        fprintf(stderr, "[VulkanAllocator] BeginSingleTimeCommands: "
                         "vkAllocateCommandBuffers FAILED VkResult=%d (pool=%p device=%p)\n",
                static_cast<int>(allocResult), (void*)commandPool_, (void*)device_);
        return VK_NULL_HANDLE;
    }

    VkCommandBufferBeginInfo beginInfo{};
    beginInfo.sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO;
    beginInfo.flags = VK_COMMAND_BUFFER_USAGE_ONE_TIME_SUBMIT_BIT;
    VkResult beginResult = vkBeginCommandBuffer(cmd, &beginInfo);
    if (beginResult != VK_SUCCESS) {
        fprintf(stderr, "[VulkanAllocator] BeginSingleTimeCommands: "
                         "vkBeginCommandBuffer FAILED VkResult=%d\n",
                static_cast<int>(beginResult));
        vkFreeCommandBuffers(device_, commandPool_, 1, &cmd);
        return VK_NULL_HANDLE;
    }

    return cmd;
}

void VulkanAllocator::EndSingleTimeCommands(VkCommandBuffer cmd) {
    if (cmd == VK_NULL_HANDLE) return;

    VkResult endResult = vkEndCommandBuffer(cmd);
    if (endResult != VK_SUCCESS) {
        fprintf(stderr, "[VulkanAllocator] EndSingleTimeCommands: "
                         "vkEndCommandBuffer FAILED VkResult=%d\n",
                static_cast<int>(endResult));
    }

    VkSubmitInfo submitInfo{};
    submitInfo.sType = VK_STRUCTURE_TYPE_SUBMIT_INFO;
    submitInfo.commandBufferCount = 1;
    submitInfo.pCommandBuffers = &cmd;

    VkResult submitResult = vkQueueSubmit(graphicsQueue_, 1, &submitInfo, VK_NULL_HANDLE);
    if (submitResult != VK_SUCCESS) {
        fprintf(stderr, "[VulkanAllocator] EndSingleTimeCommands: "
                         "vkQueueSubmit FAILED VkResult=%d (queue=%p)\n",
                static_cast<int>(submitResult), (void*)graphicsQueue_);
    } else {
        VkResult waitResult = vkQueueWaitIdle(graphicsQueue_);
        if (waitResult != VK_SUCCESS) {
            fprintf(stderr, "[VulkanAllocator] EndSingleTimeCommands: "
                             "vkQueueWaitIdle FAILED VkResult=%d\n",
                    static_cast<int>(waitResult));
        }
    }

    vkFreeCommandBuffers(device_, commandPool_, 1, &cmd);
}

} // namespace vulkan
} // namespace naksha
