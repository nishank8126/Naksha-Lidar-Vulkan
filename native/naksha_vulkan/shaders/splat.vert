#version 450
// ---------------------------------------------------------------------------
// Instant Shaded SPLAT pass - vertex stage (stored-oct16 normals).
//
// This is a SEPARATE pipeline from point.vert on purpose. The production
// Instant Shaded normal source is the STORED oct16x2 normal stream, so the
// splat must forward one normal per splat to a second colour attachment for the
// lighting pass to read. That means a 2-attachment render pass, and a pipeline
// is only compatible with the render pass it was built against - so adding an
// output to point.frag would have forced every other display mode into a
// 2-attachment pass. point.vert/point.frag are therefore left completely
// untouched (no regression risk for RGB / classification / intensity /
// elevation) and this pair is used for mode 4 only.
//
// Crispness: the class colour and the oct16 normal are per-splat constants,
// interpolated across a 1-3 px sprite and written once. There is no class-id
// interpolation, no blending and no accumulation anywhere in this path.
// ---------------------------------------------------------------------------

layout(location = 0) in vec3 inPos;         // render space (world - origin)
layout(location = 1) in vec4 inColor;       // per-point RGB (unused in this mode)
layout(location = 2) in float inClassU;     // classification id, UNORM8
layout(location = 3) in float inIntensity;  // raw intensity
layout(location = 4) in vec2 inOct;         // oct16x2 stored normal, SNORM pair

layout(set = 0, binding = 0) uniform FrameUbo {
    mat4 mvp;
    vec4 cameraPos;
    vec4 origin;
    vec4 classPalette[256];    // class id -> RGBA (alpha = visibility)
    vec4 elevationLut[256];
    vec4 intensityLut[256];
} ubo;

layout(push_constant) uniform PointParams {
    vec4 size;       // x=world footprint (m), y=minPx, z=maxPx, w=class->intensity mix
    vec4 shading;    // x=mode, y=zLo, z=zHi, w=zGamma
    vec4 intensity;  // x=iLo, y=iHi, z=contrast, w=gamma
    vec4 view;       // x=px per metre, y=edge softness, z=brightness, w=deviceMax
    vec4 parity;     // x = colour-space byte-parity flag
} pc;

layout(location = 0) out vec4 vColor;
layout(location = 1) out vec4 vOct;      // .rg carry the oct pair
layout(location = 2) out float v_point_px;

const int MODE_SHADED_CLASS_INSTANT = 4;

vec3 srgbToLinear(vec3 c) {
    c = clamp(c, 0.0, 1.0);
    vec3 lo = c / 12.92;
    vec3 hi = pow(max(c + 0.055, 0.0) / 1.055, vec3(2.4));
    return mix(lo, hi, step(vec3(0.04045), c));
}

vec3 toDisplayByte(vec3 c) {
    float mode = pc.parity.x;
    vec3 raw = clamp(c, 0.0, 1.0);
    return mix(raw, srgbToLinear(raw), mode);
}

void main() {
    gl_Position = ubo.mvp * vec4(inPos, 1.0);

    // Splat sizing: the sprite covers the world footprint this point stands
    // for, projected to pixels at this point's depth, clamped to the 1..3 px
    // band. gl_Position.w is the view depth for a perspective projection and 1
    // for an orthographic one, and pc.view.x is px-per-metre-at-1m for the
    // former / px-per-world-unit for the latter, so one expression covers both.
    float footprintPx = pc.size.x * pc.view.x / max(gl_Position.w, 1e-4);
    float pixels = clamp(footprintPx, pc.size.y, pc.size.z);
    float deviceMax = max(pc.view.w, 1.0);
    gl_PointSize = clamp(pixels, 1.0, min(deviceMax, 64.0));
    v_point_px = gl_PointSize;

    int mode = int(pc.shading.x + 0.5);
    int classId = int(inClassU * 255.0 + 0.5) & 255;
    vec3 rgb = ubo.classPalette[classId].rgb;
    // Intensity modulation is off (pc.size.w == 0) on this mode: Shaded Class
    // colours with the palette and lights it, matching the legacy TIN.
    vColor = vec4(toDisplayByte(rgb * max(pc.view.z, 0.0)),
                  (ubo.classPalette[classId].a < 0.5) ? 0.0 : 1.0);

    // Forward the STORED oct pair unchanged. Clamping to [-1,1] is a no-op for a
    // value that already came from an SNORM vertex attribute; it is here so a
    // malformed stream degrades to a defined result instead of an attachment
    // out-of-range write.
    vOct = vec4(clamp(inOct, vec2(-1.0), vec2(1.0)), 0.0, 1.0);
}
