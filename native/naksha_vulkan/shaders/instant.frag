#version 450
// ---------------------------------------------------------------------------
// Instant Shaded Class - fullscreen lighting pass.
//
// INPUT (no CPU mesh, no Delaunay, no stored normals):
//   baseColor  the offscreen target the splat pass just wrote. In this mode the
//              point pipeline runs in classification mode, so every covered
//              pixel already holds exactly the class palette colour (the
//              requirement's "class LUT" attachment - it carries the class
//              colour directly instead of a redundant class id, so no second
//              attachment and no second lookup are needed).
//   depthTex   the matching depth attachment. The normal is reconstructed from
//              it in screen space: finite differences of the un-projected
//              position of neighbouring pixels, plus a discontinuity guard so a
//              pixel on a cliff/occlusion edge never takes the normal of the
//              surface behind it.
//
// LIGHTING mirrors gui/shading_display.py::_compute_shading exactly - the same
// Nakshatech/ArcGIS hillshade the legacy CPU path uses:
//     zenith  = radians(90 - elevation)
//     azMath  = radians(360 - azimuth + 90)
//     L       = (sin(zenith)cos(azMath), sin(zenith)sin(azMath), cos(zenith))
//     shade   = clamp(max(dot(N, L), 0), 0, 1)   with the ambient floor
// so the instant path and the legacy path converge on the same look.
//
// COLOUR SPACE: the offscreen target is the swapchain format (sRGB), so
// sampling already yields linear light; lighting is done in linear and the
// sRGB swapchain re-encodes on write. No byte-parity compensation is needed
// here because it round-trips: what the splat pass stored is what comes back.
// ---------------------------------------------------------------------------

layout(location = 0) in vec2 vUV;
layout(location = 0) out vec4 outColor;

// set 1: the offscreen targets (set 0 stays the shared Frame UBO).
//   binding 0  class colour
//   binding 1  depth
//   binding 2  STORED oct16 normal - the PRODUCTION normal source. Read with a
//              NEAREST sampler so each pixel keeps exactly the normal of the
//              splat that won its depth test: no interpolation, no bleeding
//              between classes or between surfaces.
layout(set = 1, binding = 0) uniform sampler2D baseColor;
layout(set = 1, binding = 1) uniform sampler2D depthTex;
layout(set = 1, binding = 2) uniform sampler2D normalTex;

layout(push_constant) uniform LightingParams {
    vec4 sun;         // xyz = light direction (render space), w = ambient floor
    vec4 params;      // x = debug stage, y = normal radius (texels), z = 1/width, w = 1/height
    vec4 camera;      // xyz = camera position, w = NORMAL SOURCE (0 stored, 1 screen)
    vec4 background;  // rgb = viewport clear colour, a = unused
    mat4 invMvp;      // clip -> render space (Vulkan NDC z in [0,1])
} pc;

// Debug stages (mirrors NAKSHA_SHADED_DEBUG_VIEW on the Python side):
// 0 final shaded colour, 1 normal, 2 lighting factor, 3 class colour (base).
const float DEBUG_NORMAL = 1.0;
const float DEBUG_LIGHT = 2.0;
const float DEBUG_CLASS = 3.0;

// ---------------------------------------------------------------------------
// Octahedral decode (standard separable formulation). The stored stream is
// 2 x signed int16 mapped to [-1,1]; this turns it back into a unit vector.
//
// Orientation semantics are fixed by the GENERATOR, not here: this must agree
// with the oct16 encoder exactly, or every normal is mirrored about the
// diagonal - which is precisely what the DEBUG=normal view exists to catch.
// ---------------------------------------------------------------------------
vec3 octDecode(vec2 f) {
    vec3 n = vec3(f.x, f.y, 1.0 - abs(f.x) - abs(f.y));
    float t = max(-n.z, 0.0);
    n.x += (n.x >= 0.0) ? -t : t;
    n.y += (n.y >= 0.0) ? -t : t;
    return normalize(n);
}

// Vulkan keeps depth in [0,1] with z/clip.w in [0,1] too, so the clip-space z
// is the raw depth value (NOT depth*2-1, which would be the GL convention and
// would un-project the entire scene onto a degenerate slab).
vec3 reconstruct(vec2 uv, float depth) {
    vec4 clip = vec4(uv * 2.0 - 1.0, depth, 1.0);
    vec4 p = pc.invMvp * clip;
    // Guard against a point exactly on the far plane / behind the camera.
    return (abs(p.w) < 1e-8) ? vec3(0.0) : p.xyz / p.w;
}

