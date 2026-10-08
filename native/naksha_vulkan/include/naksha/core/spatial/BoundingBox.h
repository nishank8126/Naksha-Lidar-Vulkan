#pragma once
namespace naksha { namespace spatial {

// Axis-aligned bounding volume used by spatial nodes and the point-reader
// bounds. Minimal, allocation-free. A frustum visibility test uses this as the
// node volume (the exact Bentley frustum planes are pending reverse engineering).
struct BoundingBox {
    double minX = 0.0, minY = 0.0, minZ = 0.0;
    double maxX = 0.0, maxY = 0.0, maxZ = 0.0;

    void GetCorner(int i, double& x, double& y, double& z) const {
        x = (i & 1) ? maxX : minX;
        y = (i & 2) ? maxY : minY;
        z = (i & 4) ? maxZ : minZ;
    }
    bool Intersects(const BoundingBox& o) const {
        return !(maxX < o.minX || minX > o.maxX ||
                 maxY < o.minY || minY > o.maxY ||
                 maxZ < o.minZ || minZ > o.maxZ);
    }
};

} // namespace spatial
} // namespace naksha
