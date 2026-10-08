#include "naksha/core/renderer/Camera.h"
#include <cmath>

namespace naksha {
namespace renderer {

#ifndef M_PI
#define M_PI 3.14159265358979323846
#endif

Camera::Camera() {
    UpdateMatrices();
}

void Camera::SetPerspective(double fovYDegrees, double aspectRatio,
                             double nearClip, double farClip) {
    fovY_ = fovYDegrees; aspectRatio_ = aspectRatio;
    nearClip_ = nearClip; farClip_ = farClip;
    projectionType_ = CameraProjection::Perspective;
    dirty_ = true;
}
void Camera::SetOrthographic(double left, double right, double bottom, double top,
                             double nearClip, double farClip) {
    // Declared in the header but never defined in this slice until now. The
    // Naksha main view is a VTK PARALLEL camera, and emulating it with a
    // perspective FOV (the previous approach) is exactly what made the Vulkan
    // viewport behave like a 3D viewer: zooming shrank the world window, pan
    // and zoom disagreed with VTK, and point clouds stretched into rays.
    orthoLeft_ = left; orthoRight_ = right;
    orthoBottom_ = bottom; orthoTop_ = top;
    nearClip_ = nearClip; farClip_ = farClip;
    projectionType_ = CameraProjection::Orthographic;
    dirty_ = true;
}



void Camera::SetLookAt(const math::Point3d& eye, const math::Point3d& center,
                        const math::Point3d& up) {
    position_ = eye; target_ = center; worldUp_ = up;
    dirty_ = true;
}

const math::Matrix4d& Camera::GetViewMatrix() const {
    UpdateMatrices(); return viewMatrix_;
}
const math::Matrix4d& Camera::GetProjectionMatrix() const {
    UpdateMatrices(); return projectionMatrix_;
}
const math::Matrix4d& Camera::GetViewProjectionMatrix() const {
    UpdateMatrices(); return viewProjectionMatrix_;
}
const FrustumPlanes& Camera::GetFrustumPlanes() const {
    UpdateMatrices(); return frustumPlanes_;
}

void Camera::UpdateMatrices() const {
    if (!dirty_) return;
    dirty_ = false;

    const double f = 1.0 / std::tan(fovY_ * M_PI / 360.0);
    const double invAspect = 1.0 / aspectRatio_;
    const double n = nearClip_ > 1e-9 ? nearClip_ : 1e-9;
    const double fa = farClip_ > n ? farClip_ : n + 1.0;

    // Projection: RIGHT-HANDED view space (camera looks down -Z), Vulkan
    // depth range [0, 1].
    //
    // CONVENTION FIX: Matrix4d is row-major storage with the COLUMN-vector
    // convention P' = M * P (see Matrix4d.h, evidence M3/M4), and GLSL mat4
    // uses the same convention. The previous code wrote this projection (and
    // the view matrix below) in the row-vector layout, i.e. it was the
    // transpose of what the library and the shaders consume. The result was
    // that w_clip came out constant/negative for every vertex in front of the
    // camera, so every point and triangle was clipped away and the viewport
    // presented the clear colour only - the "black Vulkan pane". Net effect
    // here: w_clip = -z_v = distance in front (>0), z_ndc maps near->0 and
    // far->1.
    //
    // The y row is NEGATED on purpose: Vulkan's NDC +y points DOWN the
    // framebuffer, whereas VTK/OpenGL treat +y as up. Without the negation
    // the viewport renders vertically mirrored relative to the VTK view this
    // renderer has to match.
    projectionMatrix_ = math::Matrix4d(
        f * invAspect, 0,  0, 0,
        0,            -f,  0, 0,
        0,             0, -fa / (fa - n), -(fa * n) / (fa - n),
        0,             0, -1, 0);

    if (projectionType_ == CameraProjection::Orthographic) {
        // ORTHOGRAPHIC - the real VTK parallel projection.
        //
        // Same right-handed view space (camera looks down -Z) and the same
        // negated y row, because Vulkan NDC +y points DOWN the framebuffer.
        // The view box is what VTK's ParallelScale means: half the visible
        // height, with the width following from the aspect ratio, centred on
        // the focal point (VTK's parallel view is symmetric).
        //   x_ndc =  x_view / halfWidth
        //   y_ndc = -y_view / halfHeight          (negated for Vulkan)
        //   z_ndc = (z_view + near) / (near - far) with z_view in [-far,-near]
        // The last row is (0,0,0,1) so w_clip = 1: no perspective divide, so
        // parallel lines stay parallel and a point cloud cannot stretch into
        // rays at any zoom level.
        const double r = orthoRight_ - orthoLeft_;
        const double t = orthoTop_ - orthoBottom_;
        const double halfW = (std::fabs(r) > 1e-12) ? r * 0.5 : 1.0;
        const double halfH = (std::fabs(t) > 1e-12) ? t * 0.5 : 1.0;
        const double cx = (orthoRight_ + orthoLeft_) * 0.5;
        const double cy = (orthoTop_ + orthoBottom_) * 0.5;
        projectionMatrix_ = math::Matrix4d(
            1.0 / halfW, 0.0,         0.0,           -cx / halfW,
            0.0,      -1.0 / halfH,  0.0,            cy / halfH,
            0.0,       0.0,        -1.0 / (fa - n), -n / (fa - n),
            0.0,       0.0,         0.0,            1.0);
    }

    double fx = target_.x - position_.x;
    double fy = target_.y - position_.y;
    double fz = target_.z - position_.z;
    double flen = std::sqrt(fx*fx + fy*fy + fz*fz);
    if (flen > 1e-12) { fx/=flen; fy/=flen; fz/=flen; }

    double rx = fy * worldUp_.z - fz * worldUp_.y;
    double ry = fz * worldUp_.x - fx * worldUp_.z;
    double rz = fx * worldUp_.y - fy * worldUp_.x;
    double rlen = std::sqrt(rx*rx + ry*ry + rz*rz);
    if (rlen > 1e-12) { rx/=rlen; ry/=rlen; rz/=rlen; }

    double ux = ry * fz - rz * fy;
    double uy = rz * fx - rx * fz;
    double uz = rx * fy - ry * fx;

    // View matrix, also in the column-vector convention: the rotation basis
    // forms the first three ROWS (so x_v = r.rel, y_v = u.rel, z_v = -f.rel)
    // and the translation sits in the fourth COLUMN.
    viewMatrix_ = math::Matrix4d(
        rx,  ry,  rz,  -(rx*position_.x + ry*position_.y + rz*position_.z),
        ux,  uy,  uz,  -(ux*position_.x + uy*position_.y + uz*position_.z),
        -fx, -fy, -fz,  (fx*position_.x + fy*position_.y + fz*position_.z),
        0,   0,   0,   1);

    viewProjectionMatrix_ = math::Matrix4d::Product(projectionMatrix_, viewMatrix_);
}

void Camera::MoveForward(double) {}
void Camera::MoveRight(double) {}
void Camera::MoveUp(double) {}
void Camera::Rotate(double, double) {}
void Camera::Zoom(double) {}
void Camera::Pan(double, double) {}
math::Point3d Camera::GetForward() const {
    return math::Point3d(target_.x - position_.x, target_.y - position_.y, target_.z - position_.z);
}
math::Point3d Camera::GetRight() const {
    math::Point3d f = GetForward(); double l = std::sqrt(f.x*f.x+f.y*f.y+f.z*f.z);
    return l>1e-12 ? math::Point3d(f.x/l, f.y/l, f.z/l) : math::Point3d(1,0,0);
}
math::Point3d Camera::GetUp() const {
    math::Point3d r = GetRight(), f = GetForward();
    return math::Point3d(r.y*f.z-r.z*f.y, r.z*f.x-r.x*f.z, r.x*f.y-r.y*f.x);
}
void Camera::FocusOnBounds(const spatial::BoundingBox&, double) {}
void Camera::SetTopView(const spatial::BoundingBox&, double) {}
void Camera::Set2DMode(const spatial::BoundingBox&) {}
void Camera::Set3DMode(const spatial::BoundingBox&) {}

} // namespace renderer
} // namespace naksha
