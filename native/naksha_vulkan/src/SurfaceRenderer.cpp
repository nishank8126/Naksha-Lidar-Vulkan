#include "naksha/SurfaceRenderer.hpp"
#include "naksha/Renderer.hpp"
#include <cstring>
#include <cmath>

namespace naksha {

SurfaceRenderer::~SurfaceRenderer() { Shutdown(); }

void SurfaceLodBuffers::Release() {
    if (cellPool != VK_NULL_HANDLE) vkDestroyDescriptorPool(cellDevice, cellPool, nullptr);
    cellPool = VK_NULL_HANDLE; cellSet = VK_NULL_HANDLE;
    VulkanAllocator::Get().DestroyBuffer(cellColors);
    cellColors = {};
    VulkanAllocator::Get().DestroyBuffer(positions);
    VulkanAllocator::Get().DestroyBuffer(normals);
    VulkanAllocator::Get().DestroyBuffer(colors);
    VulkanAllocator::Get().DestroyBuffer(indices);
    positions = {}; normals = {}; colors = {}; indices = {};
}

void SurfaceResourceSet::ReleaseBuffers() {
    for (uint32_t i = 0; i < kSurfaceLodCount; ++i) {
        lods[i].Release();
        visibleTiles[i].clear();
    }
    lodMeshes.clear();
    gpuBytes = 0;
}

static VkVertexInputBindingDescription surfBindings[3] = {
    {0, sizeof(float) * 3, VK_VERTEX_INPUT_RATE_VERTEX},
    {1, sizeof(float) * 3, VK_VERTEX_INPUT_RATE_VERTEX},
    {2, sizeof(uint8_t) * 4, VK_VERTEX_INPUT_RATE_VERTEX},
};
static VkVertexInputAttributeDescription surfAttributes[3] = {
    {0, 0, VK_FORMAT_R32G32B32_SFLOAT, 0},
    {1, 1, VK_FORMAT_R32G32B32_SFLOAT, 0},
    {2, 2, VK_FORMAT_R8G8B8A8_UNORM, 0},
};

bool SurfaceRenderer::Initialize(Renderer& renderer) {
    renderer_ = &renderer;
    wireframeSupported_ = renderer.GetContext().GetReport().fillModeNonSolid != VK_FALSE;
    // Resolve the safe VRAM budget once, here, so EstimateRequiredBytes() /
    // WouldAcceptUpload() can answer before the first upload happens.
    // 70% of the device-local heap leaves room for the swapchain, depth,
    // staging and driver overhead while a swap has ACTIVE and PENDING both
    // resident.
    const uint64_t vram = renderer.GetContext().GetReport().vramBytes;
    if (vram > 0) vramBudgetBytes_ = static_cast<double>(vram) * 0.70;

    std::string vsPath = renderer.cfg_.shaderDirectory + "/surface.vert.spv";
    std::string fsPath = renderer.cfg_.shaderDirectory + "/surface.frag.spv";
    vs_ = renderer.Shaders().LoadSPIRV(vsPath, VK_SHADER_STAGE_VERTEX_BIT);
    fsSolid_ = renderer.Shaders().LoadSPIRV(fsPath, VK_SHADER_STAGE_FRAGMENT_BIT);
    fsWireframe_ = fsSolid_;
    if (vs_.module == VK_NULL_HANDLE || fsSolid_.module == VK_NULL_HANDLE) return false;

    VkPushConstantRange pcr{};
    pcr.stageFlags = VK_SHADER_STAGE_VERTEX_BIT | VK_SHADER_STAGE_FRAGMENT_BIT;
    pcr.offset = 0;
    pcr.size = sizeof(PushConstants);
    pipelineLayout_ = renderer.Pipelines().CreatePipelineLayout(
        {renderer.GetFrameSetLayout()}, {pcr});
    if (pipelineLayout_ == VK_NULL_HANDLE) return false;

    pipelineCfg_ = PipelineConfig{};
    pipelineCfg_.SetDefaults();
    pipelineCfg_.vertexBindings = {surfBindings[0], surfBindings[1], surfBindings[2]};
    pipelineCfg_.vertexAttributes = {surfAttributes[0], surfAttributes[1], surfAttributes[2]};
    pipelineCfg_.vertexInput.vertexBindingDescriptionCount = 3;
    pipelineCfg_.vertexInput.pVertexBindingDescriptions = surfBindings;
    pipelineCfg_.vertexInput.vertexAttributeDescriptionCount = 3;
    pipelineCfg_.vertexInput.pVertexAttributeDescriptions = surfAttributes;
    pipelineCfg_.inputAssembly.topology = VK_PRIMITIVE_TOPOLOGY_TRIANGLE_LIST;
    pipelineCfg_.inputAssembly.primitiveRestartEnable = VK_FALSE;
    // No back-face culling, matching the VTK side this renderer must agree
    // with: gui/shading_display.py explicitly calls BackfaceCullingOff()/
    // FrontfaceCullingOff() on every shaded-mesh actor. It is also required
    // for correctness here: a triangle wound counter-clockwise as seen from
    // above (the CPU mesh builder's convention, and VTK's front face in a
    // y-up screen space) has a NEGATIVE signed area in Vulkan framebuffer
    // coordinates, whose y axis points down - with CULL_MODE_BACK_BIT the
    // entire terrain would be culled away and the viewport would show only
    // the clear colour.
    pipelineCfg_.rasterizer.cullMode = VK_CULL_MODE_NONE;
    pipelineCfg_.rasterizer.frontFace = VK_FRONT_FACE_COUNTER_CLOCKWISE;
    pipelineCfg_.depthStencil.depthCompareOp = VK_COMPARE_OP_LESS;
    pipelineCfg_.colorBlendAttachment.blendEnable = VK_FALSE;
    pipelineCfg_.colorBlending.attachmentCount = 1;
    pipelineCfg_.colorBlending.pAttachments = &pipelineCfg_.colorBlendAttachment;
    pipelineCfg_.colorFormat = VK_FORMAT_B8G8R8A8_SRGB;
    pipelineCfg_.depthFormat = VK_FORMAT_D32_SFLOAT;

    std::vector<ShaderModule> solid{vs_, fsSolid_};
    pipelineSolid_ = renderer.Pipelines().CreateGraphicsPipeline(
        pipelineLayout_, solid, pipelineCfg_, renderer.GetRenderPass(), 0);

    // PASS 2 overlay pipeline: identical to the base pipeline except for the
    // depth comparison. The overlay draws the SAME triangles (bit-identical
    // vertex positions, built from the same face rows), so with the base
    // pipeline's VK_COMPARE_OP_LESS every overlay fragment failed against the
    // depth the base pass had just written and the mixed-class barycentric blend
    // never appeared on screen. LESS_OR_EQUAL lets the later draw win on the
    // shared depth, which is the same intent as the polygon offset VTK applies
    // to these actors in gui/shading_display.py.
    pipelineCfg_.depthStencil.depthCompareOp = VK_COMPARE_OP_LESS_OR_EQUAL;
    pipelineSolidOverlay_ = renderer.Pipelines().CreateGraphicsPipeline(
        pipelineLayout_, solid, pipelineCfg_, renderer.GetRenderPass(), 0);

    pipelineCfg_.rasterizer.polygonMode = VK_POLYGON_MODE_LINE;
    pipelineCfg_.rasterizer.lineWidth = 1.0f;
    std::vector<ShaderModule> wire{vs_, fsWireframe_};
    pipelineWireframe_ = renderer.Pipelines().CreateGraphicsPipeline(
        pipelineLayout_, wire, pipelineCfg_, renderer.GetRenderPass(), 0);

    if (renderer.GetContext().CoreDevice().GetEnabledFeatures().geometryShader) {
        indexedVs_ = renderer.Shaders().LoadSPIRV(renderer.cfg_.shaderDirectory + "/surface_indexed.vert.spv", VK_SHADER_STAGE_VERTEX_BIT);
        indexedFs_ = renderer.Shaders().LoadSPIRV(renderer.cfg_.shaderDirectory + "/surface_indexed.frag.spv", VK_SHADER_STAGE_FRAGMENT_BIT);
        cellLayout_ = renderer.Descriptors().CreateLayout({{0, VK_DESCRIPTOR_TYPE_STORAGE_BUFFER, 1, VK_SHADER_STAGE_FRAGMENT_BIT}});
        VkPushConstantRange indexedPush{VK_SHADER_STAGE_FRAGMENT_BIT, 0, sizeof(uint32_t)};
        indexedLayout_ = renderer.Pipelines().CreatePipelineLayout({renderer.GetFrameSetLayout(), cellLayout_}, {indexedPush});
        PipelineConfig indexed = pipelineCfg_;
        indexed.rasterizer.polygonMode = VK_POLYGON_MODE_FILL;
        indexed.depthStencil.depthCompareOp = VK_COMPARE_OP_LESS;
        indexed.vertexBindings = {surfBindings[0]};
        indexed.vertexAttributes = {surfAttributes[0]};
        indexed.vertexInput.vertexBindingDescriptionCount = 1;
        indexed.vertexInput.pVertexBindingDescriptions = surfBindings;
        indexed.vertexInput.vertexAttributeDescriptionCount = 1;
        indexed.vertexInput.pVertexAttributeDescriptions = surfAttributes;
        indexedPipeline_ = renderer.Pipelines().CreateGraphicsPipeline(indexedLayout_, {indexedVs_, indexedFs_}, indexed, renderer.GetRenderPass(), 0);
        if (wireframeSupported_) {
            indexed.rasterizer.polygonMode = VK_POLYGON_MODE_LINE;
            indexedWireframePipeline_ = renderer.Pipelines().CreateGraphicsPipeline(indexedLayout_, {indexedVs_, indexedFs_}, indexed, renderer.GetRenderPass(), 0);
        }
    }
    initialized_ = true;
    return (pipelineSolid_ != VK_NULL_HANDLE && pipelineSolidOverlay_ != VK_NULL_HANDLE);
}

void SurfaceRenderer::Shutdown() {
    if (!initialized_) return;
    initialized_ = false;
    if (renderer_) {
        renderer_->Pipelines().DestroyPipeline(pipelineSolid_);
        renderer_->Pipelines().DestroyPipeline(indexedPipeline_);
        renderer_->Pipelines().DestroyPipeline(indexedWireframePipeline_);
        renderer_->Pipelines().DestroyPipelineLayout(indexedLayout_);
        renderer_->Descriptors().DestroyLayout(cellLayout_);
        renderer_->Shaders().DestroyShaderModule(indexedVs_.module);
        renderer_->Shaders().DestroyShaderModule(indexedFs_.module);
        renderer_->Pipelines().DestroyPipeline(pipelineSolidOverlay_);
        renderer_->Pipelines().DestroyPipeline(pipelineWireframe_);
        renderer_->Pipelines().DestroyPipelineLayout(pipelineLayout_);
        renderer_->Shaders().DestroyShaderModule(vs_.module);
        // fsWireframe_ is the same handle as fsSolid_ (aliased in
        // Initialize() - there's only one fragment shader, shared by both
        // pipelines), so destroying it a second time here was a double-free
        // that the validation layer flagged as "invalid"/"couldn't find".
        renderer_->Shaders().DestroyShaderModule(fsSolid_.module);
    }
    VulkanAllocator::Get().DestroyBuffer(positions_);
    VulkanAllocator::Get().DestroyBuffer(normals_);
    VulkanAllocator::Get().DestroyBuffer(colors_);
    VulkanAllocator::Get().DestroyBuffer(indices_);
    // Tiled resource sets (ACTIVE / PENDING / retired). Shutdown is the one
    // place a device idle is legitimate: the device is going away.
    if (active_) { active_->ReleaseBuffers(); active_.reset(); }
    if (pending_) { pending_->ReleaseBuffers(); pending_.reset(); }
    for (auto& r : retired_) { if (r) r->ReleaseBuffers(); }
    retired_.clear();
    VulkanAllocator::Get().DestroyBuffer(positions2_);
    VulkanAllocator::Get().DestroyBuffer(normals2_);
    VulkanAllocator::Get().DestroyBuffer(colors2_);
    VulkanAllocator::Get().DestroyBuffer(indices2_);
    // GPU class-colour LUT (Phase C): 1 KB, own lifetime.
    VulkanAllocator::Get().DestroyBuffer(classLut_);
    classLut_ = {};
    positions_ = {}; normals_ = {}; colors_ = {}; indices_ = {};
    positions2_ = {}; normals2_ = {}; colors2_ = {}; indices2_ = {};
    overlayIndexCount_ = 0; crispHybridActive_ = false;
    pipelineSolid_ = VK_NULL_HANDLE; pipelineWireframe_ = VK_NULL_HANDLE;
    pipelineSolidOverlay_ = VK_NULL_HANDLE;
    pipelineLayout_ = VK_NULL_HANDLE;
    vs_ = {}; fsSolid_ = {}; fsWireframe_ = {};
    renderer_ = nullptr;
}

UploadStats SurfaceRenderer::UploadMesh(const float* positions, const float* normals,
                                              const uint8_t* colorsRGB,
                                              std::size_t vertexCount,
                                              const uint32_t* indices, std::size_t indexCount) {
    UploadStats stats;
    vertexCount_ = vertexCount;
    triangleCount_ = indexCount / 3;
    indexCount_ = indexCount;

    auto uploadOne = [&](GPUBuffer& buf, VkBufferUsageFlags extra,
                             VkDeviceSize bytes, const void* data) {
        UploadStats s;
        if (bytes == 0) return s;
        if (!buf.IsValid() || buf.size < bytes) {
            VulkanAllocator::Get().DestroyBuffer(buf);
            buf = VulkanAllocator::Get().CreateBuffer(
                bytes, VK_BUFFER_USAGE_TRANSFER_DST_BIT | extra,
                VMA_MEMORY_USAGE_GPU_ONLY);
            if (!buf.IsValid()) return s;
        }
        uint8_t* dst = renderer_->GetUploadContext().Stage(bytes);
        if (!dst) return s;
        double t2 = NowMs();
        std::memcpy(dst, data, bytes);
        double t3 = NowMs();
        CopyRegion reg{buf.buffer, 0, 0, bytes};
        s = renderer_->GetUploadContext().Execute({reg}, 0.0, t3 - t2);
        stats.Accumulate(s);
        return s;
    };

    uploadOne(positions_, VK_BUFFER_USAGE_VERTEX_BUFFER_BIT,
              vertexCount * 3 * sizeof(float), positions);
    uploadOne(normals_, VK_BUFFER_USAGE_VERTEX_BUFFER_BIT,
              vertexCount * 3 * sizeof(float), normals);
    uploadOne(colors_, VK_BUFFER_USAGE_VERTEX_BUFFER_BIT,
              vertexCount * 4, colorsRGB);
    if (indexCount > 0) {
        uploadOne(indices_, VK_BUFFER_USAGE_INDEX_BUFFER_BIT,
                  indexCount * sizeof(uint32_t), indices);
    }

    meshUploads_++;
    return stats;
}

UploadStats SurfaceRenderer::UploadShadedClassMesh(
        const float* basePositions, const float* baseNormals, const uint8_t* baseColorsRGB,
        std::size_t baseVertexCount, const uint32_t* baseIndices, std::size_t baseIndexCount,
        const float* overlayPositions, const float* overlayNormals, const uint8_t* overlayColorsRGB,
        std::size_t overlayVertexCount, const uint32_t* overlayIndices, std::size_t overlayIndexCount) {
    UploadStats stats;

    auto uploadOne = [&](GPUBuffer& buf, VkBufferUsageFlags extra,
                             VkDeviceSize bytes, const void* data) {
        UploadStats s;
        if (bytes == 0) return s;
        if (!buf.IsValid() || buf.size < bytes) {
            VulkanAllocator::Get().DestroyBuffer(buf);
            buf = VulkanAllocator::Get().CreateBuffer(
                bytes, VK_BUFFER_USAGE_TRANSFER_DST_BIT | extra,
                VMA_MEMORY_USAGE_GPU_ONLY);
            if (!buf.IsValid()) return s;
        }
        uint8_t* dst = renderer_->GetUploadContext().Stage(bytes);
        if (!dst) return s;
        double t2 = NowMs();
        std::memcpy(dst, data, bytes);
        double t3 = NowMs();
        CopyRegion reg{buf.buffer, 0, 0, bytes};
        s = renderer_->GetUploadContext().Execute({reg}, 0.0, t3 - t2);
        stats.Accumulate(s);
        return s;
    };

    // PASS 1 base - reuses the same positions_/normals_/colors_/indices_
    // buffers UploadMesh() uses, so Record()'s existing bind/draw code for
    // the "single mesh" surface_mode.py path needs no changes.
    vertexCount_ = baseVertexCount;
    indexCount_ = baseIndexCount;
    triangleCount_ = baseIndexCount / 3;
    uploadOne(positions_, VK_BUFFER_USAGE_VERTEX_BUFFER_BIT,
              baseVertexCount * 3 * sizeof(float), basePositions);
    uploadOne(normals_, VK_BUFFER_USAGE_VERTEX_BUFFER_BIT,
              baseVertexCount * 3 * sizeof(float), baseNormals);
    uploadOne(colors_, VK_BUFFER_USAGE_VERTEX_BUFFER_BIT,
              baseVertexCount * 4, baseColorsRGB);
    if (baseIndexCount > 0) {
        uploadOne(indices_, VK_BUFFER_USAGE_INDEX_BUFFER_BIT,
                  baseIndexCount * sizeof(uint32_t), baseIndices);
    }

    // PASS 2 overlay - separate buffers, only the mixed-class faces.
    overlayIndexCount_ = overlayIndexCount;
    if (overlayVertexCount > 0) {
        uploadOne(positions2_, VK_BUFFER_USAGE_VERTEX_BUFFER_BIT,
                  overlayVertexCount * 3 * sizeof(float), overlayPositions);
        uploadOne(normals2_, VK_BUFFER_USAGE_VERTEX_BUFFER_BIT,
                  overlayVertexCount * 3 * sizeof(float), overlayNormals);
        uploadOne(colors2_, VK_BUFFER_USAGE_VERTEX_BUFFER_BIT,
                  overlayVertexCount * 4, overlayColorsRGB);
    }
    if (overlayIndexCount > 0) {
        uploadOne(indices2_, VK_BUFFER_USAGE_INDEX_BUFFER_BIT,
                  overlayIndexCount * sizeof(uint32_t), overlayIndices);
    }

    crispHybridActive_ = true;
    meshUploads_++;
    return stats;
}

// ---- GPU class-colour LUT (Phase C) --------------------------------------
//
// A palette change must be a 1 KB upload and nothing else. This deliberately
// does NOT call UploadMesh/UploadShadedClassMesh, does not touch
// positions_/normals_/indices_, and does NOT increment meshUploads_ -- so the
// "palette change is not a geometry change" invariant is provable from
// GetMeshUploadCount() alone.
UploadStats SurfaceRenderer::SetClassColorLut(const uint8_t* colorsRGB) {
    UploadStats stats;
    if (!colorsRGB || !initialized_) return stats;

    // Expand RGB -> RGBA8 (alpha 255) so the shader indexes with a byte stride.
    std::vector<uint8_t> rgba(kClassLutEntries * 4);
    for (std::size_t i = 0; i < kClassLutEntries; ++i) {
        rgba[i * 4 + 0] = colorsRGB[i * 3 + 0];
        rgba[i * 4 + 1] = colorsRGB[i * 3 + 1];
        rgba[i * 4 + 2] = colorsRGB[i * 3 + 2];
        rgba[i * 4 + 3] = 255;
    }
    const std::size_t bytes = rgba.size();
    if (!classLut_.IsValid()) {
        classLut_ = VulkanAllocator::Get().CreateBuffer(
            bytes, VK_BUFFER_USAGE_TRANSFER_DST_BIT | VK_BUFFER_USAGE_STORAGE_BUFFER_BIT,
            VMA_MEMORY_USAGE_GPU_ONLY);
        if (!classLut_.IsValid()) return stats;
    }
    uint8_t* dst = renderer_->GetUploadContext().Stage(bytes);
    if (!dst) return stats;
    const double t2 = NowMs();
    std::memcpy(dst, rgba.data(), bytes);
    const double t3 = NowMs();
    CopyRegion reg{classLut_.buffer, 0, 0, bytes};
    stats = renderer_->GetUploadContext().Execute({reg}, 0.0, t3 - t2);
    classLutUpdates_++;
    lutRevision_++;
    return stats;
}

void SurfaceRenderer::SetShadingParityParams(int colorMode, int debugStage,
                                             float ambientFloor,
                                             float baseLightElevationDeg) {
    parityColorMode_ = (colorMode != 0) ? 1 : 0;
    parityDebugStage_ = (debugStage < 0) ? 0 : debugStage;
    parityAmbientFloor_ = ambientFloor;
    parityBaseElevationDeg_ = baseLightElevationDeg;
    parityParamUpdates_++;
}

void SurfaceRenderer::SetCrispShadingParams(float azimuthDeg, float sharpnessRaw, float ambient,
                                             float keyIntensity, float fillIntensity) {
    crispAzimuthDeg_ = azimuthDeg;
    crispSharpnessRaw_ = sharpnessRaw;
    crispAmbient_ = ambient;
    crispKeyIntensity_ = keyIntensity;
    crispFillIntensity_ = fillIntensity;
    shadeParamUpdates_++;
}

void SurfaceRenderer::SetShadingMode(ShadingMode mode) {
    if (shadingMode_ != mode) modeSwitches_++;
    shadingMode_ = mode;
}
ShadingMode SurfaceRenderer::GetShadingMode() const { return shadingMode_; }

void SurfaceRenderer::SetLightDirection(float x, float y, float z) {
    lightDir_[0] = x; lightDir_[1] = y; lightDir_[2] = z;
}
void SurfaceRenderer::SetAmbient(float ambient) { ambient_ = ambient; }
void SurfaceRenderer::SetElevationRange(float zMin, float zMax) {
    zMin_ = zMin; zMax_ = zMax;
}

// ---------------------------------------------------------------------------
// Tiled / LOD-aware upload with an atomic ACTIVE <-> PENDING swap
// ---------------------------------------------------------------------------
namespace {

// Uploads one LOD level's four arrays in a SINGLE staging batch + copy
// submit. Returns false if any buffer could not be created.
bool UploadOneLevel(Renderer& renderer, SurfaceLodBuffers& dst,
                    const SurfaceLodMesh& mesh, UploadStats& stats) {
    const VkDeviceSize posBytes = mesh.positions.size() * sizeof(float);
    const VkDeviceSize nrmBytes = mesh.normals.size() * sizeof(float);
    const VkDeviceSize colBytes = mesh.colors.size() * sizeof(uint8_t);
    const VkDeviceSize idxBytes = mesh.indices.size() * sizeof(uint32_t);
    const VkDeviceSize total = posBytes + nrmBytes + colBytes + idxBytes;
    if (total == 0) return false;

    dst.positions = VulkanAllocator::Get().CreateBuffer(
        posBytes, VK_BUFFER_USAGE_TRANSFER_DST_BIT | VK_BUFFER_USAGE_VERTEX_BUFFER_BIT,
        VMA_MEMORY_USAGE_GPU_ONLY);
    dst.normals = VulkanAllocator::Get().CreateBuffer(
        nrmBytes, VK_BUFFER_USAGE_TRANSFER_DST_BIT | VK_BUFFER_USAGE_VERTEX_BUFFER_BIT,
        VMA_MEMORY_USAGE_GPU_ONLY);
    dst.colors = VulkanAllocator::Get().CreateBuffer(
        colBytes, VK_BUFFER_USAGE_TRANSFER_DST_BIT | VK_BUFFER_USAGE_VERTEX_BUFFER_BIT,
        VMA_MEMORY_USAGE_GPU_ONLY);
    dst.indices = VulkanAllocator::Get().CreateBuffer(
        idxBytes, VK_BUFFER_USAGE_TRANSFER_DST_BIT | VK_BUFFER_USAGE_INDEX_BUFFER_BIT,
        VMA_MEMORY_USAGE_GPU_ONLY);
    if (!dst.IsValid()) { dst.Release(); return false; }

    // The staging buffer is one grow-only allocation, so all four regions are
    // packed back to back and copied in a single submit.
    uint8_t* stage = renderer.GetUploadContext().Stage(total);
    if (!stage) { dst.Release(); return false; }
    std::memcpy(stage + 0, mesh.positions.data(), posBytes);
    std::memcpy(stage + posBytes, mesh.normals.data(), nrmBytes);
    std::memcpy(stage + posBytes + nrmBytes, mesh.colors.data(), colBytes);
    std::memcpy(stage + posBytes + nrmBytes + colBytes, mesh.indices.data(), idxBytes);

    const std::vector<CopyRegion> regions{
        {dst.positions.buffer, 0, 0,                             posBytes},
        {dst.normals.buffer,   0, posBytes,                      nrmBytes},
        {dst.colors.buffer,    0, posBytes + nrmBytes,           colBytes},
        {dst.indices.buffer,   0, posBytes + nrmBytes + colBytes, idxBytes},
    };
    stats.Accumulate(renderer.GetUploadContext().Execute(regions, 0.0, 0.0));
    return true;
}

} // namespace

SurfaceRenderer::UploadTiledResult SurfaceRenderer::UploadTiledMesh(
        const float* positions, const float* normals, const uint8_t* colors,
        std::size_t triangleCount, uint32_t targetTileCount, float lodCellMeters,
        bool isPreview, uint64_t datasetRevision, uint64_t surfaceRevision) {
    UploadTiledResult res;
    if (!renderer_ || !initialized_ || !positions || triangleCount == 0) return res;

    const std::size_t vertexCount = triangleCount * 3;
    const std::size_t indexCount = triangleCount * 3;

    // ---- CPU side: tile level 0, then cluster the coarse levels ------------
    // Level 0 is a PERMUTATION of the input soup, so the final visual result
    // is identical to the untiled upload - tiling changes only the ORDER.
    std::vector<SurfaceLodMesh> levels(kSurfaceLodCount);
    levels[0].positions.assign(positions, positions + vertexCount * 3);
    levels[0].normals = normals ? std::vector<float>(normals, normals + vertexCount * 3)
                                : std::vector<float>(vertexCount * 3, 0.0f);
    levels[0].colors = colors ? std::vector<uint8_t>(colors, colors + vertexCount * 4)
                              : std::vector<uint8_t>(vertexCount * 4, 255);
    levels[0].vertexCount = vertexCount;
    levels[0].triangleCount = triangleCount;
    levels[0].cellSizeMeters = lodCellMeters;

    // The soup's implicit index is the identity; materialise it once.
    std::vector<uint32_t> identity(indexCount);
    for (std::size_t i = 0; i < indexCount; ++i) identity[i] = static_cast<uint32_t>(i);
    BuildSurfaceTiles(levels[0].positions.data(), vertexCount,
                      identity.data(), indexCount, targetTileCount,
                      levels[0].indices, levels[0].tiles);
    if (levels[0].indices.empty() || levels[0].tiles.empty()) {
        fprintf(stderr, "[SurfaceRenderer] tiling produced no tiles (%zu tris)\n",
                triangleCount);
        return res;
    }

    for (uint32_t lod = 1; lod < kSurfaceLodCount; ++lod) {
        // 4x then 16x the base cell size: a 2x then 4x reduction per axis,
        // which gives the projected-error test real levels to choose between
        // rather than a single binary choice.
        const float cell = lodCellMeters * (4.0f * static_cast<float>(lod * lod));
        if (!(cell > 0.0f)) continue;
        SurfaceLodMesh coarse;
        if (!ClusterSurfaceLod(levels[0].positions.data(), vertexCount,
                               levels[0].indices.data(), levels[0].indices.size(),
                               levels[0].colors.data(), cell, coarse)) {
            continue;   // level stays empty; LOD selection skips it
        }
        // Re-tile the coarse mesh so it is independently cullable too.
        BuildSurfaceTiles(coarse.positions.data(), coarse.vertexCount,
                          coarse.indices.data(), coarse.indices.size(),
                          targetTileCount, coarse.indices, coarse.tiles);
        levels[lod] = std::move(coarse);
    }

    // ---- Memory budget (Part 11) ------------------------------------------
    // An atomic swap means ACTIVE and PENDING coexist, so the check must
    // account for BOTH plus the incoming set. Refusing here keeps the current
    // surface on screen instead of failing a VMA allocation mid-copy.
    VkDeviceSize needed = 0;
    for (uint32_t lod = 0; lod < kSurfaceLodCount; ++lod) {
        const SurfaceLodMesh& m = levels[lod];
        if (m.triangleCount == 0) continue;
        needed += static_cast<VkDeviceSize>(m.positions.size() * sizeof(float));
        needed += static_cast<VkDeviceSize>(m.normals.size() * sizeof(float));
        needed += static_cast<VkDeviceSize>(m.colors.size() * sizeof(uint8_t));
        needed += static_cast<VkDeviceSize>(m.indices.size() * sizeof(uint32_t));
    }
    const uint64_t vramTotal = renderer_->GetContext().GetReport().vramBytes;
    if (vramBudgetBytes_ <= 0.0 && vramTotal > 0) {
        vramBudgetBytes_ = static_cast<double>(vramTotal) * 0.70;
    }
    const double resident = static_cast<double>(GetActiveBytes()) +
                            static_cast<double>(GetPendingBytes());
    if (vramBudgetBytes_ > 0.0 && (resident + static_cast<double>(needed)) > vramBudgetBytes_) {
        memoryRejected_ = true;
        res.memoryRejected = true;
        fprintf(stderr,
                "[SurfaceRenderer] upload REJECTED by VRAM budget: resident "
                "%.0f MB + new %.0f MB > budget %.0f MB (70%% of %.0f MB device)\n",
                resident / 1048576.0, static_cast<double>(needed) / 1048576.0,
                vramBudgetBytes_ / 1048576.0, static_cast<double>(vramTotal) / 1048576.0);
        return res;
    }
    memoryRejected_ = false;

    auto set = std::make_unique<SurfaceResourceSet>();
    set->vertexCount = vertexCount;
    set->indexCount = indexCount;
    set->triangleCount = triangleCount;
    set->isPreview = isPreview;
    set->datasetRevision = datasetRevision;
    set->surfaceRevision = surfaceRevision;
    set->totalTiles = static_cast<uint32_t>(levels[0].tiles.size());

    for (uint32_t lod = 0; lod < kSurfaceLodCount; ++lod) {
        if (levels[lod].triangleCount == 0) continue;
        if (!UploadOneLevel(*renderer_, set->lods[lod], levels[lod], res.stats)) {
            set->ReleaseBuffers();
            return res;
        }
        set->gpuBytes += set->lods[lod].Bytes();
        res.lodTriangles[lod] = levels[lod].triangleCount;
    }
    if (!set->IsValid()) { set->ReleaseBuffers(); return res; }

    set->lodMeshes = std::move(levels);
    res.tileCount = set->totalTiles;

    // Supersede any previous pending set; it is retired, not destroyed here.
    if (pending_) retired_.push_back(std::move(pending_));
    pending_ = std::move(set);
    meshUploads_++;

    const VkDeviceSize peak = GetActiveBytes() + GetPendingBytes();
    if (peak > peakBytesDuringSwap_) peakBytesDuringSwap_ = peak;
    res.peakBytesDuringSwap = static_cast<double>(peak);
    res.ok = true;
    return res;
}

// ---------------------------------------------------------------------------
// Atomic swap, retirement, culling and telemetry
// ---------------------------------------------------------------------------
SurfaceRenderer::UploadTiledResult SurfaceRenderer::UploadIndexedBlocks(
        const float* positions, std::size_t vertexCount, const uint32_t* indices,
        std::size_t indexCount, const uint32_t* cellColors,
        const uint32_t* blockCounts, uint32_t blockCount,
        uint64_t datasetRevision, uint64_t surfaceRevision) {
    UploadTiledResult result;
    if (!initialized_ || indexedPipeline_ == VK_NULL_HANDLE || !positions || !indices ||
        !cellColors || !blockCounts || !vertexCount || !indexCount || indexCount % 3) return result;
    uint64_t count = 0;
    for (uint32_t i = 0; i < blockCount; ++i) count += blockCounts[i];
    if (count != indexCount) return result;
    for (std::size_t i = 0; i < indexCount; ++i) if (indices[i] >= vertexCount) return result;
    const VkDeviceSize posBytes = vertexCount * 12, idxBytes = indexCount * 4, cellBytes = indexCount / 3 * 4;
    const VkDeviceSize required = posBytes + idxBytes + cellBytes;
    if (GetActiveBytes() + GetPendingBytes() + required > vramBudgetBytes_) {
        memoryRejected_ = result.memoryRejected = true; return result;
    }
    auto set = std::make_unique<SurfaceResourceSet>();
    set->indexedCells = true; set->datasetRevision = datasetRevision; set->surfaceRevision = surfaceRevision;
    set->vertexCount = vertexCount; set->indexCount = indexCount; set->triangleCount = indexCount / 3;
    SurfaceLodMesh mesh;
    mesh.positions.assign(positions, positions + vertexCount * 3);
    mesh.indices.assign(indices, indices + indexCount);
    mesh.vertexCount = vertexCount; mesh.triangleCount = indexCount / 3;
    uint32_t first = 0;
    for (uint32_t i = 0; i < blockCount; ++i) {
        SurfaceTile tile; tile.firstIndex = first; tile.indexCount = blockCounts[i];
        tile.triangleCount = blockCounts[i] / 3; tile.residency = 1;
        if (tile.indexCount) {
            for (int k = 0; k < 3; ++k) tile.bmin[k] = tile.bmax[k] = positions[indices[first] * 3 + k];
            for (uint32_t j = first; j < first + tile.indexCount; ++j)
                for (int k = 0; k < 3; ++k) {
                    float value = positions[indices[j] * 3 + k];
                    tile.bmin[k] = std::min(tile.bmin[k], value); tile.bmax[k] = std::max(tile.bmax[k], value);
                }
            mesh.tiles.push_back(tile);
        }
        first += tile.indexCount;
    }
    set->lodMeshes.resize(kSurfaceLodCount); set->lodMeshes[0] = std::move(mesh);
    set->totalTiles = static_cast<uint32_t>(set->lodMeshes[0].tiles.size());
    auto& dst = set->lods[0]; auto& allocator = VulkanAllocator::Get();
    dst.positions = allocator.CreateBuffer(posBytes, VK_BUFFER_USAGE_TRANSFER_DST_BIT | VK_BUFFER_USAGE_VERTEX_BUFFER_BIT, VMA_MEMORY_USAGE_GPU_ONLY);
    dst.indices = allocator.CreateBuffer(idxBytes, VK_BUFFER_USAGE_TRANSFER_DST_BIT | VK_BUFFER_USAGE_INDEX_BUFFER_BIT, VMA_MEMORY_USAGE_GPU_ONLY);
    dst.cellColors = allocator.CreateBuffer(cellBytes, VK_BUFFER_USAGE_TRANSFER_DST_BIT | VK_BUFFER_USAGE_STORAGE_BUFFER_BIT, VMA_MEMORY_USAGE_GPU_ONLY);
    if (!dst.IsValid() || !dst.cellColors.IsValid()) { set->ReleaseBuffers(); return result; }
    auto* staging = renderer_->GetUploadContext().Stage(required);
    if (!staging) { set->ReleaseBuffers(); return result; }
    std::memcpy(staging, positions, posBytes); std::memcpy(staging + posBytes, indices, idxBytes);
    std::memcpy(staging + posBytes + idxBytes, cellColors, cellBytes);
    result.stats.Accumulate(renderer_->GetUploadContext().Execute({
        {dst.positions.buffer, 0, 0, posBytes}, {dst.indices.buffer, 0, posBytes, idxBytes},
        {dst.cellColors.buffer, 0, posBytes + idxBytes, cellBytes}}, 0.0, 0.0));
    dst.cellDevice = renderer_->GetContext().GetDevice();
    dst.cellPool = renderer_->Descriptors().CreatePool({{VK_DESCRIPTOR_TYPE_STORAGE_BUFFER, 1}}, 1);
    dst.cellSet = renderer_->Descriptors().AllocateSet(dst.cellPool, cellLayout_);
    if (dst.cellSet == VK_NULL_HANDLE) { set->ReleaseBuffers(); return result; }
    renderer_->Descriptors().UpdateBuffer(dst.cellSet, 0, dst.cellColors.buffer, cellBytes, VK_DESCRIPTOR_TYPE_STORAGE_BUFFER);
    set->gpuBytes = required;
    if (pending_) retired_.push_back(std::move(pending_));
    pending_ = std::move(set); meshUploads_++; memoryRejected_ = false;
    result.ok = true; result.tileCount = pending_->totalTiles; result.lodTriangles[0] = indexCount / 3;
    peakBytesDuringSwap_ = std::max<uint64_t>(peakBytesDuringSwap_, GetActiveBytes() + GetPendingBytes());
    return result;
}

bool SurfaceRenderer::ActivatePendingIfFresh(bool* discardedPending) {
    if (discardedPending) *discardedPending = false;
    if (!pending_) return false;

    // Stale guard (Part 10): a pending set built for a different dataset or a
    // different surface generation must never be promoted. Discard it through
    // the normal retirement path so its buffers are freed safely.
    if (pending_->datasetRevision != currentDatasetRevision_ ||
        pending_->surfaceRevision != currentSurfaceRevision_) {
        staleDiscards_++;
        if (discardedPending) *discardedPending = true;
        fprintf(stderr,
                "[SurfaceRenderer] DISCARDED stale pending surface "
                "(dataset %llu->%llu, surface %llu->%llu)\n",
                (unsigned long long)pending_->datasetRevision,
                (unsigned long long)currentDatasetRevision_,
                (unsigned long long)pending_->surfaceRevision,
                (unsigned long long)currentSurfaceRevision_);
        retired_.push_back(std::move(pending_));
        return false;
    }

    // The outgoing ACTIVE set may still be referenced by an in-flight command
    // buffer, so it is retired, never destroyed here. The swap itself is two
    // pointer moves: there is no frame in which neither set is drawable, which
    // is what makes the transition atomic with respect to presentation.
    if (active_) retired_.push_back(std::move(active_));
    active_ = std::move(pending_);
    // Start at the coarsest level that actually has resident geometry. The
    // indexed-block path materialises only level 0, so unconditionally
    // selecting kSurfaceLodCount-1 left selectedLod pointing at an EMPTY level
    // whenever CullVisibleTiles() returned before recomputing it - the draw
    // loop then found no tiles and the Surface silently produced zero indexed
    // draws while still reporting itself ACTIVE.
    uint32_t resident = 0;
    const uint32_t levelCount = static_cast<uint32_t>(active_->lodMeshes.size());
    for (uint32_t lod = 0; lod < levelCount && lod < kSurfaceLodCount; ++lod) {
        if (active_->lodMeshes[lod].triangleCount != 0 && active_->lods[lod].IsValid()) {
            resident = lod;
        }
    }
    active_->selectedLod = resident;
    swapCount_++;
    return true;
}

void SurfaceRenderer::RetireCompleted() {
    if (retired_.empty() || !renderer_) return;
    // A set is safe to destroy once the frame that last used it has provably
    // completed. Renderer::IsFrameRetired() is backed by the per-slot in-flight
    // FENCES the frame loop already waits on, so this costs no device-wide
    // stall - which is exactly what a vkDeviceWaitIdle() per swap would cost.
    for (std::size_t i = retired_.size(); i-- > 0;) {
        if (renderer_->IsFrameRetired(retired_[i]->lastUsedFrame)) {
            retired_[i]->ReleaseBuffers();
            retired_.erase(retired_.begin() + static_cast<std::ptrdiff_t>(i));
        }
    }
}

void SurfaceRenderer::SetInteractionState(bool moving, double nowMs) {
    if (moving) {
        interactionMoving_ = true;
        lastInteractionMs_ = nowMs;
        // While the camera is in motion the interactive floor is enforced, so a
        // 53M-triangle SLOW surface can never be the navigation geometry.
        if (requestedLod_ < movingLodFloor_) requestedLod_ = movingLodFloor_;
    } else {
        interactionMoving_ = false;
        lastInteractionMs_ = nowMs;
        requestedLod_ = 0;   // settled: the final quality is allowed back
    }
}

uint64_t SurfaceRenderer::EstimateRequiredBytes(std::size_t triangleCount) const {
    // A triangle soup is 3 vertices of (pos 12 + normal 12 + colour 4) = 28 B,
    // i.e. 84 B per triangle, plus 3 uint32 indices = 12 B. The coarse LOD
    // levels are clustered to roughly 1/16 and 1/256 of that, which is the same
    // ratio the real build produces, so a small constant is added for them.
    const double perTri = 84.0 + 12.0;
    const double lodFactor = 1.0 + 1.0 / 16.0 + 1.0 / 256.0;
    return static_cast<uint64_t>(static_cast<double>(triangleCount) * perTri * lodFactor);
}

bool SurfaceRenderer::WouldAcceptUpload(std::size_t triangleCount) const {
    if (vramBudgetBytes_ <= 0.0) return true;   // no budget known: do not block
    const double resident = static_cast<double>(GetActiveBytes()) +
                            static_cast<double>(GetPendingBytes());
    return (resident + static_cast<double>(EstimateRequiredBytes(triangleCount)))
           <= vramBudgetBytes_;
}

SurfaceState SurfaceRenderer::GetState() const {
    if (pending_) return SurfaceState::FinalPending;
    if (!active_) return SurfaceState::Empty;
    return active_->isPreview ? SurfaceState::PreviewActive : SurfaceState::FinalActive;
}

uint32_t SurfaceRenderer::GetTotalTileCount() const {
    // The tile COUNT is per level: each LOD is re-tiled independently (LOD1/LOD2
    // have far fewer, smaller triangles, so the same target tile budget splits
    // into a different number of runs). Reporting level 0's count against the
    // SELECTED level's visible count produced nonsense like "176 visible of
    // 170 total", so the total is read from the level actually being drawn.
    if (!active_ || active_->lodMeshes.empty()) return 0;
    const uint32_t lod = active_->selectedLod;
    return static_cast<uint32_t>(active_->lodMeshes[lod].tiles.size());
}

uint32_t SurfaceRenderer::GetVisibleTileCount() const {
    return active_ ? active_->visibleTilesLastFrame : 0;
}

uint64_t SurfaceRenderer::GetTotalTriangleCount() const {
    // Reported for the level actually being drawn, so "submitted / total" is a
    // meaningful ratio. Culling and LOD only ever reduce the drawn set, and the
    // per-level counts are available separately via GetLodTriangleCount().
    if (!active_ || active_->lodMeshes.empty()) return 0;
    return static_cast<uint64_t>(active_->lodMeshes[active_->selectedLod].triangleCount);
}

uint64_t SurfaceRenderer::GetVisibleTriangleCount() const {
    return active_ ? active_->submittedTrianglesLastFrame : 0;
}

uint32_t SurfaceRenderer::GetSelectedLod() const {
    return active_ ? active_->selectedLod : 0;
}

uint64_t SurfaceRenderer::GetLodTriangleCount(uint32_t lod) const {
    if (!active_ || lod >= kSurfaceLodCount) return 0;
    return static_cast<uint64_t>(active_->lodMeshes[lod].triangleCount);
}

VkDeviceSize SurfaceRenderer::GetActiveBytes() const {
    return active_ ? active_->gpuBytes : 0;
}

VkDeviceSize SurfaceRenderer::GetPendingBytes() const {
    return pending_ ? pending_->gpuBytes : 0;
}

VkDeviceSize SurfaceRenderer::GetLodCacheBytes() const {
    // Coarse levels are the cache: level 0 is the engineering surface, so
    // everything above it is the resident LOD cache.
    if (!active_) return 0;
    VkDeviceSize sum = 0;
    for (uint32_t lod = 1; lod < kSurfaceLodCount; ++lod) {
        sum += active_->lods[lod].Bytes();
    }
    return sum;
}

// Rebuilds the visible-tile list for this frame from the live camera. Called
// once per frame, never per tile: the frustum and the projected scale are both
// derived from the CURRENT MVP here and then reused by every tile test.
void SurfaceRenderer::CullVisibleTiles(double nowMs) {
    for (uint32_t lod = 0; lod < kSurfaceLodCount; ++lod) {
        active_->visibleTiles[lod].clear();
    }
    if (!active_ || active_->lodMeshes.empty()) return;

    // ---- Idle refine (Part 8) --------------------------------------------
    // After the debounce window the requested (final) level is allowed back.
    if (interactionMoving_ && (nowMs - lastInteractionMs_) >= idleRefineMs_) {
        interactionMoving_ = false;
        requestedLod_ = 0;
    }

    float mvp[16];
    renderer_->GetLastMvp(mvp);
    ExtractFrustumPlanes(mvp, lastFrustum_);

    const Camera& cam = renderer_->GetCamera();
    const VkExtent2D ext = renderer_->GetExtent();
    lastScale_ = MakeProjectedScale(
        cam.IsOrthographic(), cam.GetParallelScale(),
        cam.GetFovYDegrees(), static_cast<double>(ext.height));

    // Highest level with resident geometry is the finest available.
    uint32_t finest = 0;
    bool any = false;
    for (uint32_t lod = 0; lod < kSurfaceLodCount; ++lod) {
        if (active_->lodMeshes[lod].triangleCount != 0 && active_->lods[lod].IsValid()) {
            finest = lod;
            any = true;
        }
    }
    if (!any) return;

    // ---- Frustum cull EVERY level independently (Part 7) -----------------
    // Each level is culled against its OWN tile bounds. Doing it per level
    // (rather than reusing level 0's partition) is what keeps the levels'
    // draws self-consistent: a tile is only drawn with triangles that are
    // actually inside the bounds that were tested, so culling can never clip
    // geometry that is on screen.
    for (uint32_t lod = 0; lod <= finest; ++lod) {
        const SurfaceLodMesh& m = active_->lodMeshes[lod];
        if (m.triangleCount == 0 || !active_->lods[lod].IsValid()) continue;
        std::vector<uint32_t>& vis = active_->visibleTiles[lod];
        vis.reserve(m.tiles.size());
        for (std::size_t t = 0; t < m.tiles.size(); ++t) {
            if (AabbVisible(lastFrustum_, m.tiles[t].bmin, m.tiles[t].bmax)) {
                vis.push_back(static_cast<uint32_t>(t));
            }
        }
    }

    // ---- Pick the single level to draw (Part 8) --------------------------
    // ONE level is drawn per frame, not a per-tile mix. Mixing levels whose
    // tile partitions differ is exactly what produces T-junction cracks at
    // the seams, and the brief requires no holes and no duplicate triangles.
    // The level is chosen by PROJECTED ERROR: the coarsest level whose
    // geometric error still fits inside the pixel budget at this zoom.
    const uint32_t lodFloor = interactionMoving_ ? movingLodFloor_ : requestedLod_;
    const double pxPerMeter = lastScale_.isOrtho
        ? lastScale_.orthoPixelsPerMeter
        : lastScale_.perspPixelsPerMeterAt1m / 1000.0;
    const double err = active_->lodMeshes[0].cellSizeMeters * pxPerMeter;
    // While moving, the interactive floor wins outright.
    uint32_t chosen = interactionMoving_ ? lodFloor : 0;
    if (!interactionMoving_ && err <= static_cast<double>(lodErrorPixelLimit_)) {
        for (uint32_t lod = 1; lod <= finest; ++lod) {
            const double e = active_->lodMeshes[lod].cellSizeMeters * pxPerMeter;
            if (e <= static_cast<double>(lodErrorPixelLimit_)) { chosen = lod; break; }
        }
    }
    if (chosen > finest) chosen = finest;
    if (active_->lodMeshes[chosen].triangleCount == 0) chosen = 0;

    const std::vector<uint32_t>& vis = active_->visibleTiles[chosen];
    uint64_t submittedTris = 0;
    for (uint32_t t : vis) {
        const std::vector<SurfaceTile>& tiles = active_->lodMeshes[chosen].tiles;
        if (t < tiles.size()) submittedTris += tiles[t].triangleCount;
    }

    active_->selectedLod = chosen;
    active_->visibleTilesLastFrame = static_cast<uint32_t>(vis.size());
    active_->submittedTrianglesLastFrame = submittedTris;
    lastCullingValid_ = true;
}

void SurfaceRenderer::Record(VkCommandBuffer cmd, uint32_t frameSlot) {
    // Tiled path (Parts 6-9): cull + pick a LOD from the live camera, then
    // issue ONE vkCmdDrawIndexed per visible tile. No upload happens here, so
    // panning and zooming cost only CPU culling plus fewer draws.
    if (active_ && active_->IsValid() && !active_->lodMeshes.empty()) {
        CullVisibleTiles(NowMs());
        const uint32_t lod = active_->selectedLod;
        const SurfaceLodMesh& mesh = active_->lodMeshes[lod];
        const SurfaceLodBuffers& buffers = active_->lods[lod];
        if (!buffers.IsValid() || mesh.tiles.empty()) {
            RetireCompleted();
            return;
        }

        VkExtent2D ext = renderer_->GetExtent();
        VkViewport vp{0.0f, 0.0f, static_cast<float>(ext.width),
                         static_cast<float>(ext.height), 0.0f, 1.0f};
        VkRect2D sc{{0, 0}, {ext.width, ext.height}};
        vkCmdSetViewport(cmd, 0, 1, &vp);
        vkCmdSetScissor(cmd, 0, 1, &sc);

        if (active_->indexedCells) {
            VkDeviceSize offset = 0;
            vkCmdBindVertexBuffers(cmd, 0, 1, &buffers.positions.buffer, &offset);
            vkCmdBindIndexBuffer(cmd, buffers.indices.buffer, 0, VK_INDEX_TYPE_UINT32);
            VkDescriptorSet sets[] = {renderer_->GetFrameDescriptorSet(), buffers.cellSet};
            vkCmdBindPipeline(cmd, VK_PIPELINE_BIND_POINT_GRAPHICS,
                shadingMode_ == ShadingMode::Wireframe ? indexedWireframePipeline_ : indexedPipeline_);
            vkCmdBindDescriptorSets(cmd, VK_PIPELINE_BIND_POINT_GRAPHICS, indexedLayout_, 0, 2, sets, 0, nullptr);
            uint64_t submitted = 0;
            for (uint32_t index : active_->visibleTiles[lod]) {
                const auto& tile = mesh.tiles[index]; uint32_t firstTriangle = tile.firstIndex / 3;
                vkCmdPushConstants(cmd, indexedLayout_, VK_SHADER_STAGE_FRAGMENT_BIT, 0, sizeof(firstTriangle), &firstTriangle);
                vkCmdDrawIndexed(cmd, tile.indexCount, 1, tile.firstIndex, 0, 0);
                indexedDrawCalls_++;
                submitted += tile.triangleCount;
            }
            active_->submittedTrianglesLastFrame = submitted;
            active_->lastUsedFrame = renderer_->GetFrameCounter(); drawCalls_++;
            renderer_->WriteTimestamp(cmd, frameSlot, Renderer::kTsSurfaceAfter);
            RetireCompleted(); return;
        }
        VkDeviceSize offsets[] = {0, 0, 0};
        VkBuffer buffers3[] = {buffers.positions.buffer, buffers.normals.buffer,
                                buffers.colors.buffer};
        vkCmdBindVertexBuffers(cmd, 0, 3, buffers3, offsets);
        vkCmdBindIndexBuffer(cmd, buffers.indices.buffer, 0, VK_INDEX_TYPE_UINT32);
        VkDescriptorSet frameSet = renderer_->GetFrameDescriptorSet();
        vkCmdBindDescriptorSets(cmd, VK_PIPELINE_BIND_POINT_GRAPHICS,
                                        pipelineLayout_, 0, 1, &frameSet, 0, nullptr);

        PushConstants pc{};
        float len = std::sqrt(lightDir_[0] * lightDir_[0] +
                              lightDir_[1] * lightDir_[1] +
                              lightDir_[2] * lightDir_[2]);
        const float inv = (len > 1e-12f) ? 1.0f / len : 1.0f;
        pc.lightDirAmbient[0] = lightDir_[0] * inv;
        pc.lightDirAmbient[1] = lightDir_[1] * inv;
        pc.lightDirAmbient[2] = lightDir_[2] * inv;
        pc.lightDirAmbient[3] = ambient_;
        pc.elevRangeMode[0] = zMin_;
        pc.elevRangeMode[1] = zMax_;
        pc.elevRangeMode[2] = crispHybridActive_
            ? 4.0f
            : static_cast<float>(static_cast<int>(shadingMode_));
        pc.elevRangeMode[3] = parityBaseElevationDeg_;
        pc.shadeParamsA[0] = crispAzimuthDeg_;
        pc.shadeParamsA[1] = crispSharpnessRaw_;
        pc.shadeParamsA[2] = crispAmbient_;
        pc.shadeParamsA[3] = parityAmbientFloor_;
        pc.shadeParamsB[0] = crispKeyIntensity_;
        pc.shadeParamsB[1] = crispFillIntensity_;
        pc.shadeParamsB[2] = static_cast<float>(parityDebugStage_);
        pc.shadeParamsB[3] = static_cast<float>(parityColorMode_);

        renderer_->WriteTimestamp(cmd, frameSlot, Renderer::kTsSurfaceBefore);
        VkPipeline pipeline = (shadingMode_ == ShadingMode::Wireframe && !crispHybridActive_)
                                  ? pipelineWireframe_ : pipelineSolid_;
        vkCmdBindPipeline(cmd, VK_PIPELINE_BIND_POINT_GRAPHICS, pipeline);
        vkCmdPushConstants(cmd, pipelineLayout_,
                              VK_SHADER_STAGE_VERTEX_BIT | VK_SHADER_STAGE_FRAGMENT_BIT,
                              0, sizeof(pc), &pc);

        // ONE DRAW PER VISIBLE TILE. Each tile is a contiguous [firstIndex,
        // +indexCount) run of this level's index buffer, so no rebinding and no
        // re-upload is needed - only the draw list changes with the camera.
        uint64_t submitted = 0;
        for (uint32_t t : active_->visibleTiles[lod]) {
            if (t >= mesh.tiles.size()) continue;
            const SurfaceTile& tile = mesh.tiles[t];
            if (tile.indexCount == 0) continue;
            vkCmdDrawIndexed(cmd, tile.indexCount, 1, tile.firstIndex, 0, 0);
            indexedDrawCalls_++;
            submitted += tile.triangleCount;
        }
        drawCalls_++;
        active_->submittedTrianglesLastFrame = submitted;
        // Mark this set as in use THIS frame so retirement waits for its fence.
        active_->lastUsedFrame = renderer_->GetFrameCounter();
        renderer_->WriteTimestamp(cmd, frameSlot, Renderer::kTsSurfaceAfter);
        RetireCompleted();
        return;
    }

    if (!initialized_ || vertexCount_ == 0) return;
    VkExtent2D ext = renderer_->GetExtent();
    VkViewport vp{0.0f, 0.0f, static_cast<float>(ext.width),
                     static_cast<float>(ext.height), 0.0f, 1.0f};
    VkRect2D sc{{0, 0}, {ext.width, ext.height}};
    vkCmdSetViewport(cmd, 0, 1, &vp);
    vkCmdSetScissor(cmd, 0, 1, &sc);

    // offsets[] previously had only 1 element for 3 bound buffers, so
    // vkCmdBindVertexBuffers() read offsets[1]/offsets[2] out of bounds.
    VkDeviceSize offsets[] = {0, 0, 0};
    VkBuffer buffers[] = {positions_.buffer, normals_.buffer, colors_.buffer};
    vkCmdBindVertexBuffers(cmd, 0, 3, buffers, offsets);
    vkCmdBindIndexBuffer(cmd, indices_.buffer, 0, VK_INDEX_TYPE_UINT32);
    VkDescriptorSet frameSet = renderer_->GetFrameDescriptorSet();
    vkCmdBindDescriptorSets(cmd, VK_PIPELINE_BIND_POINT_GRAPHICS,
                                    pipelineLayout_, 0, 1,
                                    &frameSet, 0, nullptr);

    PushConstants pc{};
    float len = std::sqrt(lightDir_[0] * lightDir_[0] +
                             lightDir_[1] * lightDir_[1] +
                             lightDir_[2] * lightDir_[2]);
    const float inv = (len > 1e-12f) ? 1.0f / len : 1.0f;
    pc.lightDirAmbient[0] = lightDir_[0] * inv;
    pc.lightDirAmbient[1] = lightDir_[1] * inv;
    pc.lightDirAmbient[2] = lightDir_[2] * inv;
    pc.lightDirAmbient[3] = ambient_;
    pc.elevRangeMode[0] = zMin_;
    pc.elevRangeMode[1] = zMax_;
    pc.elevRangeMode[2] = crispHybridActive_
        ? 4.0f  // PASS 1: crisp base, see surface.frag mode==4
        : static_cast<float>(static_cast<int>(shadingMode_));
    pc.elevRangeMode[3] = parityBaseElevationDeg_;   // crisp modes: base light elevation
    pc.shadeParamsA[0] = crispAzimuthDeg_;
    pc.shadeParamsA[1] = crispSharpnessRaw_;
    pc.shadeParamsA[2] = crispAmbient_;
    pc.shadeParamsA[3] = parityAmbientFloor_;         // < 0 -> shader default
    pc.shadeParamsB[0] = crispKeyIntensity_;
    pc.shadeParamsB[1] = crispFillIntensity_;
    pc.shadeParamsB[2] = static_cast<float>(parityDebugStage_);
    pc.shadeParamsB[3] = static_cast<float>(parityColorMode_);

    renderer_->WriteTimestamp(cmd, frameSlot, Renderer::kTsSurfaceBefore);
    VkPipeline pipeline = (shadingMode_ == ShadingMode::Wireframe && !crispHybridActive_)
                              ? pipelineWireframe_ : pipelineSolid_;
    vkCmdBindPipeline(cmd, VK_PIPELINE_BIND_POINT_GRAPHICS, pipeline);
    vkCmdPushConstants(cmd, pipelineLayout_,
                          VK_SHADER_STAGE_VERTEX_BIT | VK_SHADER_STAGE_FRAGMENT_BIT,
                          0, sizeof(pc), &pc);
    vkCmdDrawIndexed(cmd, static_cast<uint32_t>(indexCount_), 1, 0, 0, 0);
    drawCalls_++;

    // PASS 2: mixed-class barycentric overlay (only when the crisp-hybrid
    // Shaded Class path is active and there are mixed faces to draw).
    if (crispHybridActive_ && overlayIndexCount_ > 0 && positions2_.IsValid()) {
        // PASS 2 draws the same triangles as PASS 1, so it must use the
        // LESS_OR_EQUAL pipeline (see Initialize) or its fragments are all
        // depth-rejected and the mixed-class blend silently disappears.
        VkPipeline overlayPipeline = (pipelineSolidOverlay_ != VK_NULL_HANDLE)
                                         ? pipelineSolidOverlay_ : pipeline;
        vkCmdBindPipeline(cmd, VK_PIPELINE_BIND_POINT_GRAPHICS, overlayPipeline);
        VkDeviceSize offsets2[] = {0, 0, 0};
        VkBuffer buffers2[] = {positions2_.buffer, normals2_.buffer, colors2_.buffer};
        vkCmdBindVertexBuffers(cmd, 0, 3, buffers2, offsets2);
        vkCmdBindIndexBuffer(cmd, indices2_.buffer, 0, VK_INDEX_TYPE_UINT32);
        pc.elevRangeMode[2] = 5.0f;  // PASS 2: mixed overlay, see surface.frag mode==5
        vkCmdPushConstants(cmd, pipelineLayout_,
                              VK_SHADER_STAGE_VERTEX_BIT | VK_SHADER_STAGE_FRAGMENT_BIT,
                              0, sizeof(pc), &pc);
        vkCmdDrawIndexed(cmd, static_cast<uint32_t>(overlayIndexCount_), 1, 0, 0, 0);
        overlayDrawCalls_++;
    }
    renderer_->WriteTimestamp(cmd, frameSlot, Renderer::kTsSurfaceAfter);
}

std::size_t SurfaceRenderer::GetIndexCount() const {
    if (active_ && !active_->lodMeshes.empty()) {
        return active_->lodMeshes[active_->selectedLod].indices.size();
    }
    return indexCount_;
}
std::size_t SurfaceRenderer::GetVertexCount() const {
    // A tiled resource set is the modern path and its vertex count lives on
    // the set, not in the legacy vertexCount_ member.
    if (active_) return active_->vertexCount;
    return vertexCount_;
}
std::size_t SurfaceRenderer::GetTriangleCount() const {
    if (active_) return active_->triangleCount;
    return triangleCount_;
}
bool SurfaceRenderer::IsLoaded() const {
    return (active_ && active_->IsValid()) || vertexCount_ > 0;
}
uint64_t SurfaceRenderer::GetMeshUploadCount() const { return meshUploads_; }
uint64_t SurfaceRenderer::GetModeSwitchCount() const { return modeSwitches_; }
VkDeviceSize SurfaceRenderer::GetGpuBytes() const {
    // Includes every resident set (active + pending + retired) so the memory
    // report reflects what the driver is actually holding, which is what the
    // budget check has to stay under during a swap.
    VkDeviceSize total = positions_.size + normals_.size + colors_.size + indices_.size +
                         positions2_.size + normals2_.size + colors2_.size + indices2_.size;
    if (active_) total += active_->gpuBytes;
    if (pending_) total += pending_->gpuBytes;
    for (const auto& r : retired_) if (r) total += r->gpuBytes;
    return total;
}
bool SurfaceRenderer::IsWireframeSupported() const { return wireframeSupported_; }

void SurfaceRenderer::OnSwapchainRebuild() {
    if (!initialized_ || !renderer_) return;
    renderer_->Pipelines().DestroyPipeline(pipelineSolid_);
    renderer_->Pipelines().DestroyPipeline(pipelineSolidOverlay_);
    renderer_->Pipelines().DestroyPipeline(pipelineWireframe_);
    pipelineSolid_ = VK_NULL_HANDLE;
    pipelineSolidOverlay_ = VK_NULL_HANDLE;
    pipelineWireframe_ = VK_NULL_HANDLE;
    std::vector<ShaderModule> solid{vs_, fsSolid_};
    pipelineCfg_.depthStencil.depthCompareOp = VK_COMPARE_OP_LESS;
    pipelineSolid_ = renderer_->Pipelines().CreateGraphicsPipeline(
        pipelineLayout_, solid, pipelineCfg_, renderer_->GetRenderPass(), 0);
    // Same LESS_OR_EQUAL overlay pipeline as Initialize() (see its note): the
    // depth comparison of pipelineCfg_ is part of the created pipeline, so a
    // swapchain rebuild has to recreate both variants from their own state.
    pipelineCfg_.depthStencil.depthCompareOp = VK_COMPARE_OP_LESS_OR_EQUAL;
    pipelineSolidOverlay_ = renderer_->Pipelines().CreateGraphicsPipeline(
        pipelineLayout_, solid, pipelineCfg_, renderer_->GetRenderPass(), 0);
    pipelineCfg_.rasterizer.polygonMode = VK_POLYGON_MODE_LINE;
    std::vector<ShaderModule> wire{vs_, fsWireframe_};
    pipelineWireframe_ = renderer_->Pipelines().CreateGraphicsPipeline(
        pipelineLayout_, wire, pipelineCfg_, renderer_->GetRenderPass(), 0);
}

} // namespace naksha
