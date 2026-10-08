#pragma once
#include <cmath>

namespace naksha {
namespace math {

// OUR DESIGN documentation (allowed: naming/code organization). The clean-room
// math library uses row-major storage. This is NOT derived from any proprietary
// layout; it is our explicit choice, recorded for traceability.
const char* MatrixStorageConvention();

// Plain finite-check helper (std::isfinite). This is NOT a tolerance/comparison
// policy. No NearlyZero / NearlyEqual production policy exists in Piece 4A:
// those were removed because they are not part of the verified M1-M6 slice.
inline bool IsFinite(double a) { return std::isfinite(a); }

} // namespace math
} // namespace naksha
