#pragma once
#include "naksha/core/math/Point3d.h"
#include "naksha/core/math/Point4d.h"

namespace naksha {
namespace math {

// VERIFIED WORKSTATION TYPE (Piece 4A).
// 4x4 double matrix, row-major: | m00 m01 m02 m03 |
//                                  | m10 m11 m12 m13 |
//                                  | m20 m21 m22 m23 |
//                                  | m30 m31 m32 m33 |
// Storage mapping confirmed by EVIDENCE M4 (InitFromRowValues).
// Point convention (M3): mathematical COLUMN vector, P' = M * P.
class Matrix4d {
    double m_[4][4] = {{0, 0, 0, 0}, {0, 0, 0, 0}, {0, 0, 0, 0}, {0, 0, 0, 0}};

public:
    Matrix4d() = default;

    // EVIDENCE M4: 16 row values map directly to m00..m33 in row-major order.
    Matrix4d(double m00, double m01, double m02, double m03,
             double m10, double m11, double m12, double m13,
             double m20, double m21, double m22, double m23,
             double m30, double m31, double m32, double m33) {
        m_[0][0] = m00; m_[0][1] = m01; m_[0][2] = m02; m_[0][3] = m03;
        m_[1][0] = m10; m_[1][1] = m11; m_[1][2] = m12; m_[1][3] = m13;
        m_[2][0] = m20; m_[2][1] = m21; m_[2][2] = m22; m_[2][3] = m23;
        m_[3][0] = m30; m_[3][1] = m31; m_[3][2] = m32; m_[3][3] = m33;
    }

    double operator()(int r, int c) const { return m_[r][c]; }
    double& operator()(int r, int c) { return m_[r][c]; }

    bool operator==(const Matrix4d& o) const {
        for (int r = 0; r < 4; ++r)
            for (int c = 0; c < 4; ++c)
                if (m_[r][c] != o.m_[r][c]) return false;
        return true;
    }
    bool operator!=(const Matrix4d& o) const { return !(*this == o); }

    // ---- EVIDENCE M3: raw homogeneous Point4d multiplication (P' = M * P) ----
    Point4d Multiply(const Point4d& p) const {
        const double x = p.x, y = p.y, z = p.z, w = p.w;
        return Point4d(
            m_[0][0] * x + m_[0][1] * y + m_[0][2] * z + m_[0][3] * w,
            m_[1][0] * x + m_[1][1] * y + m_[1][2] * z + m_[1][3] * w,
            m_[2][0] * x + m_[2][1] * y + m_[2][2] * z + m_[2][3] * w,
            m_[3][0] * x + m_[3][1] * y + m_[3][2] * z + m_[3][3] * w);
    }

    // Array form. count <= 0 is a no-op. Exact in-place (dst == src) is
    // supported because all four input components are read before any output
    // is written.
    void Multiply(Point4d* destination, const Point4d* source, int count) const {
        if (count <= 0) return;
        for (int i = 0; i < count; ++i)
            destination[i] = Multiply(source[i]);
    }

    // ---- EVIDENCE M2: affine Point3d multiplication ----
    // Implicit w = 1. Only the first three rows are used; the fourth row is
    // completely ignored (no W, no normalization, no validation).
    Point3d MultiplyAffine(const Point3d& p) const {
        const double x = p.x, y = p.y, z = p.z;
        return Point3d(
            m_[0][0] * x + m_[0][1] * y + m_[0][2] * z + m_[0][3],
            m_[1][0] * x + m_[1][1] * y + m_[1][2] * z + m_[1][3],
            m_[2][0] * x + m_[2][1] * y + m_[2][2] * z + m_[2][3]);
    }

    void MultiplyAffine(Point3d* destination, const Point3d* source, int count) const {
        if (count <= 0) return;
        for (int i = 0; i < count; ++i)
            destination[i] = MultiplyAffine(source[i]);
    }

