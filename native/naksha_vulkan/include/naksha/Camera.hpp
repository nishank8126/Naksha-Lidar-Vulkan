#pragma once
#include "naksha/RenderOrigin.hpp"
#include "naksha/core/renderer/Camera.h"

namespace naksha {

// ---------------------------------------------------------------------------
// Engine camera.
//
// World-space state (eye/target) is kept in float64 world coordinates - the
// authoritative frame for LiDAR/UTM data. Matrix computation is delegated to
// the reused naksha::renderer::Camera (double precision, Z-up, Vulkan [0,1]
// depth) but is always performed in RENDER space (world - render origin), so
// the float32 GPU matrix never contains large translations (floating-origin
// rule, spec section 13).
//
// Output is a column-major float32 mat4, which is exactly what GLSL mat4
// expects (Matrix4d is row-major with a column-vector convention; the
// transpose-on-export below is the documented layout bridge).
// ---------------------------------------------------------------------------
class Camera {
public:
    Camera();

    // Projection setup (radians handled internally; degrees at the API).
    void SetPerspective(double fovYDegrees, double aspectRatio,
                        double nearClip, double farClip);
    void SetAspectRatio(double aspectRatio);

    // TRUE orthographic projection - the real VTK parallel camera, not a
    // perspective FOV approximation. `centre` is the world point the view is
    // centred on (VTK's focal point), `viewDir` the unit direction the camera
    // looks along, and `parallelScale` VTK's ParallelScale: HALF the visible
    // world height. Zoom changes only that number; pan only moves `centre`.
    void SetOrthographicWorld(const double centre[3], const double viewDir[3],
                              double parallelScale, double aspectRatio,
                              double nearClip, double farClip);
    bool IsOrthographic() const { return orthographic_; }
    double GetParallelScale() const { return parallelScale_; }
    // Switch back to the perspective path (3D orbit mode).
    void SetPerspectiveMode();

    // Authoritative world-space view state (metres, float64).
    void SetLookAtWorld(const double eye[3], const double target[3]);
    void SetPositionWorld(const double position[3]);
    void SetTargetWorld(const double target[3]);
    void GetPositionWorld(double out[3]) const;
    void GetTargetWorld(double out[3]) const;

    // Navigation. All math in float64 world space; Z-up world.
    // Yaw rotates around world +Z, pitch is clamped to +-89 degrees.
    void Orbit(double deltaYawDegrees, double deltaPitchDegrees);
    void Dolly(double factor);                          // factor<1 zooms in
    void Pan(double horizontal, double vertical);       // screen-parallel move

    // Matrix export (column-major float32 for GLSL / push-constant-free UBO).
    void GetViewProjectionFloat(const RenderOrigin& origin, float out[16]) const;
    void GetViewFloat(const RenderOrigin& origin, float out[16]) const;
    // Camera position in RENDER space (float32) - what shaders need for
    // distance/attenuation style effects.
    void GetPositionRender(const RenderOrigin& origin, float out[3]) const;

    double GetFovYDegrees() const { return fovYDegrees_; }
    double GetAspectRatio() const { return aspectRatio_; }
    double GetNearClip() const { return nearClip_; }
    double GetFarClip() const { return farClip_; }

private:
    double eye_[3] = {0.0, 0.0, 100.0};
    double target_[3] = {0.0, 0.0, 0.0};
    double up_[3] = {0.0, 1.0, 0.0};
    double fovYDegrees_ = 45.0;
    double aspectRatio_ = 16.0 / 9.0;
    double nearClip_ = 0.1;
    double farClip_ = 10000.0;
    // Orthographic (VTK parallel) state. When orthographic_ is true the
    // projection is a real ortho box built from these, and eye_/target_ only
    // carry the view DIRECTION (the eye distance is irrelevant to an ortho
    // projection - it is exactly why zooming no longer moves the camera).
    bool orthographic_ = false;
    double orthoHalfHeight_ = 100.0;    // == VTK ParallelScale
    double centre_[3] = {0.0, 0.0, 0.0};
    double viewDir_[3] = {0.0, 0.0, -1.0};
    double parallelScale_ = 100.0;      // public mirror of orthoHalfHeight_

    mutable renderer::Camera core_;  // render-space math engine (reused core)

    // Pushes current world state (shifted by origin) into core_.
    void ApplyToCore(const RenderOrigin& origin) const;
};

} // namespace naksha
