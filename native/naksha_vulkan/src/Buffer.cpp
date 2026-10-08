#include "naksha/Buffer.hpp"
#include <cstdio>

namespace naksha {

bool UploadContext::Initialize(VkDevice device, VkQueue queue,
                                uint32_t queueFamilyIndex,
                                float timestampPeriodNs) {
    device_ = device;
    queue_ = queue;
    queueFamilyIndex_ = queueFamilyIndex;
    timestampPeriodNs_ = timestampPeriodNs;

    VkCommandPoolCreateInfo poolInfo{};
    poolInfo.sType = VK_STRUCTURE_TYPE_COMMAND_POOL_CREATE_INFO;
    poolInfo.flags = VK_COMMAND_POOL_CREATE_RESET_COMMAND_BUFFER_BIT;
    poolInfo.queueFamilyIndex = queueFamilyIndex_;
    if (vkCreateCommandPool(device_, &poolInfo, nullptr, &commandPool_) != VK_SUCCESS) {
        fprintf(stderr, "[UploadContext] command pool FAILED\n");
        return false;
    }

    VkCommandBufferAllocateInfo allocInfo{};
    allocInfo.sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO;
    allocInfo.commandPool = commandPool_;
    allocInfo.level = VK_COMMAND_BUFFER_LEVEL_PRIMARY;
    allocInfo.commandBufferCount = 1;
    if (vkAllocateCommandBuffers(device_, &allocInfo, &commandBuffer_) != VK_SUCCESS) {
        fprintf(stderr, "[UploadContext] command buffer FAILED\n");
        return false;
    }

    VkQueryPoolCreateInfo qpi{};
    qpi.sType = VK_STRUCTURE_TYPE_QUERY_POOL_CREATE_INFO;
    qpi.queryType = VK_QUERY_TYPE_TIMESTAMP;
    qpi.queryCount = 2;
    if (vkCreateQueryPool(device_, &qpi, nullptr, &queryPool_) != VK_SUCCESS) {
        fprintf(stderr, "[UploadContext] query pool FAILED\n");
        return false;
    }

    return true;
}

void UploadContext::Shutdown() {
    if (queryPool_) { vkDestroyQueryPool(device_, queryPool_, nullptr); queryPool_ = VK_NULL_HANDLE; }
    if (commandBuffer_) { vkFreeCommandBuffers(device_, commandPool_, 1, &commandBuffer_); commandBuffer_ = VK_NULL_HANDLE; }
    if (commandPool_) { vkDestroyCommandPool(device_, commandPool_, nullptr); commandPool_ = VK_NULL_HANDLE; }
    VulkanAllocator::Get().DestroyBuffer(staging_);
    staging_ = {};
    device_ = VK_NULL_HANDLE;
}

uint8_t* UploadContext::Stage(VkDeviceSize bytes) {
    if (!staging_.IsValid() || staging_.size < bytes) {
        VulkanAllocator::Get().DestroyBuffer(staging_);
        // grow: next power-of-two-ish over-allocation to amortize
        VkDeviceSize cap = bytes;
        if (cap < 1024 * 1024) cap = 1024 * 1024;
        staging_ = VulkanAllocator::Get().CreateBuffer(
            cap,
            VK_BUFFER_USAGE_TRANSFER_SRC_BIT,
            VMA_MEMORY_USAGE_AUTO,
            VMA_ALLOCATION_CREATE_HOST_ACCESS_SEQUENTIAL_WRITE_BIT |
            VMA_ALLOCATION_CREATE_MAPPED_BIT);
        if (!staging_.IsValid()) {
            fprintf(stderr, "[UploadContext] staging buffer FAILED (%llu bytes)\n",
                    static_cast<unsigned long long>(bytes));
            return nullptr;
        }
    }
    if (!staging_.mappedData) {
        // Should be persistently mapped by the allocation flags above.
        // Guard against unexpected unmapped.
        VmaAllocationInfo info{};
        vmaGetAllocationInfo(VulkanAllocator::Get().GetAllocator(), staging_.allocation, &info);
        if (!info.pMappedData) return nullptr;
        staging_.mappedData = info.pMappedData;
    }
    return static_cast<uint8_t*>(staging_.mappedData);
}

UploadStats UploadContext::Execute(const std::vector<CopyRegion>& regions,
                                     double cpuPrepMs, double stageMs) {
    UploadStats stats{};
    stats.cpuPrepMs = cpuPrepMs;
    stats.stageMs = stageMs;
    stats.bytes = 0;
    for (const auto& r : regions) stats.bytes += r.size;

    const double wallStart = NowMs();
    VkDeviceSize totalSrc = 0;
    for (const auto& r : regions) totalSrc += r.size;
    if (totalSrc == 0) { stats.wallMs = 0; return stats; }

    VkCommandBufferAllocateInfo allocInfo{};
    allocInfo.sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO;
    allocInfo.commandPool = commandPool_;
    allocInfo.level = VK_COMMAND_BUFFER_LEVEL_PRIMARY;
    allocInfo.commandBufferCount = 1;
    VkCommandBuffer cmd = VK_NULL_HANDLE;
    if (vkAllocateCommandBuffers(device_, &allocInfo, &cmd) != VK_SUCCESS) return stats;

    VkCommandBufferBeginInfo beginInfo{};
    beginInfo.sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO;
    beginInfo.flags = VK_COMMAND_BUFFER_USAGE_ONE_TIME_SUBMIT_BIT;
    if (vkBeginCommandBuffer(cmd, &beginInfo) != VK_SUCCESS) return stats;

    VkDeviceSize srcOffset = 0;
    if (timestampPeriodNs_ > 0.0f) {
        vkCmdResetQueryPool(cmd, queryPool_, 0, 2);
        vkCmdWriteTimestamp(cmd, VK_PIPELINE_STAGE_TOP_OF_PIPE_BIT, queryPool_, 0);
    }
    for (const auto& r : regions) {
        VkBufferCopy copy{};
        copy.srcOffset = srcOffset;
        copy.dstOffset = r.dstOffset;
        copy.size = r.size;
        vkCmdCopyBuffer(cmd, staging_.buffer, r.dst, 1, &copy);
        srcOffset += r.size;
    }
    if (timestampPeriodNs_ > 0.0f) {
        vkCmdWriteTimestamp(cmd, VK_PIPELINE_STAGE_BOTTOM_OF_PIPE_BIT, queryPool_, 1);
    }

    if (vkEndCommandBuffer(cmd) != VK_SUCCESS) return stats;

    // The staging buffer is persistently mapped and never unmapped, so VMA
    // performs no automatic flush: without this the copies below may read
    // stale host memory (the uploaded buffers would be garbage/zero).
    if (staging_.mappedData) staging_.FlushMapped();

    VkSubmitInfo si{};
    si.sType = VK_STRUCTURE_TYPE_SUBMIT_INFO;
    si.commandBufferCount = 1;
    si.pCommandBuffers = &cmd;
    if (vkQueueSubmit(queue_, 1, &si, VK_NULL_HANDLE) != VK_SUCCESS) return stats;
    vkQueueWaitIdle(queue_);

    if (timestampPeriodNs_ > 0.0f) {
        uint64_t data[2] = {0, 0};
        vkGetQueryPoolResults(device_, queryPool_, 0, 2, sizeof(data), data,
                              sizeof(uint64_t), VK_QUERY_RESULT_64_BIT);
        stats.gpuCopyMs = static_cast<double>(data[1] - data[0]) * timestampPeriodNs_ / 1e6;
    }

    stats.wallMs = NowMs() - wallStart;
    totalUploadedBytes_ += stats.bytes;
    vkFreeCommandBuffers(device_, commandPool_, 1, &cmd);
    return stats;
}

} // namespace naksha