float sampleDepth(vec2 uv) {
    return texture(depthTex, clamp(uv, vec2(0.0), vec2(1.0))).r;
}

void main() {
    // Sample FIRST: the background branch deliberately PASSES THE VALUE THROUGH
    // rather than re-emitting pc.background. Writing x and reading it back is
    // sRGB-encode-then-decode, i.e. the identity, for ANY clear semantics the
    // render pass may have used - re-emitting the CPU clear colour instead would
    // only match if the hardware's clear happened to encode the same way. This
    // makes the viewport background byte-exact in this mode by construction.
    vec3 base = texture(baseColor, vUV).rgb;
    float depth = sampleDepth(vUV);
    // Cleared depth == no splat landed here -> viewport background.
    if (depth >= 1.0) {
        outColor = vec4(base, 1.0);
        return;
    }

    float debugStage = pc.params.x;
    vec3 n;

    if (pc.camera.w < 0.5) {
        // ------------------------------------------------------------------
        // PRODUCTION: STORED oct16x2 normal.
        // ------------------------------------------------------------------
        // Exactly the normal of the splat that won this pixel's depth test -
        // no reconstruction, no filtering, no cross-surface interpolation.
        // This is why the shaded surface is stable instead of per-pixel noise.
        n = octDecode(texture(normalTex, vUV).rg);
    } else {
        // ------------------------------------------------------------------
        // DEV FALLBACK: screen-space depth reconstruction.
        // ------------------------------------------------------------------
        // Kept for debugging and A/B comparison only. It is NOT the production
        // source: on a 1-3 px splat depth buffer which point wins the depth
        // test differs per pixel, so a 1-texel difference measures that noise
        // rather than the surface. The neighbourhood radius below mitigates it
        // but cannot remove it.
        vec3 pos = reconstruct(vUV, depth);
        // For each axis take the neighbour whose depth is CLOSEST to the
        // centre: at a silhouette or a step in the terrain the other neighbour
        // belongs to a different surface. A cleared (>=1.0) neighbour is
        // infinite distance and loses automatically.
        vec2 rad = max(pc.params.y, 1.0) * pc.params.zw;
        float dL = sampleDepth(vUV - vec2(rad.x, 0.0));
        float dR = sampleDepth(vUV + vec2(rad.x, 0.0));
        float dU = sampleDepth(vUV - vec2(0.0, rad.y));
        float dD = sampleDepth(vUV + vec2(0.0, rad.y));

        float axSel = (abs(dL - depth) <= abs(dR - depth)) ? -rad.x : rad.x;
        float dXSel = (abs(dL - depth) <= abs(dR - depth)) ? dL : dR;
        float aySel = (abs(dU - depth) <= abs(dD - depth)) ? -rad.y : rad.y;
        float dYSel = (abs(dU - depth) <= abs(dD - depth)) ? dU : dD;

        vec3 px = (dXSel >= 1.0) ? vec3(0.0)
                                 : reconstruct(vUV + vec2(axSel, 0.0), dXSel) - pos;
        vec3 py = (dYSel >= 1.0) ? vec3(0.0)
                                 : reconstruct(vUV + vec2(0.0, aySel), dYSel) - pos;

        n = cross(px, py);
        float nLen = length(n);
        vec3 V = pc.camera.xyz - pos;
        float vLen = length(V);
        if (nLen < 1e-12 || vLen < 1e-12) {
            n = (vLen > 1e-12) ? V / vLen : vec3(0.0, 0.0, 1.0);
        } else {
            n /= nLen;
            if (dot(n, V) < 0.0) n = -n;
        }
    }

    if (debugStage == DEBUG_NORMAL) {
        outColor = vec4(n * 0.5 + 0.5, 1.0);
        return;
    }
    if (debugStage == DEBUG_CLASS) {
        outColor = vec4(base, 1.0);
        return;
    }

    float ndl = max(dot(n, pc.sun.xyz), 0.0);
    float light = clamp(max(ndl, pc.sun.w), 0.0, 1.0);
    if (debugStage == DEBUG_LIGHT) {
        outColor = vec4(vec3(light), 1.0);
        return;
    }
    outColor = vec4(base * light, 1.0);
}