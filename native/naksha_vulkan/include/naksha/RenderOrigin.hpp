#pragma once
#include <cstddef>

namespace naksha {

// ---------------------------------------------------------------------------
// Floating origin (Phase 1, spec section 13).
//
// Authoritative world coordinates are float64 (app.data["xyz"] in metres,
// never modified by the engine). GPU vertex buffers store float32 in RENDER
// space:  renderPos = worldPos - renderOrigin. Render-space coordinates are
// near the scene so float32 keeps centimetre precision even for UTM-scale
// data. The render origin is uploaded to shaders (frame UBO) so any shader
// can reconstruct absolute world position when needed (e.g. fog, styling).
// ---------------------------------------------------------------------------
struct RenderOrigin {
    double x = 0.0;
    double y = 0.0;
    double z = 0.0;

    void Set(double nx, double ny, double nz) { x = nx; y = ny; z = nz; }
    void Reset() { Set(0.0, 0.0, 0.0); }

    void WorldToRender(double wx, double wy, double wz, double out[3]) const {
        out[0] = wx - x;
        out[1] = wy - y;
        out[2] = wz - z;
    }

    void RenderToWorld(double rx, double ry, double rz, double out[3]) const {
        out[0] = rx + x;
        out[1] = ry + y;
        out[2] = rz + z;
    }

    // Bulk conversion float64 world (N,3) C-contiguous -> float32 render
    // (N,3) C-contiguous, single linear pass. There is deliberately no
    // per-point API anywhere in this engine.
    // Returns the number of floats written (pointCount * 3).
    static std::size_t WorldToRenderF64ToF32(const double* world,
                                             float* renderOut,
                                             std::size_t pointCount,
                                             const RenderOrigin& origin) {
        const double ox = origin.x;
        const double oy = origin.y;
        const double oz = origin.z;
        const std::size_t n = pointCount * 3;
        for (std::size_t i = 0; i < n; i += 3) {
            renderOut[i + 0] = static_cast<float>(world[i + 0] - ox);
            renderOut[i + 1] = static_cast<float>(world[i + 1] - oy);
            renderOut[i + 2] = static_cast<float>(world[i + 2] - oz);
        }
        return n;
    }
};

} // namespace naksha
