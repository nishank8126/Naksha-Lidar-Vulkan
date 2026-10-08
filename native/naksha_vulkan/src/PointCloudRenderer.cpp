#include "naksha/PointCloudRenderer.hpp"
#include "naksha/Renderer.hpp"
#include <cmath>
#include <cstring>

namespace naksha {

PointCloudRenderer::~PointCloudRenderer() { Shutdown(); }

// Four vertex streams, all four bound on every draw:
//   0 positions       float3 xyz, render space
//   1 colors          RGBA8 (true-colour data; also the mode-0 shade)
//   2 classification  R8_UNORM (shader recovers the id with round(v*255))
//   3 intensity       R32_SFLOAT
// Classification and intensity travel as data so point.vert does the colouring
// (palette / ramp lookup) instead of Python re-baking an RGBA buffer every time
// the user flips a display mode.
static VkVertexInputBindingDescription pointBindings[4] = {
    {0, sizeof(float) * 3, VK_VERTEX_INPUT_RATE_VERTEX},
    {1, sizeof(uint8_t) * 4, VK_VERTEX_INPUT_RATE_VERTEX},
    {2, sizeof(uint8_t), VK_VERTEX_INPUT_RATE_VERTEX},
    {3, sizeof(float), VK_VERTEX_INPUT_RATE_VERTEX},
};
static VkVertexInputAttributeDescription pointAttributes[4] = {
    {0, 0, VK_FORMAT_R32G32B32_SFLOAT, 0},
    {1, 1, VK_FORMAT_R8G8B8A8_UNORM, 0},
    {2, 2, VK_FORMAT_R8_UNORM, 0},
    {3, 3, VK_FORMAT_R32_SFLOAT, 0},
};

// ---- Instant Shaded SPLAT pass vertex layout (mode 4 only) ----------------
// Same four streams PLUS the packed oct16x2 normal at location 4.
//
// R16G16_SNORM is exactly 2 x int16 = 4 bytes: the stored representation is
// uploaded verbatim and decoded in the shader, never expanded to float32x3 on
// the CPU. The layout is separate because this pipeline targets a TWO-attachment
// render pass (colour + normal), which point.vert's pipeline is not compatible
// with - so point.vert/point.frag stay untouched for every other display mode.
static VkVertexInputBindingDescription splatBindings[5] = {
    {0, sizeof(float) * 3, VK_VERTEX_INPUT_RATE_VERTEX},
    {1, sizeof(uint8_t) * 4, VK_VERTEX_INPUT_RATE_VERTEX},
    {2, sizeof(uint8_t), VK_VERTEX_INPUT_RATE_VERTEX},
    {3, sizeof(float), VK_VERTEX_INPUT_RATE_VERTEX},
    {4, sizeof(int16_t) * 2, VK_VERTEX_INPUT_RATE_VERTEX},
};
static VkVertexInputAttributeDescription splatAttributes[5] = {
    {0, 0, VK_FORMAT_R32G32B32_SFLOAT, 0},
    {1, 1, VK_FORMAT_R8G8B8A8_UNORM, 0},
    {2, 2, VK_FORMAT_R8_UNORM, 0},
    {3, 3, VK_FORMAT_R32_SFLOAT, 0},
    {4, 4, VK_FORMAT_R16G16_SNORM, 0},
};

// Defined below, next to the upload helpers they belong to.
static void ensureBuffer(GPUBuffer& buf, VkDeviceSize bytes, VkDeviceSize extraUsage);
static bool uploadZeros(Renderer* renderer, GPUBuffer& buf, VkDeviceSize bytes);

bool PointCloudRenderer::Initialize(Renderer& renderer) {
    renderer_ = &renderer;

    std::string vsPath = renderer.cfg_.shaderDirectory + "/point.vert.spv";
    std::string fsPath = renderer.cfg_.shaderDirectory + "/point.frag.spv";
    vs_ = renderer.Shaders().LoadSPIRV(vsPath, VK_SHADER_STAGE_VERTEX_BIT);
    fs_ = renderer.Shaders().LoadSPIRV(fsPath, VK_SHADER_STAGE_FRAGMENT_BIT);
    if (vs_.module == VK_NULL_HANDLE || fs_.module == VK_NULL_HANDLE) return false;

    // Cache the device point-size ceiling at init, not only in Record(): the
    // Python side queries it to reason about gl_PointSize, and a query issued
    // before the first draw must not report 0.
    {
        const float devMax = renderer.GetContext().GetReport().maxPointSize;
        if (devMax > 1.0f) {
            maxPointSizeDevice_ = devMax;
            if (pointSizeMaxPx_ > devMax) pointSizeMaxPx_ = devMax;
        }
    }

    VkPushConstantRange pcr{};
    // PointParams is 5 vec4 (80 bytes, well inside the guaranteed 128-byte
    // minimum): the vertex stage consumes all of it for sizing + shading, the
    // fragment stage reads .view for the sprite rim, and .parity.x is the
    // colour-space byte-parity flag (see shaders/point.vert), so the range has
    // to cover both stages.
    pcr.stageFlags = VK_SHADER_STAGE_VERTEX_BIT | VK_SHADER_STAGE_FRAGMENT_BIT;
    pcr.offset = 0;
    pcr.size = sizeof(float) * 20;
    pipelineLayout_ = renderer.Pipelines().CreatePipelineLayout(
        {renderer.GetFrameSetLayout()}, {pcr});
    if (pipelineLayout_ == VK_NULL_HANDLE) return false;

    std::vector<ShaderModule> shaders{vs_, fs_};
    pipelineCfg_ = PipelineConfig{};
    pipelineCfg_.SetDefaults();
    // Override the legacy point layout with the 4-stream layout above.
    pipelineCfg_.vertexBindings = {pointBindings[0], pointBindings[1],
                                   pointBindings[2], pointBindings[3]};
    pipelineCfg_.vertexAttributes = {pointAttributes[0], pointAttributes[1],
                                     pointAttributes[2], pointAttributes[3]};
    pipelineCfg_.vertexInput.vertexBindingDescriptionCount = 4;
    pipelineCfg_.vertexInput.pVertexBindingDescriptions = pointBindings;
    pipelineCfg_.vertexInput.vertexAttributeDescriptionCount = 4;
    pipelineCfg_.vertexInput.pVertexAttributeDescriptions = pointAttributes;
    pipelineCfg_.inputAssembly.topology = VK_PRIMITIVE_TOPOLOGY_POINT_LIST;
    pipelineCfg_.rasterizer.cullMode = VK_CULL_MODE_NONE;
    pipelineCfg_.depthStencil.depthCompareOp = VK_COMPARE_OP_LESS;
    // VTK PARITY: no alpha blending.
    // gui/unified_actor_manager.py's //VTK::Color::Impl writes `opacity = 1.0`
    // and only discards fragments outside radius 0.5, so every surviving
    // fragment is an OPAQUE write. Blending was previously enabled to make
    // "neighbouring returns accumulate into a continuous cloud" - but that is
    // precisely the soft-ball/splat appearance: a point whose rim fades toward
    // transparent, blended into its neighbours, reads as a fuzzy ball rather
    // than a crisp individual return. VTK does not do that, so neither do we.
    //
    // Overlap is still handled correctly: the first (nearest) point to write a
    // pixel wins via the depth test, exactly as in the VTK viewport. Coverage
    // therefore matches instead of being inflated by accumulation.
    pipelineCfg_.colorBlendAttachment.blendEnable = VK_FALSE;
    pipelineCfg_.colorBlendAttachment.srcColorBlendFactor = VK_BLEND_FACTOR_ONE;
    pipelineCfg_.colorBlendAttachment.dstColorBlendFactor = VK_BLEND_FACTOR_ZERO;
    pipelineCfg_.colorBlendAttachment.colorBlendOp = VK_BLEND_OP_ADD;
    pipelineCfg_.colorBlendAttachment.srcAlphaBlendFactor = VK_BLEND_FACTOR_ONE;
    pipelineCfg_.colorBlendAttachment.dstAlphaBlendFactor = VK_BLEND_FACTOR_ZERO;
    pipelineCfg_.colorBlendAttachment.alphaBlendOp = VK_BLEND_OP_ADD;
    pipelineCfg_.colorBlendAttachment.colorWriteMask =
        VK_COLOR_COMPONENT_R_BIT | VK_COLOR_COMPONENT_G_BIT |
        VK_COLOR_COMPONENT_B_BIT | VK_COLOR_COMPONENT_A_BIT;
    pipelineCfg_.colorBlending.attachmentCount = 1;
    pipelineCfg_.colorBlending.pAttachments = &pipelineCfg_.colorBlendAttachment;
    pipelineCfg_.colorFormat = VK_FORMAT_B8G8R8A8_SRGB;
    pipelineCfg_.depthFormat = VK_FORMAT_D32_SFLOAT;

    pipeline_ = renderer.Pipelines().CreateGraphicsPipeline(
        pipelineLayout_, shaders, pipelineCfg_, renderer.GetRenderPass(), 0);
    if (pipeline_ == VK_NULL_HANDLE) return false;

    // ---- Instant Shaded SPLAT pipeline (display mode 4) --------------------
    // Two colour outputs (class colour + the splat's STORED oct16x2 normal) and
    // a 5-stream vertex layout, against the render pass that declares exactly
    // those attachments. It shares pipelineLayout_ and every buffer with the
    // ordinary point pipeline.
    //
    // Failure here is NOT fatal: mode 4 then draws nothing and the app falls
    // back, whereas every other display mode is unaffected.
    {
        const std::string vsPath = renderer.cfg_.shaderDirectory + "/splat.vert.spv";
        const std::string fsPath = renderer.cfg_.shaderDirectory + "/splat.frag.spv";
        splatVs_ = renderer.Shaders().LoadSPIRV(vsPath, VK_SHADER_STAGE_VERTEX_BIT);
        splatFs_ = renderer.Shaders().LoadSPIRV(fsPath, VK_SHADER_STAGE_FRAGMENT_BIT);
        if (splatVs_.module != VK_NULL_HANDLE && splatFs_.module != VK_NULL_HANDLE) {
            PipelineConfig scfg;
            scfg.SetDefaults();
            scfg.vertexBindings.assign(splatBindings, splatBindings + 5);
            scfg.vertexAttributes.assign(splatAttributes, splatAttributes + 5);
            scfg.vertexInput.vertexBindingDescriptionCount = 5;
            scfg.vertexInput.pVertexBindingDescriptions = scfg.vertexBindings.data();
            scfg.vertexInput.vertexAttributeDescriptionCount = 5;
            scfg.vertexInput.pVertexAttributeDescriptions = scfg.vertexAttributes.data();
            scfg.inputAssembly.topology = VK_PRIMITIVE_TOPOLOGY_POINT_LIST;
            scfg.rasterizer.cullMode = VK_CULL_MODE_NONE;
            // Opaque, depth-tested: no blending, so class colours and normals
            // can never mix between splats.
            scfg.colorBlendAttachment.blendEnable = VK_FALSE;
            scfg.depthStencil.depthCompareOp = VK_COMPARE_OP_LESS;
            // Two colour attachments. They MUST be byte-identical unless the
            // independentBlend feature is enabled (VUID-...-pAttachments-00605),
            // and this device does not enable it - so one shared state, written
            // to both. Writing alpha into the R16G16_SNORM normal target is
            // harmless: that format has no alpha component, so it is dropped.
            static VkPipelineColorBlendAttachmentState blendStates[2] = {};
            blendStates[0].colorWriteMask =
                VK_COLOR_COMPONENT_R_BIT | VK_COLOR_COMPONENT_G_BIT |
                VK_COLOR_COMPONENT_B_BIT | VK_COLOR_COMPONENT_A_BIT;
            blendStates[1] = blendStates[0];
            scfg.colorBlending.attachmentCount = 2;
            scfg.colorBlending.pAttachments = blendStates;
            splatPipeline_ = renderer.Pipelines().CreateGraphicsPipeline(
                pipelineLayout_, {splatVs_, splatFs_}, scfg,
                renderer.GetInstantRenderPass(), 0);
            if (splatPipeline_ == VK_NULL_HANDLE) {
                fprintf(stderr, "[INSTANT SHADED] splat pipeline FAILED - "
                                "Shaded Class falls back\n");
            }
        } else {
            fprintf(stderr, "[INSTANT SHADED] splat shaders missing - "
                            "Shaded Class falls back\n");
        }
    }

    // Stand-in streams for datasets that carry no classification or intensity
    // channel: the pipeline always references bindings 2 and 3, so they have to
    // point at defined memory (4 bytes covers one element of either type).
    uploadZeros(renderer_, fallbackClass_, 4);
    uploadZeros(renderer_, fallbackIntensity_, 4);
    // Oct (0,0) decodes to +Z: a flat, up-facing surface. So a cloud with no
    // normal stream resident yet shades as flat ground rather than as noise.
    uploadZeros(renderer_, fallbackNormals_, 4);

    initialized_ = true;
    return true;
}

void PointCloudRenderer::Shutdown() {
    if (!initialized_) return;
    initialized_ = false;
    if (renderer_) {
        renderer_->Pipelines().DestroyPipeline(pipeline_);
        renderer_->Pipelines().DestroyPipeline(splatPipeline_);
        renderer_->Pipelines().DestroyPipelineLayout(pipelineLayout_);
        renderer_->Shaders().DestroyShaderModule(vs_.module);
        renderer_->Shaders().DestroyShaderModule(fs_.module);
        renderer_->Shaders().DestroyShaderModule(splatVs_.module);
        renderer_->Shaders().DestroyShaderModule(splatFs_.module);
    }
    VulkanAllocator::Get().DestroyBuffer(positions_);
    VulkanAllocator::Get().DestroyBuffer(colors_);
    VulkanAllocator::Get().DestroyBuffer(classification_);
    VulkanAllocator::Get().DestroyBuffer(intensity_);
    VulkanAllocator::Get().DestroyBuffer(normals_);
    VulkanAllocator::Get().DestroyBuffer(fallbackClass_);
    VulkanAllocator::Get().DestroyBuffer(fallbackIntensity_);
    VulkanAllocator::Get().DestroyBuffer(fallbackNormals_);
    positions_ = {}; colors_ = {}; classification_ = {}; intensity_ = {};
    normals_ = {};
    fallbackClass_ = {}; fallbackIntensity_ = {}; fallbackNormals_ = {};
    pipeline_ = VK_NULL_HANDLE; pipelineLayout_ = VK_NULL_HANDLE;
    splatPipeline_ = VK_NULL_HANDLE;
    vs_ = {}; fs_ = {}; splatVs_ = {}; splatFs_ = {};
    renderer_ = nullptr;
}

static void ensureBuffer(GPUBuffer& buf, VkDeviceSize bytes,
                             VkDeviceSize extraUsage) {
    if (buf.IsValid() && buf.size >= bytes) return;
    VulkanAllocator::Get().DestroyBuffer(buf);
    buf = VulkanAllocator::Get().CreateBuffer(
        bytes, VK_BUFFER_USAGE_TRANSFER_DST_BIT | extraUsage,
        VMA_MEMORY_USAGE_GPU_ONLY);
}

// Zero-fill a buffer through the normal staging path.
static bool uploadZeros(Renderer* renderer, GPUBuffer& buf, VkDeviceSize bytes) {
    ensureBuffer(buf, bytes, VK_BUFFER_USAGE_VERTEX_BUFFER_BIT);
    if (!buf.IsValid()) return false;
    uint8_t* dst = renderer->GetUploadContext().Stage(bytes);
    if (!dst) return false;
    std::memset(dst, 0, bytes);
    CopyRegion reg{buf.buffer, 0, 0, bytes};
    renderer->GetUploadContext().Execute({reg}, 0.0, 0.0);
    return true;
}

UploadStats PointCloudRenderer::UploadPositionsWorldF64(
    const double* xyz, std::size_t count, const RenderOrigin& origin) {
    UploadStats stats;
    const std::size_t n = count * 3;
    const VkDeviceSize bytes = n * sizeof(float);
    if (bytes == 0) return stats;

    std::vector<float> render(n);
    double t0 = NowMs();
    RenderOrigin::WorldToRenderF64ToF32(xyz, render.data(), count, origin);
    double t1 = NowMs();

    // A whole-cloud upload re-packs from offset 0: it ends arena residency.
    arenaMode_ = false; arenaCapacity_ = 0; arenaTilesUploaded_ = 0;
    classInArena_ = false; intensityInArena_ = false;
    ensureBuffer(positions_, bytes, VK_BUFFER_USAGE_VERTEX_BUFFER_BIT);
    if (!positions_.IsValid()) return stats;
    uint8_t* dst = renderer_->GetUploadContext().Stage(bytes);
    if (!dst) return stats;
    double t2 = NowMs();
    std::memcpy(dst, render.data(), bytes);
    double t3 = NowMs();
    CopyRegion reg{positions_.buffer, 0, 0, bytes};
    stats = renderer_->GetUploadContext().Execute({reg}, t1 - t0, t3 - t2);
    positionUploads_++;
    pointCount_ = count;
    // PHASE 1: the attribute streams are indexed by POINT POSITION, so a new
    // position buffer invalidates them even when its length is unchanged - a
    // different tile set reorders the concatenation. Until the matching stream
    // is re-uploaded, Record()/RecordSplat() bind the zero stand-ins, which is
    // the honest "attribute not resident" state rather than a stale attribute
    // applied to whichever points now sit at those indices.
    classificationCount_ = 0;
    intensityCount_ = 0;
    // Size the zero-filled oct16 stand-in to the cloud so the splat pass can
    // bind it without ever reading past the end of the buffer. A short stand-in
    // would be sampled out of bounds for every point past its length.
    ensureFallbackNormals(count);
    return stats;
}

// ---------------------------------------------------------------------------
// Arena residency
// ---------------------------------------------------------------------------
int PointCloudRenderer::ReserveArena(std::size_t capacityPoints) {
    if (capacityPoints == 0 || !renderer_) return 0;
    if (arenaMode_ && arenaCapacity_ >= capacityPoints) return 1;
    const int rc = (arenaMode_ || positions_.IsValid()) ? 2 : 1;
    // A new arena discards every previous stream: offsets are only meaningful
    // inside ONE arena. Attribute buffers are re-created lazily AT CAPACITY, so
    // a Neutral-only session never pays for class/intensity/normal memory.
    VulkanAllocator::Get().DestroyBuffer(positions_);
    VulkanAllocator::Get().DestroyBuffer(classification_);
    VulkanAllocator::Get().DestroyBuffer(intensity_);
    VulkanAllocator::Get().DestroyBuffer(normals_);
    positions_ = VulkanAllocator::Get().CreateBuffer(
        static_cast<VkDeviceSize>(capacityPoints) * 3 * sizeof(float),
        VK_BUFFER_USAGE_TRANSFER_DST_BIT | VK_BUFFER_USAGE_VERTEX_BUFFER_BIT,
        VMA_MEMORY_USAGE_GPU_ONLY);
    if (!positions_.IsValid()) {
        arenaMode_ = false; arenaCapacity_ = 0;
        return 0;
    }
    arenaMode_ = true;
    arenaCapacity_ = capacityPoints;
    arenaTilesUploaded_ = 0;
    pointCount_ = 0;
    classificationCount_ = 0; intensityCount_ = 0; normalCount_ = 0;
    classInArena_ = false; intensityInArena_ = false;
    // Zero stand-ins must cover the whole arena: the splat pass binds the normal
    // fallback when no normal stream is resident, and reads up to pointCount_.
    ensureFallbackNormals(capacityPoints);
    return rc;
}

UploadStats PointCloudRenderer::UploadArenaTile(
    std::size_t first, std::size_t count, const double* xyzWorldF64,
    const RenderOrigin& origin, const uint8_t* classification,
    const float* intensity, const uint8_t* normalsOct16) {
    UploadStats stats;
    // xyzWorldF64 == nullptr means ATTRIBUTE-ONLY: the tile's positions are
    // already resident (a mode switch backfilling class / intensity / normals),
    // so they must not be sent again. Nothing to do if there is no stream at all.
    const bool hasXyz = (xyzWorldF64 != nullptr);
    if (!arenaMode_ || count == 0) return stats;
    if (!hasXyz && !classification && !intensity && !normalsOct16) return stats;
    if (first + count > arenaCapacity_) return stats;     // never write past the arena
    if (!hasXyz && first + count > pointCount_) return stats;  // attributes need positions

    const VkDeviceSize posBytes = hasXyz ? static_cast<VkDeviceSize>(count) * 3 * sizeof(float) : 0;
    const VkDeviceSize clsBytes = classification ? static_cast<VkDeviceSize>(count) : 0;
    const VkDeviceSize intBytes = intensity ? static_cast<VkDeviceSize>(count) * sizeof(float) : 0;
    const VkDeviceSize nrmBytes = normalsOct16
        ? static_cast<VkDeviceSize>(count) * kOct16BytesPerNormal : 0;

    std::vector<float> render;
    double t0 = NowMs();
    if (hasXyz) {
        render.resize(count * 3);
        RenderOrigin::WorldToRenderF64ToF32(xyzWorldF64, render.data(), count, origin);
    }
    double t1 = NowMs();

    if (classification) {
        ensureBuffer(classification_, static_cast<VkDeviceSize>(arenaCapacity_),
                     VK_BUFFER_USAGE_VERTEX_BUFFER_BIT);
        if (!classification_.IsValid()) return stats;
    }
    if (intensity) {
        ensureBuffer(intensity_, static_cast<VkDeviceSize>(arenaCapacity_) * sizeof(float),
                     VK_BUFFER_USAGE_VERTEX_BUFFER_BIT);
        if (!intensity_.IsValid()) return stats;
    }
    if (normalsOct16) {
        ensureBuffer(normals_, static_cast<VkDeviceSize>(arenaCapacity_) * kOct16BytesPerNormal,
                     VK_BUFFER_USAGE_VERTEX_BUFFER_BIT);
        if (!normals_.IsValid()) return stats;
    }

    // ONE staged submit for every stream: the staging buffer is filled
    // consecutively and Execute() walks the regions in the same order.
    const VkDeviceSize total = posBytes + clsBytes + intBytes + nrmBytes;
    uint8_t* dst = renderer_->GetUploadContext().Stage(total);
    if (!dst) return stats;
    double t2 = NowMs();
    std::vector<CopyRegion> regions;
    VkDeviceSize cursor = 0;
    if (hasXyz) {
        std::memcpy(dst + cursor, render.data(), static_cast<std::size_t>(posBytes));
        CopyRegion r; r.dst = positions_.buffer;
        r.dstOffset = static_cast<VkDeviceSize>(first) * 3 * sizeof(float);
        r.srcOffset = cursor; r.size = posBytes; regions.push_back(r);
        cursor += posBytes;
    }
    if (classification) {
        std::memcpy(dst + cursor, classification, static_cast<std::size_t>(clsBytes));
        CopyRegion r; r.dst = classification_.buffer;
        r.dstOffset = static_cast<VkDeviceSize>(first);
        r.srcOffset = cursor; r.size = clsBytes; regions.push_back(r);
        cursor += clsBytes;
    }
    if (intensity) {
        std::memcpy(dst + cursor, intensity, static_cast<std::size_t>(intBytes));
        CopyRegion r; r.dst = intensity_.buffer;
        r.dstOffset = static_cast<VkDeviceSize>(first) * sizeof(float);
        r.srcOffset = cursor; r.size = intBytes; regions.push_back(r);
        cursor += intBytes;
    }
    if (normalsOct16) {
        std::memcpy(dst + cursor, normalsOct16, static_cast<std::size_t>(nrmBytes));
        CopyRegion r; r.dst = normals_.buffer;
        r.dstOffset = static_cast<VkDeviceSize>(first) * kOct16BytesPerNormal;
        r.srcOffset = cursor; r.size = nrmBytes; regions.push_back(r);
        cursor += nrmBytes;
    }
    double t3 = NowMs();
    stats = renderer_->GetUploadContext().Execute(regions, t1 - t0, t3 - t2);
    if (stats.bytes == 0) return stats;

    if (hasXyz) positionUploads_++;
    if (classification) { classificationUploads_++; classInArena_ = true; }
    if (intensity)      { intensityUploads_++;      intensityInArena_ = true; }
    if (normalsOct16)   { normalUploads_++; normalCount_ = arenaCapacity_;
                          normalBytes_ = static_cast<VkDeviceSize>(arenaCapacity_) * kOct16BytesPerNormal; }
    if (hasXyz) {
        if (first + count > pointCount_) pointCount_ = first + count;
        ++arenaTilesUploaded_;
    }
    return stats;
}

void PointCloudRenderer::ensureFallbackNormals(std::size_t count) {
    const VkDeviceSize bytes = static_cast<VkDeviceSize>(count) * kOct16BytesPerNormal;
    if (bytes == 0) return;
    if (fallbackNormals_.IsValid() && fallbackNormals_.size >= bytes) return;
    uploadZeros(renderer_, fallbackNormals_, bytes);
}

UploadStats PointCloudRenderer::UploadPositionsRenderF32(const float* xyz, std::size_t count) {
    UploadStats stats;
    const VkDeviceSize bytes = count * 3 * sizeof(float);
    if (bytes == 0) return stats;
    if (!positions_.IsValid() || positions_.size < bytes) {
        VulkanAllocator::Get().DestroyBuffer(positions_);
        positions_ = VulkanAllocator::Get().CreateBuffer(
            bytes, VK_BUFFER_USAGE_TRANSFER_DST_BIT | VK_BUFFER_USAGE_VERTEX_BUFFER_BIT,
            VMA_MEMORY_USAGE_GPU_ONLY);
        if (!positions_.IsValid()) return stats;
    }
    uint8_t* dst = renderer_->GetUploadContext().Stage(bytes);
    if (!dst) return stats;
    double t2 = NowMs();
    std::memcpy(dst, xyz, bytes);
    double t3 = NowMs();
    CopyRegion reg{positions_.buffer, 0, 0, bytes};
    stats = renderer_->GetUploadContext().Execute({reg}, 0.0, t3 - t2);
    positionUploads_++;
    pointCount_ = count;
    return stats;
}

UploadStats PointCloudRenderer::UploadColorsRGB8(const uint8_t* rgb, std::size_t count) {
    UploadStats stats;
    const VkDeviceSize bytes = count * 4;
    if (bytes == 0) return stats;
    ensureBuffer(colors_, bytes, VK_BUFFER_USAGE_VERTEX_BUFFER_BIT);
    if (!colors_.IsValid()) return stats;
    uint8_t* dst = renderer_->GetUploadContext().Stage(bytes);
    if (!dst) return stats;
    double t2 = NowMs();
    for (std::size_t i = 0; i < count; ++i) {
        dst[i * 4 + 0] = rgb[i * 3 + 0];
        dst[i * 4 + 1] = rgb[i * 3 + 1];
        dst[i * 4 + 2] = rgb[i * 3 + 2];
        dst[i * 4 + 3] = 255;
    }
    double t3 = NowMs();
    CopyRegion reg{colors_.buffer, 0, 0, bytes};
    stats = renderer_->GetUploadContext().Execute({reg}, 0.0, t3 - t2);
    colorUploads_++;
    return stats;
}

UploadStats PointCloudRenderer::UploadClassificationU8(const uint8_t* cls, std::size_t count) {
    UploadStats stats;
    const VkDeviceSize bytes = count;
    if (bytes == 0) return stats;
    // TRANSFER_DST alone is no longer enough: classification is a bound vertex
    // attribute now, so the buffer also needs VERTEX_BUFFER usage.
    ensureBuffer(classification_, bytes, VK_BUFFER_USAGE_VERTEX_BUFFER_BIT);
    if (!classification_.IsValid()) return stats;
    uint8_t* dst = renderer_->GetUploadContext().Stage(bytes);
    if (!dst) return stats;
    double t2 = NowMs();
    std::memcpy(dst, cls, bytes);
    double t3 = NowMs();
    CopyRegion reg{classification_.buffer, 0, 0, bytes};
    stats = renderer_->GetUploadContext().Execute({reg}, 0.0, t3 - t2);
    classificationUploads_++;
    classificationCount_ = count;
    return stats;
}

UploadStats PointCloudRenderer::UploadIntensityF32(const float* intensity, std::size_t count) {
    UploadStats stats;
    const VkDeviceSize bytes = count * sizeof(float);
    if (bytes == 0) return stats;
    ensureBuffer(intensity_, bytes, VK_BUFFER_USAGE_VERTEX_BUFFER_BIT);
    if (!intensity_.IsValid()) return stats;
    uint8_t* dst = renderer_->GetUploadContext().Stage(bytes);
    if (!dst) return stats;
    double t2 = NowMs();
    std::memcpy(dst, intensity, bytes);
    double t3 = NowMs();
    CopyRegion reg{intensity_.buffer, 0, 0, bytes};
    stats = renderer_->GetUploadContext().Execute({reg}, 0.0, t3 - t2);
    intensityUploads_++;
    intensityCount_ = count;
    return stats;
}

// ---------------------------------------------------------------------------
// Stored oct16x2 normals (Instant Shaded, production normal source).
//
// PACKED ON THE WIRE, DECODED ON THE GPU. The 4-byte octahedral pair is uploaded
// verbatim - it is never expanded to float32x3 on the CPU, which would cost
// 12 bytes/point and a full-buffer CPU pass for zero visual benefit. The
// vertex format is R16G16_SNORM, i.e. exactly the 2 x int16 already present,
// and instant.frag decodes it per pixel.
//
// Point order MUST match the position stream exactly; that invariant is
// verified by validate_stream_attribute_alignment.py.
// ---------------------------------------------------------------------------
UploadStats PointCloudRenderer::UploadNormalsOct16(const uint8_t* packed,
                                                  std::size_t count) {
    UploadStats stats;
    if (!packed || count == 0) return stats;
    const VkDeviceSize bytes = count * kOct16BytesPerNormal;
    if (bytes == 0) return stats;
    // VERTEX_BUFFER_BIT: the oct pair is a bound vertex attribute, exactly like
    // classification.
    ensureBuffer(normals_, bytes, VK_BUFFER_USAGE_VERTEX_BUFFER_BIT);
    if (!normals_.IsValid()) return stats;
    uint8_t* dst = renderer_->GetUploadContext().Stage(bytes);
    if (!dst) return stats;
    double t2 = NowMs();
    std::memcpy(dst, packed, static_cast<std::size_t>(bytes));
    double t3 = NowMs();
    CopyRegion reg{normals_.buffer, 0, 0, bytes};
    stats = renderer_->GetUploadContext().Execute({reg}, 0.0, t3 - t2);
    normalUploads_++;
    normalBytes_ = bytes;
    normalCount_ = count;
    return stats;
}

// ---- Push-constant state ---------------------------------------------------
// None of these touch a GPU buffer: a display-mode switch, a ramp re-range or
// a point-size tweak is 64 bytes of push constants on the next frame.

void PointCloudRenderer::SetDisplayMode(int mode) { displayMode_ = mode; }

void PointCloudRenderer::SetElevationRange(float lo, float hi, float gamma) {
    elevationLo_ = lo;
    elevationHi_ = (hi > lo) ? hi : lo + 1.0f;   // never divide by a zero span
    elevationGamma_ = gamma;
}

void PointCloudRenderer::SetIntensityRange(float lo, float hi, float contrast, float gamma) {
    intensityLo_ = lo;
    intensityHi_ = (hi > lo) ? hi : lo + 1.0f;
    intensityContrast_ = contrast;
    intensityGamma_ = gamma;
}

// DEPTH normalisation. Push-constant only. Depth is VIEW dependent, so the app
// re-derives (lo, hi) from the live camera whenever it changes; a zero span is
// widened rather than dividing by zero, which would produce NaN colours.
void PointCloudRenderer::SetDepthRange(float lo, float hi, float gamma) {
    depthLo_ = lo;
    depthHi_ = (hi > lo) ? hi : lo + 1.0f;
    depthGamma_ = gamma;
}

void PointCloudRenderer::SetPointSizeParams(float footprintMeters, float minPx,
                                           float maxPx, float classIntensityMix) {
    footprintMeters_ = footprintMeters;
    pointSizeMinPx_ = minPx;
    pointSizeMaxPx_ = (maxPx >= minPx) ? maxPx : minPx;
    classIntensityMix_ = classIntensityMix;
    pointSize_ = pointSizeMaxPx_;   // legacy accessor mirror
}

void PointCloudRenderer::SetPointSize(float pixels) {
    // Legacy fixed-size knob: pin the clamp band to one value so the shader's
    // depth term has no effect, matching the old constant gl_PointSize.
    pointSize_ = pixels;
    pointSizeMinPx_ = pixels;
    pointSizeMaxPx_ = pixels;
}

float PointCloudRenderer::GetPointSize() const { return pointSize_; }

void PointCloudRenderer::SetColorParityMode(int mode) {
    colorParityMode_ = (mode != 0) ? 1 : 0;
}

void PointCloudRenderer::SetSpriteParams(float softness, float brightness) {
    spriteSoftness_ = softness;
    brightness_ = brightness;
}

void PointCloudRenderer::Record(VkCommandBuffer cmd, uint32_t frameSlot) {
    if (!initialized_ || pointCount_ == 0 || !visible_) return;
    VkExtent2D ext = renderer_->GetExtent();
    VkViewport vp{0.0f, 0.0f, static_cast<float>(ext.width),
                     static_cast<float>(ext.height), 0.0f, 1.0f};
    VkRect2D sc{{0, 0}, {ext.width, ext.height}};
    vkCmdSetViewport(cmd, 0, 1, &vp);
    vkCmdSetScissor(cmd, 0, 1, &sc);

    // Every stream the pipeline references has to be bound. A dataset with no
    // classification or intensity channel binds the zero-filled stand-ins from
    // Initialize; if even those are missing, re-binding the (always valid, and
    // always large enough) position buffer keeps the draw legal instead of
    // handing validation a null buffer.
    //
    // PHASE 1: an attribute stream is only bound when it covers EXACTLY the
    // resident point count. Checking the byte size alone is not enough once
    // attribute streams have an independent lifetime: after a position upload
    // that changed the point count, the previous (correctly sized) class buffer
    // still satisfies `size >= needed` while describing the PREVIOUS point
    // order, which would colour every point with another point's class. An
    // exact count match makes that unrepresentable.
    auto streamFor = [](const GPUBuffer& primary, const GPUBuffer& fallback,
                        const GPUBuffer& lastResort, VkDeviceSize needed) -> VkBuffer {
        if (primary.IsValid() && primary.size >= needed) return primary.buffer;
        if (fallback.IsValid()) return fallback.buffer;
        return lastResort.buffer;
    };
    const VkDeviceSize colorBytes = static_cast<VkDeviceSize>(pointCount_) * 4;
    const VkDeviceSize classBytes = static_cast<VkDeviceSize>(pointCount_);
    const VkDeviceSize intensityBytes = static_cast<VkDeviceSize>(pointCount_) * sizeof(float);
    const bool classCovers = classInArena_ || (classificationCount_ == pointCount_);
    const bool intensityCovers = intensityInArena_ || (intensityCount_ == pointCount_);
    VkBuffer buffers[] = {
        positions_.buffer,
        streamFor(colors_, colors_, positions_, colorBytes),
        classCovers ? streamFor(classification_, fallbackClass_, positions_, classBytes)
                    : (fallbackClass_.IsValid() ? fallbackClass_.buffer
                                                : positions_.buffer),
        intensityCovers ? streamFor(intensity_, fallbackIntensity_, positions_, intensityBytes)
                        : (fallbackIntensity_.IsValid() ? fallbackIntensity_.buffer
                                                        : positions_.buffer),
    };
    VkDeviceSize offsets[] = {0, 0, 0, 0};

    vkCmdBindPipeline(cmd, VK_PIPELINE_BIND_POINT_GRAPHICS, pipeline_);
    vkCmdBindVertexBuffers(cmd, 0, 4, buffers, offsets);
    VkDescriptorSet frameSet = renderer_->GetFrameDescriptorSet();
    vkCmdBindDescriptorSets(cmd, VK_PIPELINE_BIND_POINT_GRAPHICS,
                                    pipelineLayout_, 0, 1,
                                    &frameSet, 0, nullptr);

    // How many pixels a 1 m world-space object spans at a view depth of 1 m.
    // With a column-major MVP, clip.y = mvp[1]*x + mvp[5]*y + mvp[9]*z + mvp[13],
    // so the length of that row is the projection's y-scale per world metre;
    // times half the viewport height it converts straight to pixels. Deriving it
    // from the matrix actually in use (instead of a second FOV constant the CPU
    // would have to keep in sync) is what keeps the point footprint correct
    // through zoom, resize and FOV changes.
    float mvp[16] = {};
    renderer_->GetLastMvp(mvp);
    float rowY = std::sqrt(mvp[1] * mvp[1] + mvp[5] * mvp[5] +
                           mvp[9] * mvp[9] + mvp[13] * mvp[13]);
    float pxPerMetreAt1m = rowY * 0.5f * static_cast<float>(ext.height);

    // Device ceiling: report_.maxPointSize is limits.pointSizeRange[1]
    // (e.g. 2047.9 on this GPU). A gl_PointSize above it is clamped by the
    // driver, and some drivers clamp hard (down to 1 px) instead of
    // saturating, which makes the cloud look like it vanished. Clamping to the
    // real limit keeps the footprint we ask for and honours the capability.
    //
    // VTK PARITY (point size).
    // gui/unified_actor_manager.py computes a FIXED pixel size per class
    // (compute_point_size -> weight_lut -> gl_PointSize) that does not vary with
    // camera distance, zoom, FOV or the device's point-size ceiling. The device
    // limit (e.g. 2047.9 on this GPU) is a hardware CAPABILITY, not a rendering
    // target, and must never be used as one.
    //
    // REMOVED (was the "GRID-GAP FIX"): a zoom-adaptive ceiling
    //     maxPx = max(maxPx, min(naturalPx, maxPx * 4.0))
    // that let the ceiling grow up to 4x the UI value as you zoomed in. That is
    // exactly the reported defect - Vulkan points overlap more than VTK and
    // balloon under zoom - and it also re-introduced the screen-filling blob
    // that the safety bound was itself added to prevent. Screen-space point
    // sizing has no need for it: VTK shows the same gaps at the same zoom and
    // resolves them the same way (by revealing real detail, not bigger dots).
    //
    // maxPx is now taken verbatim from the UI, with only a hardware cap so an
    // out-of-range value still rasterises.
    const float deviceMax = renderer_->GetContext().GetReport().maxPointSize;
    float maxPx = pointSizeMaxPx_;
    if (deviceMax > 1.0f) {
        maxPx = std::fmin(maxPx, deviceMax);
    }
    maxPx = std::fmax(maxPx, pointSizeMinPx_);
    maxPointSizeDevice_ = (deviceMax > 1.0f) ? deviceMax : 0.0f;

    // PointParams: must match shaders/point.vert's push-constant block exactly.
    // vColor is a display-domain byte value (palette/ramp/data RGB), so the
    // shader must pre-compensate the swapchain's sRGB encode to leave that byte
    // untouched in the framebuffer - the same rule surface.frag follows. Without
    // it every point renders 1.5-2.5x too bright against the VTK viewport.
    float pc[20] = {
        footprintMeters_, pointSizeMinPx_, maxPx, classIntensityMix_,
        static_cast<float>(displayMode_), elevationLo_, elevationHi_, elevationGamma_,
        intensityLo_, intensityHi_, intensityContrast_, intensityGamma_,
        pxPerMetreAt1m, spriteSoftness_, brightness_, maxPointSizeDevice_,
        // parity.x is the colour-space byte-parity flag. parity.y/z/w carry the
        // DEPTH normalisation (lo, hi, gamma) for NKV_DISPLAY_DEPTH. Those three
        // vec4 slots were previously pushed as literal zeros; reusing them keeps
        // PointParams at 5 vec4 / 80 bytes, so the push-constant range, the SPIR-V
        // and the C ABI are all unchanged.
        static_cast<float>(colorParityMode_), depthLo_, depthHi_, depthGamma_,
    };
    renderer_->WriteTimestamp(cmd, frameSlot, Renderer::kTsPointBefore);
    vkCmdPushConstants(cmd, pipelineLayout_,
                       VK_SHADER_STAGE_VERTEX_BIT | VK_SHADER_STAGE_FRAGMENT_BIT,
                       0, sizeof(pc), pc);
    // Screen-space LOD (NAKSHA_VULKAN_LOD=1): one vkCmdDraw per contiguous
    // visible range. The buffers are NOT re-uploaded - a range is a window
    // into the same persistent point buffer, which was uploaded in cell
    // order. With no ranges set this is byte-for-byte the previous single
    // full-buffer draw, so LOD off is exactly the old behaviour.
    if (drawRanges_.empty()) {
        // Arena residency: an empty draw list means "nothing selected". A
        // full-buffer draw would rasterise unwritten gaps between tiles.
        if (!arenaMode_) {
            vkCmdDraw(cmd, static_cast<uint32_t>(pointCount_), 1, 0, 0);
            drawCalls_++;
        }
    } else {
        for (const auto& r : drawRanges_) {
            if (r.second == 0) continue;
            vkCmdDraw(cmd, r.second, 1, r.first, 0);
            drawCalls_++;
        }
    }
    renderer_->WriteTimestamp(cmd, frameSlot, Renderer::kTsPointAfter);
}

// ---------------------------------------------------------------------------
// Instant Shaded SPLAT pass.
//
// Same persistent buffers as Record(); the differences are the 2-attachment
// pipeline (class colour + the splat's STORED oct16x2 normal) and the extra
// vertex stream. Nothing is uploaded and no state is rebuilt - switching
// between "normal point draw" and "splat draw" is a bind plus a push constant.
// ---------------------------------------------------------------------------
void PointCloudRenderer::RecordSplat(VkCommandBuffer cmd, uint32_t frameSlot) {
    // Same guards as Record(): an empty cloud or a hidden cloud draws nothing,
    // and the lighting pass then shows the viewport background.
    if (!initialized_ || pointCount_ == 0 || !visible_) return;
    if (splatPipeline_ == VK_NULL_HANDLE) return;

    VkExtent2D ext = renderer_->GetExtent();
    VkViewport vp{0.0f, 0.0f, static_cast<float>(ext.width),
                     static_cast<float>(ext.height), 0.0f, 1.0f};
    VkRect2D sc{{0, 0}, {ext.width, ext.height}};
    vkCmdSetViewport(cmd, 0, 1, &vp);
    vkCmdSetScissor(cmd, 0, 1, &sc);

    auto streamFor = [](const GPUBuffer& primary, const GPUBuffer& fallback,
                        const GPUBuffer& lastResort, VkDeviceSize needed) -> VkBuffer {
        if (primary.IsValid() && primary.size >= needed) return primary.buffer;
        if (fallback.IsValid()) return fallback.buffer;
        return lastResort.buffer;
    };
    const VkDeviceSize colorBytes = static_cast<VkDeviceSize>(pointCount_) * 4;
    const VkDeviceSize classBytes = static_cast<VkDeviceSize>(pointCount_);
    const VkDeviceSize intensityBytes = static_cast<VkDeviceSize>(pointCount_) * sizeof(float);
    const VkDeviceSize normalBytes = static_cast<VkDeviceSize>(pointCount_) * kOct16BytesPerNormal;
    // PHASE 1: same exact-coverage rule as Record(). An attribute stream that
    // does not cover the resident point count describes a different point
    // order, so it must fall back to zeros rather than be bound.
    const bool classCovers = classInArena_ || (classificationCount_ == pointCount_);
    const bool intensityCovers = intensityInArena_ || (intensityCount_ == pointCount_);
    // The 5th stream. With no normal stream resident (or one shorter than the
    // cloud) the zero-filled stand-in is bound: oct (0,0) decodes to +Z, i.e. a
    // flat up-facing surface, so an unshaded region reads as flat ground rather
    // than as noise.
    VkBuffer buffers[5] = {
        positions_.buffer,
        streamFor(colors_, colors_, positions_, colorBytes),
        classCovers ? streamFor(classification_, fallbackClass_, positions_, classBytes)
                    : (fallbackClass_.IsValid() ? fallbackClass_.buffer
                                                : positions_.buffer),
        intensityCovers ? streamFor(intensity_, fallbackIntensity_, positions_, intensityBytes)
                        : (fallbackIntensity_.IsValid() ? fallbackIntensity_.buffer
                                                        : positions_.buffer),
        streamFor(normals_, fallbackNormals_, positions_, normalBytes),
    };
    VkDeviceSize offsets[] = {0, 0, 0, 0, 0};

    vkCmdBindPipeline(cmd, VK_PIPELINE_BIND_POINT_GRAPHICS, splatPipeline_);
    vkCmdBindVertexBuffers(cmd, 0, 5, buffers, offsets);
    VkDescriptorSet frameSet = renderer_->GetFrameDescriptorSet();
    vkCmdBindDescriptorSets(cmd, VK_PIPELINE_BIND_POINT_GRAPHICS,
                            pipelineLayout_, 0, 1, &frameSet, 0, nullptr);

    float mvp[16] = {};
    renderer_->GetLastMvp(mvp);
    float rowY = std::sqrt(mvp[1] * mvp[1] + mvp[5] * mvp[5] +
                           mvp[9] * mvp[9] + mvp[13] * mvp[13]);
    float pxPerMetreAt1m = rowY * 0.5f * static_cast<float>(ext.height);

    const float deviceMax = renderer_->GetContext().GetReport().maxPointSize;
    float maxPx = pointSizeMaxPx_;
    if (deviceMax > 1.0f) maxPx = std::fmin(maxPx, deviceMax);
    maxPx = std::fmax(maxPx, pointSizeMinPx_);

    // Identical push-constant block to Record() (splat.vert declares the same
    // PointParams layout), so the band/size/lighting state cannot drift -
    // including parity.y/z/w, which carry the depth normalisation.
    float pc[20] = {
        footprintMeters_, pointSizeMinPx_, maxPx, classIntensityMix_,
        static_cast<float>(displayMode_), elevationLo_, elevationHi_, elevationGamma_,
        intensityLo_, intensityHi_, intensityContrast_, intensityGamma_,
        pxPerMetreAt1m, spriteSoftness_, brightness_, maxPointSizeDevice_,
        static_cast<float>(colorParityMode_), depthLo_, depthHi_, depthGamma_,
    };
    renderer_->WriteTimestamp(cmd, frameSlot, Renderer::kTsPointBefore);
    vkCmdPushConstants(cmd, pipelineLayout_,
                       VK_SHADER_STAGE_VERTEX_BIT | VK_SHADER_STAGE_FRAGMENT_BIT,
                       0, sizeof(pc), pc);
    if (drawRanges_.empty()) {
        // Arena residency: an empty draw list means "nothing selected". A
        // full-buffer draw would rasterise unwritten gaps between tiles.
        if (!arenaMode_) {
            vkCmdDraw(cmd, static_cast<uint32_t>(pointCount_), 1, 0, 0);
            drawCalls_++;
        }
    } else {
        for (const auto& r : drawRanges_) {
            if (r.second == 0) continue;
            vkCmdDraw(cmd, r.second, 1, r.first, 0);
            drawCalls_++;
        }
    }
    renderer_->WriteTimestamp(cmd, frameSlot, Renderer::kTsPointAfter);
}

std::size_t PointCloudRenderer::GetPointCount() const { return pointCount_; }
bool PointCloudRenderer::IsLoaded() const { return pointCount_ > 0; }
uint64_t PointCloudRenderer::GetPositionUploadCount() const { return positionUploads_; }
uint64_t PointCloudRenderer::GetColorUploadCount() const { return colorUploads_; }
uint64_t PointCloudRenderer::GetClassificationUploadCount() const { return classificationUploads_; }
uint64_t PointCloudRenderer::GetIntensityUploadCount() const { return intensityUploads_; }
VkDeviceSize PointCloudRenderer::GetGpuBytes() const {
    return positions_.size + colors_.size + classification_.size + intensity_.size +
           fallbackClass_.size + fallbackIntensity_.size;
}

void PointCloudRenderer::OnSwapchainRebuild() {
    if (!initialized_ || !renderer_) return;
    renderer_->Pipelines().DestroyPipeline(pipeline_);
    pipeline_ = VK_NULL_HANDLE;
    std::vector<ShaderModule> shaders{vs_, fs_};
    pipeline_ = renderer_->Pipelines().CreateGraphicsPipeline(
        pipelineLayout_, shaders, pipelineCfg_, renderer_->GetRenderPass(), 0);
}

} // namespace naksha