    // ---- EVIDENCE M8: affine VECTOR multiply (linear 3x3 only) ----
    // Direction vector: NO translation (m03/m13/m23 ignored), NO fourth row
    // (m30..m33 ignored), no W, no normalization, no division, no tolerance.
    // count <= 0 is a no-op. Exact in-place (dst == src) supported because
    // x/y/z are read before output is written. The binary unrolls 4-at-a-time
    // for count >= 4; that is treated as compiled optimization only.
    Point3d MultiplyAffineVectors(const Point3d& v) const {
        const double x = v.x, y = v.y, z = v.z;
        return Point3d(
            m_[0][0] * x + m_[0][1] * y + m_[0][2] * z,
            m_[1][0] * x + m_[1][1] * y + m_[1][2] * z,
            m_[2][0] * x + m_[2][1] * y + m_[2][2] * z);
    }

    void MultiplyAffineVectors(Point3d* dst, const Point3d* src, int count) const {
        if (count <= 0) return;
        for (int i = 0; i < count; ++i)
            dst[i] = MultiplyAffineVectors(src[i]);
    }

    // ---- EVIDENCE M1: homogeneous multiply AND renormalize ----
    // For each point, compute X,Y,Z,W as the full 4x4 product with implicit
    // w = 1, then:
    //     if (W != 1.0 && W != 0.0) { q = 1/W; X*=q; Y*=q; Z*=q; }
    // There is NO epsilon, NO near-zero rejection, NO failure status.
    //   W == 0.0 -> XYZ returned as the raw numerators (undivided).
    //   W == 1.0 -> no division.
    //   any other W -> exact division by W.
    // count <= 0 is a no-op. Exact in-place (dst == src) is supported because
    // x/y/z are read before output is written.
    Point3d MultiplyAndRenormalize(const Point3d& p) const {
        Point3d d;
        MultiplyAndRenormalize(&d, &p, 1);
        return d;
    }

    void MultiplyAndRenormalize(Point3d* destination, const Point3d* source, int count) const {
        if (count <= 0) return;
        for (int i = 0; i < count; ++i) {
            const Point3d s = source[i];
            const double x = s.x, y = s.y, z = s.z;
            double X = m_[0][0] * x + m_[0][1] * y + m_[0][2] * z + m_[0][3];
            double Y = m_[1][0] * x + m_[1][1] * y + m_[1][2] * z + m_[1][3];
            double Z = m_[2][0] * x + m_[2][1] * y + m_[2][2] * z + m_[2][3];
            double W = m_[3][0] * x + m_[3][1] * y + m_[3][2] * z + m_[3][3];
            if (W != 1.0 && W != 0.0) {
                const double q = 1.0 / W;
                X *= q; Y *= q; Z *= q;
            }
            destination[i] = Point3d(X, Y, Z);
        }
    }

    // ---- EVIDENCE M5: matrix product C = A * B ----
    // C[i][j] = sum_k A[i][k] * B[k][j].
    // Computed into temporary storage first, so the destination may alias
    // EITHER operand (A.SetProduct(A,B) and B.SetProduct(A,B) both valid).
    void SetProduct(const Matrix4d& a, const Matrix4d& b) {
        double tmp[4][4];
        for (int i = 0; i < 4; ++i)
            for (int j = 0; j < 4; ++j)
                tmp[i][j] = a.m_[i][0] * b.m_[0][j]
                          + a.m_[i][1] * b.m_[1][j]
                          + a.m_[i][2] * b.m_[2][j]
                          + a.m_[i][3] * b.m_[3][j];
        for (int i = 0; i < 4; ++i)
            for (int j = 0; j < 4; ++j)
                m_[i][j] = tmp[i][j];
    }

    static Matrix4d Product(const Matrix4d& a, const Matrix4d& b) {
        Matrix4d r;
        r.SetProduct(a, b);
        return r;
    }
};

} // namespace math
} // namespace naksha
