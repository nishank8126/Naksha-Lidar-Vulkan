#version 450
// ---------------------------------------------------------------------------
// Instant Shaded SPLAT pass - fragment stage (stored-oct16 normals).
//
// Two outputs, both deterministic and un-blended:
//   location 0  class colour (RGBA8)   - one class per splat, LUT lookup only
//   location 1  the splat's oct16x2 normal, written verbatim
//
// Discarding outside the disc (VTK parity) keeps the sprite round. Discarding
// a hidden class (alpha 0 from the visibility LUT) means it writes NEITHER a
// colour NOR a normal NOR a depth entry, so the lighting pass shows the
// viewport background through it.
// ---------------------------------------------------------------------------

layout(location = 0) in vec4 vColor;
layout(location = 1) in vec4 vOct;
layout(location = 2) in float v_point_px;

layout(location = 0) out vec4 outColor;
layout(location = 1) out vec4 outNormal;

layout(push_constant) uniform PointParams {
    vec4 size;
    vec4 shading;
    vec4 intensity;
    vec4 view;
    vec4 parity;
} pc;

void main() {
    // Hidden class: no colour, no normal, no depth.
    if (vColor.a < 0.5) discard;

    // Same disc test as VTK / point.frag.
    vec2 offset = gl_PointCoord - vec2(0.5);
    if (dot(offset, offset) > 0.25) discard;

    outColor = vec4(vColor.rgb, 1.0);
    outNormal = vOct;
}
