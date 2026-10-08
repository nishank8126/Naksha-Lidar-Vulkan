#include "naksha/SurfaceTiling.hpp"

#include <algorithm>
#include <cmath>
#include <unordered_map>

namespace naksha {

float SurfaceTile::DiagonalMeters() const {
    const float dx = bmax[0] - bmin[0];
    const float dy = bmax[1] - bmin[1];
    const float dz = bmax[2] - bmin[2];
    return (dx > 0 ? dx : 0) + (dy > 0 ? dy : 0) + (dz > 0 ? dz : 0);
}

namespace {

inline void ExpandBox(const float* p, float* bmin, float* bmax) {
    for (int k = 0; k < 3; ++k) {
        if (p[k] < bmin[k]) bmin[k] = p[k];
        if (p[k] > bmax[k]) bmax[k] = p[k];
    }
}

} // namespace

// ---------------------------------------------------------------------------
// Tiling
// ---------------------------------------------------------------------------
void BuildSurfaceTiles(const float* positions, std::size_t vertexCount,
                       const uint32_t* indices, std::size_t indexCount,
                       uint32_t targetTileCount,
                       std::vector<uint32_t>& outIndices,
                       std::vector<SurfaceTile>& outTiles) {
    outIndices.clear();
    outTiles.clear();
    const std::size_t triCount = indexCount / 3;
    if (!positions || !indices || triCount == 0) return;

    // ---- XY bounds of the mesh (the tile grid is a top-down partition) ---
    double lo[2] = { 1e300, 1e300 };
    double hi[2] = { -1e300, -1e300 };
    for (std::size_t i = 0; i < vertexCount; ++i) {
        for (int k = 0; k < 2; ++k) {
            const double v = positions[i * 3 + k];
            if (!(v == v)) continue;                 // NaN guard
            if (v < lo[k]) lo[k] = v;
            if (v > hi[k]) hi[k] = v;
        }
    }
    if (!(lo[0] <= hi[0]) || !(lo[1] <= hi[1])) return;
    const double spanX = hi[0] - lo[0];
    const double spanY = hi[1] - lo[1];
    // Degenerate (a single column/row): one tile is the correct answer.
    if (!(spanX > 0.0) || !(spanY > 0.0)) targetTileCount = 1;

    // ---- Choose a grid that lands near the requested tile count ----------
    // Square-ish cells over the data aspect keep the partition isotropic; a
    // wildly anisotropic grid produces diagonal strips and defeats the
    // coalescing pass below.
    uint32_t gx = 1, gy = 1;
    if (targetTileCount > 1 && spanX > 0.0 && spanY > 0.0) {
        const double aspect = spanX / spanY;
        const double approx = std::sqrt(static_cast<double>(targetTileCount));
        gx = std::max(1u, static_cast<uint32_t>(std::lround(approx * std::sqrt(aspect))));
        gy = std::max(1u, static_cast<uint32_t>(std::lround(approx / std::sqrt(aspect))));
        // Bin at 2x the requested density, then coalesce down: too few cells
        // can force one enormous tile, too many just wastes the pass.
        gx = std::min(512u, gx * 2);
        gy = std::min(512u, gy * 2);
    }
    const std::size_t cellCount = static_cast<std::size_t>(gx) * gy;

    // ---- Bin every triangle into a cell by its centroid ------------------
    std::vector<uint32_t> cellOfTri(triCount, 0);
    const double cellW = (spanX > 0.0) ? (spanX / gx) : 1.0;
    const double cellH = (spanY > 0.0) ? (spanY / gy) : 1.0;
    for (std::size_t t = 0; t < triCount; ++t) {
        const uint32_t* tri = &indices[t * 3];
        double cx = 0.0, cy = 0.0;
        for (int k = 0; k < 3; ++k) {
            const uint32_t vi = tri[k];
            if (vi >= vertexCount) continue;   // caller-validated; be safe
            cx += positions[vi * 3 + 0];
            cy += positions[vi * 3 + 1];
        }
        cx /= 3.0;
        cy /= 3.0;
        uint32_t ix = static_cast<uint32_t>((cx - lo[0]) / cellW);
        uint32_t iy = static_cast<uint32_t>((cy - lo[1]) / cellH);
        if (ix >= gx) ix = gx - 1;
        if (iy >= gy) iy = gy - 1;
        cellOfTri[t] = iy * gx + ix;
    }

    // ---- Counting sort by cell: O(T), stable, no comparison sort ---------
    std::vector<uint32_t> cellStart(cellCount + 1, 0);
    for (std::size_t t = 0; t < triCount; ++t) cellStart[cellOfTri[t] + 1]++;
    for (std::size_t c = 0; c < cellCount; ++c) cellStart[c + 1] += cellStart[c];
    std::vector<uint32_t> cursor(cellStart.begin(), cellStart.end() - 1);
    std::vector<uint32_t> order(triCount);
    for (std::size_t t = 0; t < triCount; ++t) order[cursor[cellOfTri[t]]++] = static_cast<uint32_t>(t);

    // ---- Coalesce adjacent cells along X into balanced runs --------------
    // Hitting the requested count keeps tiles small enough that culling
    // actually removes work at high zoom.
    const uint32_t budget = std::max<uint32_t>(
        1u, static_cast<uint32_t>((triCount + targetTileCount - 1) / std::max(1u, targetTileCount)));

    outIndices.resize(indexCount);
    std::size_t writeTri = 0;
    for (uint32_t iy = 0; iy < gy; ++iy) {
        uint32_t ix = 0;
        while (ix < gx) {
            const uint32_t cell = iy * gx + ix;
            if (cellStart[cell + 1] == cellStart[cell]) { ++ix; continue; }

            // Extend right while the merged tile stays within budget. `next`
            // MUST be re-derived from `end` on every iteration: using a fixed
            // cell+1 re-counts the same cell forever, which inflates `count`
            // past the number of triangles actually written and desynchronises
            // firstIndex from the emitted range.
            uint32_t end = ix;
            uint32_t count = cellStart[cell + 1] - cellStart[cell];
            while (end + 1 < gx) {
                const uint32_t next = iy * gx + (end + 1);
                if (cellStart[next + 1] == cellStart[next]) break;  // empty: stop
                const uint32_t merged = count + (cellStart[next + 1] - cellStart[next]);
                if (count > 0 && merged > budget * 2u) break;
                count = merged;
                ++end;
            }

            SurfaceTile tile;
            tile.bmin[0] = tile.bmin[1] = tile.bmin[2] =  1e30f;
            tile.bmax[0] = tile.bmax[1] = tile.bmax[2] = -1e30f;
            tile.firstIndex = static_cast<uint32_t>(writeTri * 3);
            for (uint32_t cx = ix; cx <= end; ++cx) {
                const uint32_t c = iy * gx + cx;
                for (uint32_t o = cellStart[c]; o < cellStart[c + 1]; ++o) {
                    const uint32_t t = order[o];
                    for (int k = 0; k < 3; ++k) {
                        const uint32_t src = indices[t * 3 + k];
                        outIndices[writeTri * 3 + k] = src;
                        if (src < vertexCount) {
                            ExpandBox(&positions[src * 3], tile.bmin, tile.bmax);
                        }
                    }
                    ++writeTri;
                }
            }
            tile.indexCount = count * 3u;
            tile.triangleCount = count;
            if (tile.bmin[0] > tile.bmax[0]) {
                // Cannot happen (every counted cell has triangles), but a
                // degenerate box would silently cull the tile away.
                for (int k = 0; k < 3; ++k) { tile.bmin[k] = 0.0f; tile.bmax[k] = 0.0f; }
            }
            outTiles.push_back(tile);
            ix = end + 1;
        }
    }
}

// ---------------------------------------------------------------------------
// LOD generation: vertex clustering
// ---------------------------------------------------------------------------
bool ClusterSurfaceLod(const float* positions, std::size_t vertexCount,
                       const uint32_t* indices, std::size_t indexCount,
                       const uint8_t* colors,
                       float cellSizeMeters,
                       SurfaceLodMesh& out) {
    out.positions.clear();
    out.normals.clear();
    out.colors.clear();
    out.indices.clear();
    out.tiles.clear();
    out.vertexCount = 0;
    out.triangleCount = 0;
    if (!positions || !indices || vertexCount == 0 || indexCount < 3) return false;
    if (!(cellSizeMeters > 0.0f)) return false;

    // ---- Lattice origin: model minimum, so cell indices stay small -------
    float gmin[3] = { 1e30f, 1e30f, 1e30f };
    for (std::size_t i = 0; i < vertexCount; ++i) {
        for (int k = 0; k < 3; ++k) {
            const float v = positions[i * 3 + k];
            if (!(v == v)) continue;
            if (v < gmin[k]) gmin[k] = v;
        }
    }
    if (!(gmin[0] < 1e29f)) return false;
    const float inv = 1.0f / cellSizeMeters;

    // ---- Pass 1: map every vertex to a lattice cell ----------------------
    // The key packs 3 cell coordinates into 63 bits. Coordinates are clamped
    // so a pathological extent/cell ratio cannot overflow the packing.
    auto cellKey = [&](uint32_t v, uint64_t& key) -> bool {
        const float* p = &positions[v * 3];
        uint64_t c[3];
        for (int k = 0; k < 3; ++k) {
            if (!(p[k] == p[k])) return false;        // NaN vertex
            const double raw = (static_cast<double>(p[k]) - gmin[k]) * inv;
            if (raw < 0.0) return false;
            int64_t ci = static_cast<int64_t>(raw);
            if (ci > (1 << 20)) ci = (1 << 20);
            c[k] = static_cast<uint64_t>(ci);
        }
        key = (c[0] << 42) | (c[1] << 21) | c[2];
        return true;
    };

    // Open-addressing map key -> new vertex index, sized to a power of two
    // >= 2x the vertex count so the load factor stays under 0.5.
    std::size_t cap = 1;
    while (cap < vertexCount * 2) cap <<= 1;
    const std::size_t mask = cap - 1;
    const uint64_t kEmpty = ~uint64_t(0);
    std::vector<uint64_t> keys(cap, kEmpty);
    std::vector<uint32_t> vals(cap, 0);
    std::vector<uint32_t> srcToCluster(vertexCount, 0xFFFFFFFFu);
    std::vector<double> accum;   // 3 doubles per cluster
    std::vector<uint32_t> accumCount;
    accum.reserve(vertexCount / 4 + 1);
    accumCount.reserve(vertexCount / 4 + 1);

    for (std::size_t v = 0; v < vertexCount; ++v) {
        uint64_t key;
        if (!cellKey(static_cast<uint32_t>(v), key)) continue;
        std::size_t h = static_cast<std::size_t>(key) & mask;
        while (keys[h] != kEmpty && keys[h] != key) h = (h + 1) & mask;
        if (keys[h] == kEmpty) {
            keys[h] = key;
            vals[h] = static_cast<uint32_t>(accum.size() / 3);
            const float* p = &positions[v * 3];
            accum.push_back(p[0]); accum.push_back(p[1]); accum.push_back(p[2]);
            accumCount.push_back(1);
            srcToCluster[v] = vals[h];
        } else {
            const uint32_t ci = vals[h];
            const float* p = &positions[v * 3];
            accum[ci * 3 + 0] += p[0];
            accum[ci * 3 + 1] += p[1];
            accum[ci * 3 + 2] += p[2];
            accumCount[ci]++;
            srcToCluster[v] = ci;
        }
    }

    const std::size_t clusterCount = accum.size() / 3;
    if (clusterCount == 0) return false;

    // ---- Pass 2: materialise one vertex per occupied cell ----------------
    out.positions.resize(clusterCount * 3);
    for (std::size_t c = 0; c < clusterCount; ++c) {
        const double n = (accumCount[c] > 0) ? accumCount[c] : 1;
        out.positions[c * 3 + 0] = static_cast<float>(accum[c * 3 + 0] / n);
        out.positions[c * 3 + 1] = static_cast<float>(accum[c * 3 + 1] / n);
        out.positions[c * 3 + 2] = static_cast<float>(accum[c * 3 + 2] / n);
    }
    out.normals.assign(clusterCount * 3, 0.0f);   // passthrough shading path

    if (colors) {
        // Average member colours so the coarse mesh keeps the fine mesh's
        // tone instead of sampling whichever vertex won the race.
        out.colors.assign(clusterCount * 4, 255);
        std::vector<uint32_t> colorAccum(clusterCount * 3, 0);
        for (std::size_t v = 0; v < vertexCount; ++v) {
            const uint32_t c = srcToCluster[v];
            if (c == 0xFFFFFFFFu) continue;
            colorAccum[c * 3 + 0] += colors[v * 4 + 0];
            colorAccum[c * 3 + 1] += colors[v * 4 + 1];
            colorAccum[c * 3 + 2] += colors[v * 4 + 2];
        }
        for (std::size_t c = 0; c < clusterCount; ++c) {
            const uint32_t n = (accumCount[c] > 0) ? accumCount[c] : 1;
            for (int k = 0; k < 3; ++k) {
                out.colors[c * 4 + k] =
                    static_cast<uint8_t>(colorAccum[c * 3 + k] / n);
            }
            out.colors[c * 4 + 3] = 255;
        }
    } else {
        out.colors.assign(clusterCount * 4, 255);
    }

    // ---- Pass 3: re-emit only triangles that survive with 3 distinct cells
    // A triangle with two corners merged into one cell collapses to a sliver;
    // dropping it is what makes the coarse level watertight rather than
    // degenerately spiky.
    out.indices.reserve(indexCount);
    for (std::size_t t = 0; t + 2 < indexCount; t += 3) {
        const uint32_t a = indices[t + 0], b = indices[t + 1], c = indices[t + 2];
        if (a >= vertexCount || b >= vertexCount || c >= vertexCount) continue;
        const uint32_t ca = srcToCluster[a], cb = srcToCluster[b], cc = srcToCluster[c];
        if (ca == 0xFFFFFFFFu || cb == 0xFFFFFFFFu || cc == 0xFFFFFFFFu) continue;
        if (ca == cb || cb == cc || ca == cc) continue;   // degenerate
        out.indices.push_back(ca);
        out.indices.push_back(cb);
        out.indices.push_back(cc);
    }

    out.vertexCount = clusterCount;
    out.triangleCount = out.indices.size() / 3;
    out.cellSizeMeters = cellSizeMeters;
    return out.triangleCount != 0;
}

std::size_t CountStridedTriangles(std::size_t triangleCount, uint32_t stride) {
    if (stride <= 1) return triangleCount;
    return (triangleCount + stride - 1) / stride;
}

// ---------------------------------------------------------------------------
// Frustum culling
// ---------------------------------------------------------------------------
void ExtractFrustumPlanes(const float* mvp16, FrustumPlanes& out) {
    out.valid = false;
    if (!mvp16) return;
    // Gribb-Hartmann on a COLUMN-MAJOR matrix: element (row r, column c) is
    // mvp16[c * 4 + r]. The six planes are row3 +/- row0/1/2, which is valid
    // for any projection (perspective or orthographic) because clip space is
    // defined by the matrix itself - no assumption about which projection the
    // caller installed.
    const float* m = mvp16;
    auto setPlus = [&](int idx, int row) {
        for (int c = 0; c < 4; ++c) out.p[idx][c] = m[c * 4 + 3] + m[c * 4 + row];
    };
    auto setMinus = [&](int idx, int row) {
        for (int c = 0; c < 4; ++c) out.p[idx][c] = m[c * 4 + 3] - m[c * 4 + row];
    };
    setPlus(0, 0);    // left
    setMinus(1, 0);   // right
    setPlus(2, 1);    // bottom
    setMinus(3, 1);   // top
    // Vulkan clips Z to [0, w]. Near is z_clip >= 0 and far is z_clip <= w,
    // i.e. row2.p >= 0 and (row2 - row3).p <= 0, which is row3 - row2 on the
    // inside-positive side. (OpenGL's symmetric [-w, w] would need a -w far
    // plane; using it here would leave the whole [0, w] depth range untested.)
    setPlus(4, 2);    // near
    setMinus(5, 2);   // far

    // Normalize so the plane test is a true signed distance, and so a
    // degenerate (zero-length) normal - which an all-zero MVP produces - is
    // caught here instead of silently culling the entire surface away.
    for (int i = 0; i < 6; ++i) {
        const float len = std::sqrt(out.p[i][0] * out.p[i][0] +
                                    out.p[i][1] * out.p[i][1] +
                                    out.p[i][2] * out.p[i][2]);
        if (!(len > 1e-12f)) return;   // invalid projection: report "no frustum"
        const float inv = 1.0f / len;
        for (int k = 0; k < 4; ++k) out.p[i][k] *= inv;
    }
    out.valid = true;
}

bool AabbVisible(const FrustumPlanes& fr, const float* bmin, const float* bmax) {
    if (!fr.valid) return true;   // no usable frustum: draw everything
    for (int i = 0; i < 6; ++i) {
        const float* p = fr.p[i];
        // Positive vertex: pick the box corner furthest along the plane normal.
        const float px = (p[0] >= 0.0f) ? bmax[0] : bmin[0];
        const float py = (p[1] >= 0.0f) ? bmax[1] : bmin[1];
        const float pz = (p[2] >= 0.0f) ? bmax[2] : bmin[2];
        const float dist = p[0] * px + p[1] * py + p[2] * pz + p[3];
        if (dist < 0.0f) return false;
    }
    return true;
}

// ---------------------------------------------------------------------------
// Projected-error LOD selection
// ---------------------------------------------------------------------------
ProjectedScale MakeProjectedScale(bool isOrtho, double parallelScale,
                                  double fovYDegrees, double viewportHeight) {
    ProjectedScale s;
    s.isOrtho = isOrtho;
    if (viewportHeight <= 0.0) viewportHeight = 1.0;
    if (isOrtho) {
        // parallelScale is the HALF visible world height (VTK convention), so
        // the full window spans 2*parallelScale metres.
        const double span = (parallelScale > 0.0) ? (2.0 * parallelScale) : 1.0;
        s.orthoPixelsPerMeter = viewportHeight / span;
    } else {
        const double fov = (fovYDegrees > 1.0 && fovYDegrees < 179.0)
                           ? fovYDegrees : 45.0;
        s.perspPixelsPerMeterAt1m =
            viewportHeight / (2.0 * std::tan(fov * 3.14159265358979323846 / 360.0));
    }
    return s;
}

uint32_t SelectLodForTile(const std::vector<const SurfaceLodMesh*>& levels,
                          const SurfaceTile& tile,
                          double depth, const ProjectedScale& scale,
                          uint32_t minLod, uint32_t maxLod,
                          float errorPixelLimit) {
    if (levels.empty()) return 0;
    uint32_t hi = maxLod;
    if (hi >= levels.size()) hi = static_cast<uint32_t>(levels.size() - 1);
    if (hi <= minLod) return minLod;

    // Pixels per metre at this tile's depth. A tile may only drop to a coarse
    // level when that level's own geometric error still fits inside the pixel
    // budget, so the decision follows PROJECTED SCREEN SIZE rather than raw
    // distance - which is what makes it behave correctly under the VTK
    // parallel camera, where there is no perspective foreshortening at all.
    double pxPerMeter;
    if (scale.isOrtho) {
        pxPerMeter = scale.orthoPixelsPerMeter;
    } else {
        const double d = (depth > 0.05) ? depth : 0.05;
        pxPerMeter = scale.perspPixelsPerMeterAt1m / d;
    }
    if (!(pxPerMeter > 0.0)) pxPerMeter = 1.0;

    // Walk from the finest resident level down and take the first level whose
    // projected error fits the budget. A level with no resident geometry is
    // skipped, so a failed coarse build degrades to the next level rather
    // than drawing nothing.
    for (uint32_t lod = hi; lod > minLod; --lod) {
        const SurfaceLodMesh* m = levels[lod];
        if (!m || m->triangleCount == 0) continue;
        const double errPx = m->cellSizeMeters * pxPerMeter;
        if (errPx <= static_cast<double>(errorPixelLimit)) return lod;
    }
    (void)tile;
    return minLod;
}

} // namespace naksha

