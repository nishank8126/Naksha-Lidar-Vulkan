#pragma once

namespace naksha {
namespace math {

// VERIFIED WORKSTATION TYPE (Piece 4A / M2, M3).
// 3D position quantity. Exactly three double values: x, y, z.
// No vector arithmetic, no distance, no subtraction here: those operations are
// not part of the verified M1-M6 slice and remain RE_PENDING.
class Point3d {
public:
    double x = 0.0;
    double y = 0.0;
    double z = 0.0;

    Point3d() = default;
    constexpr Point3d(double x_, double y_, double z_) : x(x_), y(y_), z(z_) {}
};

inline bool operator==(const Point3d& a, const Point3d& b) {
    return a.x == b.x && a.y == b.y && a.z == b.z;
}
inline bool operator!=(const Point3d& a, const Point3d& b) { return !(a == b); }

} // namespace math
} // namespace naksha
