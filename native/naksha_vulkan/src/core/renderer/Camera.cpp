#include "naksha/Camera.hpp"
#include <cmath>
#include <algorithm>

#ifndef M_PI
#define M_PI 3.14159265358979323846
#endif

namespace naksha {

Camera::Camera() {
    core_.SetPerspective(fovYDegrees_, aspectRatio_, nearClip_, farClip_);
    core_.SetLookAt(
        math::Point3d(eye_[0], eye_[1], eye_[2]),
        math::Point3d(target_[0], target_[1], target_[2]),
        math::Point3d(up_[0], up_[1], up_[2]));
}

void Camera::SetPerspective(double fovYDegrees, double aspectRatio,
                            double nearClip, double farClip) {
    fovYDegrees_ = fovYDegrees;
    aspectRatio_ = aspectRatio;
    nearClip_ = nearClip;
    farClip_ = farClip;
    core_.SetPerspective(fovYDegrees_, aspectRatio_, nearClip_, farClip_);
}

void Camera::SetAspectRatio(double aspect) {
    aspectRatio_ = aspect;
    core_.SetAspectRatio(aspect);
}

void Camera::SetPerspectiveMode() {
    orthographic_ = false;
    core_.SetPerspective(fovYDegrees_, aspectRatio_, nearClip_, farClip_);
}

void Camera::SetOrthographicWorld(const double centre[3], const double viewDir[3],
                                  double parallelScale, double aspect,
                                  double nearClip, double farClip) {
    orthographic_ = true;
    orthoHalfHeight_ = std::max(parallelScale, 1e-6);
    parallelScale_ = orthoHalfHeight_;
    aspectRatio_ = (aspect > 1e-9) ? aspect : aspectRatio_;
    nearClip_ = nearClip;
    farClip_ = farClip;

    double dx = viewDir[0], dy = viewDir[1], dz = viewDir[2];
    double len = std::sqrt(dx * dx + dy * dy + dz * dz);
    if (len < 1e-12) { dx = 0.0; dy = 0.0; dz = -1.0; len = 1.0; }
    viewDir_[0] = dx / len; viewDir_[1] = dy / len; viewDir_[2] = dz / len;
    centre_[0] = centre[0]; centre_[1] = centre[1]; centre_[2] = centre[2];

    // An orthographic projection is independent of eye distance; the eye only
    // defines the view direction. It DOES define the depth range though: every
    // vertex ends up at eye-space z = -(eye -> vertex distance) and the ortho
    // matrix maps that onto [0,1] using nearClip_/farClip_, which the app
    // pushes from the source VTK camera (its own eye sits at the MIDDLE of
    // that range: e.g. distance 5000 with clipping range 4873.76..5123.36).
    //
    // BLACK-FRAME ROOT CAUSE (fixed here): parking the eye at farClip*0.5 put
    // it at 2561 units from the centre while near was 4873, so every vertex
    // was in FRONT of the near plane. nkv_render() ran, vkQueuePresentKHR
    // succeeded, the clear colour appeared - and every draw call was clipped,
    // i.e. a permanently black viewport that no resize/sync fix could ever
    // repair (measured: nkv_capture_frame std=0.00 with point draws counting
    // up, and the same frame black on the real screen).
    //
    // Put the eye in the middle of the clip range instead: the centre then
    // lands at depth 0.5 and the whole box VTK sized for fits, exactly like
    // the source camera it mirrors.
    double standoff = 0.5 * (nearClip_ + farClip_);
    if (!(standoff > nearClip_ && standoff < farClip_)) {
        // Degenerate range (near >= far) - fall back to a mid-span eye.
        standoff = nearClip_ + std::max(farClip_ - nearClip_, 1.0) * 0.5;
    }
    standoff = std::max(standoff, 1.0);
    eye_[0] = centre_[0] - viewDir_[0] * standoff;
    eye_[1] = centre_[1] - viewDir_[1] * standoff;
    eye_[2] = centre_[2] - viewDir_[2] * standoff;
    target_[0] = centre_[0]; target_[1] = centre_[1]; target_[2] = centre_[2];
}

void Camera::SetLookAtWorld(const double eye[3], const double target[3]) {
    eye_[0] = eye[0]; eye_[1] = eye[1]; eye_[2] = eye[2];
    target_[0] = target[0]; target_[1] = target[1]; target_[2] = target[2];
}

void Camera::SetPositionWorld(const double p[3]) {
    eye_[0] = p[0]; eye_[1] = p[1]; eye_[2] = p[2];
}

void Camera::SetTargetWorld(const double t[3]) {
    target_[0] = t[0]; target_[1] = t[1]; target_[2] = t[2];
}

void Camera::GetPositionWorld(double out[3]) const {
    out[0] = eye_[0]; out[1] = eye_[1]; out[2] = eye_[2];
}

void Camera::GetTargetWorld(double out[3]) const {
    out[0] = target_[0]; out[1] = target_[1]; out[2] = target_[2];
}

void Camera::Orbit(double deltaYawDegrees, double deltaPitchDegrees) {
    const double dx = eye_[0] - target_[0];
    const double dy = eye_[1] - target_[1];
    const double dz = eye_[2] - target_[2];
    const double r = std::sqrt(dx * dx + dy * dy + dz * dz);
    if (r < 1e-12) return;
    double azimuth = std::atan2(dy, dx);
    double elevation = std::asin(std::clamp(dz / r, -1.0, 1.0));
    const double deg2rad = M_PI / 180.0;
    azimuth += deltaYawDegrees * deg2rad;
    elevation = std::clamp(elevation + deltaPitchDegrees * deg2rad,
                           -M_PI / 2.0 + 0.001, M_PI / 2.0 - 0.001);
    const double h = r * std::cos(elevation);
    eye_[0] = target_[0] + h * std::cos(azimuth);
    eye_[1] = target_[1] + h * std::sin(azimuth);
    eye_[2] = target_[2] + r * std::sin(elevation);
}

void Camera::Dolly(double factor) {
    const double dx = eye_[0] - target_[0];
    const double dy = eye_[1] - target_[1];
    const double dz = eye_[2] - target_[2];
    const double r = std::sqrt(dx * dx + dy * dy + dz * dz);
    if (r < 1e-12) return;
    const double newR = std::max(r * factor, 1e-6);
    const double scale = newR / r;
    eye_[0] = target_[0] + dx * scale;
    eye_[1] = target_[1] + dy * scale;
    eye_[2] = target_[2] + dz * scale;
}

void Camera::Pan(double horizontal, double vertical) {
    const double fx = target_[0] - eye_[0];
    const double fy = target_[1] - eye_[1];
    const double fz = target_[2] - eye_[2];
    const double len = std::sqrt(fx * fx + fy * fy + fz * fz);
    if (len < 1e-12) return;
    const double inv = 1.0 / len;
    const double fwx = fx * inv, fwy = fy * inv, fwz = fz * inv;
    const double rx = fwy * up_[2] - fwz * up_[1];
    const double ry = fwz * up_[0] - fwx * up_[2];
    const double rz = fwx * up_[1] - fwy * up_[0];
    const double rlen = std::sqrt(rx * rx + ry * ry + rz * rz);
    if (rlen < 1e-12) return;
    const double invR = 1.0 / rlen;
    const double rightX = rx * invR, rightY = ry * invR, rightZ = rz * invR;
    const double upX = rightY * fwz - rightZ * fwy;
    const double upY = rightZ * fwx - rightX * fwz;
    const double upZ = rightX * fwy - rightY * fwx;
    eye_[0] += rightX * horizontal + upX * vertical;
    eye_[1] += rightY * horizontal + upY * vertical;
    eye_[2] += rightZ * horizontal + upZ * vertical;
    target_[0] += rightX * horizontal + upX * vertical;
    target_[1] += rightY * horizontal + upY * vertical;
    target_[2] += rightZ * horizontal + upZ * vertical;
}

void Camera::ApplyToCore(const RenderOrigin& origin) const {
    const double ex = eye_[0] - origin.x;
    const double ey = eye_[1] - origin.y;
    const double ez = eye_[2] - origin.z;
    const double tx = target_[0] - origin.x;
    const double ty = target_[1] - origin.y;
    const double tz = target_[2] - origin.z;
    core_.SetLookAt(math::Point3d(ex, ey, ez),
                    math::Point3d(tx, ty, tz),
                    math::Point3d(up_[0], up_[1], up_[2]));
    if (orthographic_) {
        // Real ortho box. Half-height is VTK's ParallelScale; the width follows
        // the aspect ratio so the visible world rectangle matches VTK's.
        const double halfH = orthoHalfHeight_;
        const double halfW = halfH * (aspectRatio_ > 1e-9 ? aspectRatio_ : 1.0);
        core_.SetOrthographic(-halfW, halfW, -halfH, halfH, nearClip_, farClip_);
    } else {
        core_.SetPerspective(fovYDegrees_, aspectRatio_, nearClip_, farClip_);
    }
}

void Camera::GetViewProjectionFloat(const RenderOrigin& origin,
                                      float out[16]) const {
    ApplyToCore(origin);
    const math::Matrix4d& vp = core_.GetViewProjectionMatrix();
    for (int r = 0; r < 4; ++r)
        for (int c = 0; c < 4; ++c)
            out[c * 4 + r] = static_cast<float>(vp(r, c));
}

void Camera::GetViewFloat(const RenderOrigin& origin, float out[16]) const {
    ApplyToCore(origin);
    const math::Matrix4d& v = core_.GetViewMatrix();
    for (int r = 0; r < 4; ++r)
        for (int c = 0; c < 4; ++c)
            out[c * 4 + r] = static_cast<float>(v(r, c));
}

void Camera::GetPositionRender(const RenderOrigin& origin, float out[3]) const {
    out[0] = static_cast<float>(eye_[0] - origin.x);
    out[1] = static_cast<float>(eye_[1] - origin.y);
    out[2] = static_cast<float>(eye_[2] - origin.z);
}

} // namespace naksha
