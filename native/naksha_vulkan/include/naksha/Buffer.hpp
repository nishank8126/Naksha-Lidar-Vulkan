#pragma once
#include <cstdint>
#include <vector>
#include <chrono>

#include "naksha/core/vulkan/VulkanHeaders.h"
#include "naksha/core/vulkan/VulkanAllocator.h"

namespace naksha {
using namespace vulkan;

// Timings reported by every upload path (spec section: benchmark report fields).
struct UploadStats {
    double cpuPrepMs = 0.0;   // CPU conversion/expansion before/while staging
                              // (0 when input already matches GPU layout)
    double stageMs = 0.0;      // CPU memcpy/write into host-visible staging
    double gpuCopyMs = 0.0;    // GPU timestamp span covering the copy commands
    double wallMs = 0.0;       // wall time of the whole upload call
    uint64_t bytes = 0;        // device-side bytes transferred

    void Accumulate(const UploadStats& o) {
        cpuPrepMs += o.cpuPrepMs;
        stageMs += o.stageMs;
        gpuCopyMs += o.gpuCopyMs;
        wallMs += o.wallMs;
        bytes += o.bytes;
    }
};

struct CopyRegion {
    VkBuffer dst = VK_NULL_HANDLE;
    VkDeviceSize dstOffset = 0;
    VkDeviceSize srcOffset = 0;  // relative to staging buffer base
    VkDeviceSize size = 0;
};

// Monotonic milliseconds for CPU timings.
inline double NowMs() {
    using namespace std::chrono;
    return duration<double, std::milli>(
               steady_clock::now().time_since_epoch())
        .count();
}

// ---------------------------------------------------------------------------
// Staging uploader: ONE grow-only persistently-mapped VMA staging buffer
// (VkBuffer in HOST_VISIBLE memory, memcpy writes, VMA flush), ONE command
// buffer, GPU timestamps (vkCmdWriteTimestamp) around the copy batch.
//
// The engine is single-queue (all work on the graphics+present family), so
// uploads are allowed to be blocking (queue idle wait). That is what makes
// "GPU upload duration" a well-defined timestamp span instead of an
// estimate. Uploads never touch frame-synced resources: a copy submit here
// is fully retired before Execute() returns, independent of frame fences.
//
// Allocation rule (spec section 8): every buffer/image goes through
// VulkanAllocator (VMA) - no raw vkAllocateMemory exists in this engine.
// ---------------------------------------------------------------------------
class UploadContext {
public:
    // timestampPeriodNs: from VkPhysicalDeviceLimits (0 disables GPU timing).
    bool Initialize(VkDevice device, VkQueue queue, uint32_t queueFamilyIndex,
                    float timestampPeriodNs);
    void Shutdown();
    bool IsInitialized() const { return device_ != VK_NULL_HANDLE; }

    // Ensures staging capacity >= bytes (grows, never shrinks) and returns
    // the persistently mapped pointer at offset 0. Caller writes CPU source
    // data here (conversion passes can write straight into staging).
    uint8_t* Stage(VkDeviceSize bytes);

    // Records all regions (sources come from staging), submits with a
    // timestamp pair around the copy batch, waits for completion, reads back
    // the timestamps. cpuPrepMs/stageMs are caller-measured CPU phases.
    UploadStats Execute(const std::vector<CopyRegion>& regions,
                        double cpuPrepMs, double stageMs);

    VkDeviceSize GetStagingCapacity() const { return staging_.size; }
    uint64_t GetTotalUploadedBytes() const { return totalUploadedBytes_; }

private:
    VkDevice device_ = VK_NULL_HANDLE;
    VkQueue queue_ = VK_NULL_HANDLE;
    uint32_t queueFamilyIndex_ = 0;
    float timestampPeriodNs_ = 0.0f;
    GPUBuffer staging_;
    VkCommandPool commandPool_ = VK_NULL_HANDLE;
    VkCommandBuffer commandBuffer_ = VK_NULL_HANDLE;
    VkQueryPool queryPool_ = VK_NULL_HANDLE;  // 2 timestamps, reused
    uint64_t totalUploadedBytes_ = 0;
};

} // namespace naksha
