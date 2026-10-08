#version 450
// ---------------------------------------------------------------------------
// LiDAR point fragment shader: circular sprite matching VTK's shape exactly.
//
// SOURCE OF TRUTH: gui/unified_actor_manager.py _attach_view_shader_context(),
// the //VTK::Color::Impl replacement:
//     vec2 uv25 = gl_PointCoord.xy - vec2(0.5);
//     float radial_sq = dot(uv25, uv25);
//     if (radial_sq > 0.25) discard;
//     ...
//     opacity = 1.0;
//
// So VTK draws a HARD-EDGED, FULLY OPAQUE disc of radius 0.5 in gl_PointCoord
// space: outside that radius the fragment is discarded, inside it the colour is
// written at full strength. There is no alpha ramp, no soft rim, and no
// accumulation of overlapping sprites.
//
// This shader previously faded the outer `softness` fraction of every sprite
// (default 0.4) to zero and alpha-blended it, which is what produced the
// "large soft balls / splats" appearance: a 2.5 px sprite with a 40% soft rim
// is mostly translucent, so overlapping points smear into round blobs and the
// cloud reads as fuzzy instead of as crisp individual returns. That is NOT what
// Naksha's VTK view does, so it is removed here.
//
// crispness (pc.view.y) is kept as a parameter but now defaults to 0 (hard
// edge, VTK behaviour). A small non-zero value enables coverage-based
// anti-aliasing, which is the only thing that differs from VTK and only
// softens a single pixel of edge by a single alpha step - it does not turn the
// sprite into a ball.
// ---------------------------------------------------------------------------

layout(location = 0) in vec4 vColor;
layout(location = 1) in float v_point_px;   // gl_PointSize, for edge coverage
layout(location = 0) out vec4 outColor;

layout(push_constant) uniform PointParams {
    vec4 size;
    vec4 shading;
    vec4 intensity;
    vec4 view;
    vec4 parity;     // x = colour-space byte-parity flag (unused here; layout must
                     // match point.vert, which applies it to vColor)
} pc;

void main() {
    // Class visibility (mode 4, Instant Shaded Class): a hidden class writes
    // alpha = 0 from point.vert, so the splat leaves NO pixel and no depth
    // entry - the fullscreen lighting pass then shows the viewport background
    // through it, which is what "class unchecked" must look like. Modes 0-3
    // always send alpha = 1 here, so this branch is unreachable for them and
    // their output is byte-for-byte what it was.
    if (vColor.a < 0.5) discard;

    // Same disc test as VTK: discard outside radius 0.5 of gl_PointCoord.
    vec2 offset = gl_PointCoord - vec2(0.5);
    float radial_sq = dot(offset, offset);
    if (radial_sq > 0.25) discard;                 // round, not square

    // alpha = 1.0 everywhere inside the disc - VTK writes `opacity = 1.0`.
    // The optional AA term below only exists to avoid a jagged rim; it is off
    // by default and never produces a translucent interior.
    float alpha = 1.0;
    float crispness = clamp(pc.view.y, 0.0, 1.0);
    if (crispness > 0.0) {
        // Coverage of this fragment by the disc: a single-pixel-wide ramp on
        // the boundary only. Kept deliberately tiny so points stay crisp.
        float d = sqrt(radial_sq) * 2.0;           // 0 centre, 1 rim
        float edge = clamp((1.0 - d) * max(v_point_px, 1.0), 0.0, 1.0);
        alpha = clamp(1.0 - (1.0 - edge) * crispness, 0.0, 1.0);
    }
    outColor = vec4(vColor.rgb, vColor.a * alpha);
}
