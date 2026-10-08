#pragma once
#include "naksha/core/math/Matrix4d.h"
#include "naksha/core/math/Point3d.h"
#include "naksha/core/spatial/BoundingBox.h"

#include <cmath>
#include <algorithm>

namespace naksha {
namespace renderer {

enum class CameraProjection { Perspective, Orthographic };

struct FrustumPlanes {
    double left[4] = {};
    double right[4] = {};
    double top[4] = {};
    double bottom[4] = {};
    double nearPlane[4] = {};
    double farPlane[4] = {};

    void ExtractFromVP(const math::Matrix4d& vp);
    bool TestAABB(const spatial::BoundingBox& box) const;
};

class Camera {
public:
    Camera();

    void SetPerspective(double fovYDegrees, double aspectRatio,
                        double nearClip, double farClip);
    void SetAspectRatio(double aspectRatio) { aspectRatio_ = aspectRatio; dirty_ = true; }
    void SetOrthographic(double left, double right, double bottom, double top,
                         double nearClip, double farClip);
    void SetLookAt(const math::Point3d& eye, const math::Point3d& center,
                   const math::Point3d& up);

    void MoveForward(double distance);
    void MoveRight(double distance);
    void MoveUp(double distance);
    void Rotate(double yawDegrees, double pitchDegrees);
        void Zoom(double factor);
    void Pan(double dx, double dy);

    const math::Matrix4d& GetViewMatrix() const;
    const math::Matrix4d& GetProjectionMatrix() const;
    const math::Matrix4d& GetViewProjectionMatrix() const;
    const FrustumPlanes& GetFrustumPlanes() const;

    math::Point3d GetPosition() const { return position_; }
    math::Point3d GetTarget() const { return target_; }
    math::Point3d GetForward() const;
    math::Point3d GetRight() const;
    math::Point3d GetUp() const;

    double GetFOV() const { return fovY_; }
    double GetFovY() const { return fovY_; }
    double GetAspectRatio() const { return aspectRatio_; }
    double GetNearClip() const { return nearClip_; }
    double GetFarClip() const { return farClip_; }
    CameraProjection GetProjectionType() const { return projectionType_; }
    math::Point3d GetWorldUp() const { return worldUp_; }
    double GetYaw() const { return yaw_; }
    double GetPitch() const { return pitch_; }
    double GetOrthoLeft() const { return orthoLeft_; }
    double GetOrthoRight() const { return orthoRight_; }
    double GetOrthoBottom() const { return orthoBottom_; }
    double GetOrthoTop() const { return orthoTop_; }

    void Invalidate() { dirty_ = true; }

    void FocusOnBounds(const spatial::BoundingBox& bounds, double padding = 1.5);

    // Top/Plan view: camera directly above, looking straight down (-Z),
    // orthographic projection fitted to XY footprint.
    void SetTopView(const spatial::BoundingBox& bounds, double padding = 1.1);

    // Strict 2D/3D view mode locking.
    void Set2DMode(const spatial::BoundingBox& bounds);
    void Set3DMode(const spatial::BoundingBox& bounds);
    bool Is2DMode() const { return is2D_; }
    bool Is3DMode() const { return !is2D_; }
    void Set2DMode(bool val) { is2D_ = val; }
    // NOTE (Naksha extraction): Save3DState()/Restore3DState() declarations
    // removed - they had no definitions in the source project (link error if
    // called). Any 2D/3D state save must be implemented properly if needed.

private:
    bool is2D_ = true;
    math::Point3d last3DPosition_ = {0, 0, 5};
    math::Point3d last3DTarget_ = {0, 0, 0};
    double last3DYaw_ = -90.0;
    double last3DPitch_ = 0.0;
    double last3DFovY_ = 45.0;

private:
    math::Point3d position_ = {0, 0, 5};
    math::Point3d target_ = {0, 0, 0};
    math::Point3d worldUp_ = {0, 0, 1}; // Z-up: matches LiDAR/CAD convention

    double yaw_ = -90.0;
    double pitch_ = 0.0;

    double fovY_ = 45.0;
    double aspectRatio_ = 16.0 / 9.0;
    double nearClip_ = 0.1;
    double farClip_ = 10000.0;
    double orthoLeft_ = -10, orthoRight_ = 10;
    double orthoBottom_ = -10, orthoTop_ = 10;

    CameraProjection projectionType_ = CameraProjection::Perspective;

    mutable math::Matrix4d viewMatrix_;
    mutable math::Matrix4d projectionMatrix_;
    mutable math::Matrix4d viewProjectionMatrix_;
    mutable FrustumPlanes frustumPlanes_;
    mutable bool dirty_ = true;

    void UpdateMatrices() const;
    math::Matrix4d ComputeViewMatrix() const;
    math::Matrix4d ComputeProjectionMatrix() const;
};

} // namespace renderer
} // namespace naksha
