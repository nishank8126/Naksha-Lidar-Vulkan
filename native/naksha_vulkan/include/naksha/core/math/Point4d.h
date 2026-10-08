#pragma once

namespace naksha {
namespace math {

// VERIFIED WORKSTATION TYPE (Piece 4A / M3).
// Homogeneous 4D point. Exactly four double values: x, y, z, w.
// Convention (M3): mathematical COLUMN vector, so point transform is P' = M * P.
class Point4d {
public:
    double x = 0.0;
    double y = 0.0;
    double z = 0.0;
    double w = 0.0;

    Point4d() = default;
    constexpr Point4d(double x_, double y_, double z_, double w_)
        : x(x_), y(y_), z(z_), w(w_) {}
};

inline bool operator==(const Point4d& a, const Point4d& b) {
    return a.x == b.x && a.y == b.y && a.z == b.z && a.w == b.w;
}
inline bool operator!=(const Point4d& a, const Point4d& b) { return !(a == b); }

} // namespace math
} // namespace naksha
