#pragma once
#include <cstddef>
#include <cstdint>
#include <vector>

namespace naksha {

// Number of resident surface LOD levels. 0 = final/engineering quality,
// 1 = medium, 2 = interaction/coarse. Three is the smallest set that lets a
// pan/zoom drop two levels while still having a mid-fidelity level for the
// settled-but-not-final window.
static constexpr uint32_t kSurfaceLodCount = 3;

// ===========================================================================
// Surface tiling, LOD generation and frustum culling.
//
// Pure CPU geometry code - no Vulkan types - so the spatial logic can be
// reasoned about independently of the renderer. SurfaceRenderer owns the GPU
// side; this file owns "which triangles, grouped how, at which fidelity".
//
// Everything here operates in RENDER space (world minus the render origin),
// i.e. exactly the space the vertex buffer holds and the MVP consumes. An AABB
// built here is therefore directly testable against the frame's frustum with
// no further transform.
// ===========================================================================

// One tile of the surface: a contiguous run of the index buffer plus the
// render-space bounds that let the draw path skip it.
struct SurfaceTile {
    float bmin[3] = {0, 0, 0};
    float bmax[3] = {0, 0, 0};
    uint32_t firstIndex = 0;   // offset into the level's index buffer
    uint32_t indexCount = 0;   // always a multiple of 3
    uint32_t triangleCount = 0;
    uint32_t lodLevel = 0;     // 0 final, 1 medium, 2 interaction
    // Residency state: 0 = not uploaded, 1 = GPU-resident, 2 = upload failed.
    uint8_t residency = 0;

    float DiagonalMeters() const;
};

// A complete tileable surface at one level of detail: its own vertex/index
// soup AND its own tiling. LOD levels are independent meshes, not subsets of
// one buffer, so a coarse level costs no memory in the final level.
struct SurfaceLodMesh {
    std::vector<float> positions;   // (V,3) render space
    std::vector<float> normals;     // (V,3), zeros for the passthrough path
    std::vector<uint8_t> colors;    // (V,4) RGBA8
    std::vector<uint32_t> indices;  // permuted so each tile is one run
    std::vector<SurfaceTile> tiles;
    std::size_t vertexCount = 0;
    std::size_t triangleCount = 0;
    // Representative vertex spacing of this level in metres: the geometric
    // error bound the projected-error LOD test compares against one pixel.
    float cellSizeMeters = 0.0f;
};

// Six frustum planes (Gribb-Hartmann), each (a,b,c,d) with the inside
// half-space on the positive side.
struct FrustumPlanes {
    float p[6][4] = {};
    bool valid = false;
};

// ---------------------------------------------------------------------------
// Tiling
// ---------------------------------------------------------------------------

// Partitions `indices` into ~targetTileCount spatially coherent tiles by
// binning triangle centroids on a uniform XY grid, then greedily coalescing
// adjacent cells along X into balanced runs.
//
// The output index buffer is a PERMUTATION of the input: every input triangle
// appears exactly once, so there are no duplicate triangles and no gaps. Each
// tile is one contiguous [firstIndex, +indexCount) run, which is exactly what
// vkCmdDrawIndexed(firstIndex, indexCount, ...) needs - one draw per visible
// tile with no rebinding.
void BuildSurfaceTiles(const float* positions, std::size_t vertexCount,
                       const uint32_t* indices, std::size_t indexCount,
                       uint32_t targetTileCount,
                       std::vector<uint32_t>& outIndices,
                       std::vector<SurfaceTile>& outTiles);

// ---------------------------------------------------------------------------
// LOD generation (vertex clustering / grid decimation)
// ---------------------------------------------------------------------------

// Produces a coarser, WATERTIGHT version of the mesh by snapping every vertex
// to a `cellSizeMeters` lattice, merging each occupied cell into one vertex
// placed at the centroid of its members, and re-emitting only triangles whose
// three corners survive in three DIFFERENT cells.
//
// Why this and not "skip every Nth triangle": a TIN has no independent
// triangles, so striding punches visible holes. Grid clustering keeps exactly
// one shared vertex per cell, so adjacent cells agree on the surface and the
// coarse mesh has no cracks at tile boundaries.
//
// Returns false only if the input is empty or the cell size is degenerate.
bool ClusterSurfaceLod(const float* positions, std::size_t vertexCount,
                       const uint32_t* indices, std::size_t indexCount,
                       const uint8_t* colors /* (V,4) or null */,
                       float cellSizeMeters,
                       SurfaceLodMesh& out);

// A stride decimation kept for reporting only: the cheapest possible "fewer
// triangles" that does NOT build a new mesh. It leaves holes, so it is never
// used for the shipped LOD path - only to show what the alternative costs.
std::size_t CountStridedTriangles(std::size_t triangleCount, uint32_t stride);

// ---------------------------------------------------------------------------
// Frustum culling
// ---------------------------------------------------------------------------

// Extracts the 6 clip planes from a column-major float32 view-projection
// matrix (the exact bytes GLSL receives). Handles orthographic and
// perspective alike; for an ortho box the near/far planes are still emitted,
// which is harmless because the AABB test is conservative.
void ExtractFrustumPlanes(const float* mvp16, FrustumPlanes& out);

// Conservative AABB/plane test. Returns false only when the box is provably
// outside; a box straddling any plane is reported visible.
bool AabbVisible(const FrustumPlanes& fr, const float* bmin, const float* bmax);

// ---------------------------------------------------------------------------
// Projected-error LOD selection
// ---------------------------------------------------------------------------

// Pixels per metre at a given depth for the current projection. `isOrtho`
// selects the VTK-parallel path (parallelScale = HALF the visible world
// height); otherwise perspPixelsPerMeterAt1m comes from the FOV and the value
// is divided by the depth.
struct ProjectedScale {
    bool isOrtho = true;
    double orthoPixelsPerMeter = 0.0;      // constant, depth-independent
    double perspPixelsPerMeterAt1m = 0.0;  // height / (2 * tan(fovY/2))
};

// Builds the projected scale for the current camera + viewport.
ProjectedScale MakeProjectedScale(bool isOrtho, double parallelScale,
                                  double fovYDegrees, double viewportHeight);

// Chooses the coarsest LOD whose geometric error is still under one pixel for
// this tile at this depth, clamped to [minLod, maxLod].
//
// This is a genuine screen-space error test, not a distance constant: the same
// tile at the same distance picks a different level when the user zooms, and a
// top-down ortho view - with no perspective foreshortening at all - still
// behaves correctly because the ortho branch is exact.
uint32_t SelectLodForTile(const std::vector<const SurfaceLodMesh*>& levels,
                          const SurfaceTile& tile,
                          double depth, const ProjectedScale& scale,
                          uint32_t minLod, uint32_t maxLod,
                          float errorPixelLimit);

} // namespace naksha

