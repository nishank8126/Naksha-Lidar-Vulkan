#version 450
// ---------------------------------------------------------------------------
// LiDAR point vertex shader.
//
// Two responsibilities (both missing/incomplete before this pass, which is why
// the Vulkan view showed sparse 1px squares instead of a dense LiDAR cloud):
//
//  1. ADAPTIVE SCREEN SIZE. gl_PointSize is derived from the point's view
//     depth so a point covers roughly its real-world footprint:
//         size_px = clamp(footprint_m * pxPerMetreAt1m / depth, minPx, maxPx)
//     pxPerMetreAt1m = 0.5 * viewportHeight / tan(fovY/2), computed per frame
//     on the CPU (PointCloudRenderer::Record) and pushed as pc.view.x. The
//     clamp keeps the footprint inside the VTK-parity band (default 1.5..6 px,
//     target 2-5 px) so density reads the same as VTK at any zoom level.
//
//  2. GPU-SIDE SHADING. Python uploads per-point *data* only (position,
//     classification id, intensity, plus the RGB that is itself data in RGB
//     mode). Colours are computed here:
//         class_id  -> ubo.classPalette[id]
//         z         -> normalise -> ubo.elevationLut[t]
//         intensity -> contrast/gamma -> ubo.intensityLut[t]
//     The three 256-entry LUTs ride in the shared FrameUbo (uploaded once per
//     load by nkv_set_point_luts) and are sampled from the app's own VTK
//     colouring helpers (gui/pointcloud_display.py), so the result matches the
//     VTK viewport instead of an approximation of it.
// ---------------------------------------------------------------------------

layout(location = 0) in vec3 inPos;         // render space (world - origin)
layout(location = 1) in vec4 inColor;       // per-point RGB = data (RGB mode)
layout(location = 2) in float inClassU;     // classification id, UNORM8 (id = round(*255))
layout(location = 3) in float inIntensity;  // raw intensity as uploaded

layout(set = 0, binding = 0) uniform FrameUbo {
    mat4 mvp;
    vec4 cameraPos;
    vec4 origin;
    vec4 classPalette[256];    // class id  -> RGBA
    vec4 elevationLut[256];    // normalized z -> RGBA
    vec4 intensityLut[256];    // normalized intensity -> RGBA
} ubo;

layout(push_constant) uniform PointParams {
    vec4 size;       // x=world footprint (m), y=minPx, z=maxPx, w=class->intensity mix
    vec4 shading;    // x=mode (NkvDisplayMode), y=zLo, z=zHi, w=zGamma
    vec4 intensity;  // x=iLo, y=iHi, z=contrast, w=gamma
    vec4 view;       // x=px per metre at 1 m, y=edge softness 0..1, z=brightness, w=unused
    vec4 parity;     // x = colour-space byte-parity flag (see the output note below)
} pc;

layout(location = 0) out vec4 vColor;
layout(location = 1) out float v_point_px;   // gl_PointSize, for edge coverage

// Mirror of NkvDisplayMode in include/naksha/naksha_vulkan_c_api.h - the value
// travels through pc.shading.x unchanged, so a display-mode switch on the
// Python side is one float, not a re-upload.
const int MODE_RGB = 0;
const int MODE_CLASSIFICATION = 1;
const int MODE_INTENSITY = 2;
const int MODE_ELEVATION = 3;
// Instant Shaded Class. The splat pass draws class colour + depth into an
// offscreen target; a separate fullscreen pass then lights it using normals
// reconstructed from that depth. Because this stage only has to emit the class
// colour (the lighting happens later), mode 4 behaves like classification for
// COLOUR but differs for SIZE and for the visibility alpha below - which is why
// it is a distinct mode rather than a flag on mode 1.
const int MODE_SHADED_CLASS_INSTANT = 4;
const int MODE_NEUTRAL = 5;
// DEPTH: grayscale by distance from the eye, matching the legacy VTK depth mode.
// This is a genuine view-space computation, NOT an alias for RGB and NOT a CPU
// colour bake - the shader derives it from the resident position stream and the
// live camera, so Depth follows zoom and orbit in both the 2D orthographic and
// the 3D perspective projection with no extra buffer and no re-upload.
const int MODE_DEPTH = 6;

// ---- byte parity with VTK (see surface.frag's OUTPUT ENCODING note) --------
// VTK paints the raw palette/ramp byte. The swapchain is
// VK_FORMAT_B8G8R8A8_SRGB, so the hardware encodes whatever this shader writes;
// pre-compensating with the exact sRGB inverse makes the framebuffer hold the
// byte VTK would have painted. pc.parity.x selects it (1 = on, the default).
vec3 srgbToLinear(vec3 c) {
    c = clamp(c, 0.0, 1.0);
    vec3 lo = c / 12.92;
    vec3 hi = pow(max(c + 0.055, 0.0) / 1.055, vec3(2.4));
    return mix(lo, hi, step(vec3(0.04045), c));
}

vec3 toDisplayByte(vec3 colour) {
    colour = clamp(colour, 0.0, 1.0);
    if (pc.parity.x < 0.5) return colour;
    return srgbToLinear(floor(colour * 255.0 + 1e-4) / 255.0);
}

float normalize01(float value, float lo, float hi) {
    return clamp((value - lo) / max(hi - lo, 1e-6), 0.0, 1.0);
}

vec3 elevationColor(float renderZ) {
    // inPos is RENDER space (world - origin) because the position buffer is
    // float32 (RenderOrigin::WorldToRenderF64ToF32), while pc.shading.yz holds
    // the WORLD z range that gui/pointcloud_display.compute_colors() derives
    // from app.data["xyz"][:, 2]. Mixing the two spaces shifts every colour on
    // the ramp, so the origin offset is added back here: the normalisation,
    // clamp and LUT index must see the same world Z the CPU path saw.
    float worldZ = renderZ + ubo.origin.z;
    float t = normalize01(worldZ, pc.shading.y, pc.shading.z);
    t = pow(t, max(pc.shading.w, 0.01));
    return ubo.elevationLut[int(t * 255.0)].rgb;
}

vec3 intensityColor(float intensity) {
    float t = normalize01(intensity, pc.intensity.x, pc.intensity.y);
    // Contrast around the mid pivot, then the perceptual gamma the Python
    // helper applies (darker mid-tones, Nakshatech look).
    float contrast = max(pc.intensity.z, 0.0);
    t = clamp((t - 0.5) * contrast + 0.5, 0.0, 1.0);
    t = pow(t, max(pc.intensity.w, 0.01));
    return ubo.intensityLut[int(t * 255.0)].rgb;
}

// DEPTH. The legacy VTK depth mode shades by distance from the eye
// (gui/naksha_cache/display_modes.py::depth_lut), and the legacy colour table
// is the GRAYSCALE ramp - the same intensityLut - not a rainbow.
//
// inPos and ubo.cameraPos are both in RENDER space (world - origin), so the
// difference is a true eye-relative distance in either projection. Using the
// radial distance rather than gl_Position.w matters: w is 1.0 for every point
// under an ORTHOGRAPHIC projection, so a w-based depth would be completely flat
// in the 2D plan view - precisely the case Depth has to keep working in.
vec3 depthColor(vec3 renderPos) {
    float d = length(renderPos - ubo.cameraPos.xyz);
    float t = normalize01(d, pc.parity.y, pc.parity.z);
    t = pow(t, max(pc.parity.w, 0.01));
    return ubo.intensityLut[int(t * 255.0)].rgb;
}

void main() {
    gl_Position = ubo.mvp * vec4(inPos, 1.0);

    // ---- VTK-equivalent screen-space point size ---------------------------
    // SOURCE OF TRUTH: gui/unified_actor_manager.py _attach_view_shader_context()
    //     float ps = max(1.0, weight_lut[c_idx]);       // weight_lut from
    //                                                      // compute_point_size()
    //     ... gl_PointSize = total_ps;                   // + border growth
    //
    // VTK sizes a point in PIXELS and that size does NOT depend on camera
    // distance, zoom, field of view, or the device's max point size. Zooming in
    // reveals more detail; it does not inflate the dots.
    //
    // This shader previously computed
    //     size_px = clamp(footprint_m * pxPerMetreAt1m / depth, minPx, maxPx)
    // which is a PERSPECTIVE/footprint model VTK never used, and then
    // PointCloudRenderer::Record() RAISED maxPx up to 4x the UI value to close
    // "grid gaps". Both of those are why Vulkan points overlapped more than
    // VTK and why zooming made them balloon. Neither belongs in a
    // screen-space-sized renderer.
    //
    // pc.size.y now carries the resolved per-class pixel size (min_px and max_px
    // are both set to the SAME value by the Python side, which is exactly the
    // "min_px == max_px" fixed-pixel mode the C API documents). The depth term
    // is deliberately gone: it is the entire source of the size mismatch.
    float pixels = pc.size.y;
    // The device limit is a CAPABILITY, not a target. Clamp only so a value
    // above the hardware limit still rasterises, never to inflate.
    float deviceMax = max(pc.view.w, 1.0);
    int mode = int(pc.shading.x + 0.5);

    if (mode == MODE_SHADED_CLASS_INSTANT) {
        // SPLAT sizing (this mode only - every other mode keeps the fixed
        // VTK-parity pixel size above). The sprite is sized from the world
        // footprint it has to cover, projected to pixels at this point's depth:
        //     size_px = footprint_m * pxPerMetreAt1m / view_depth
        // clamped to the 1..3 px band the Shaded Class mode is specified to
        // use. It grows as you zoom in and shrinks as you zoom out, so the
        // surface stays closed without the sprites ballooning. gl_Position.w
        // is the view depth for a perspective projection and 1 for an
        // orthographic one, and pc.view.x is px-per-metre-at-1m for the former
        // / px-per-world-unit for the latter, so the same expression is correct
        // for both projections.
        float footprintPx = pc.size.x * pc.view.x / max(gl_Position.w, 1e-4);
        pixels = clamp(footprintPx, pc.size.y, pc.size.z);
    }
    gl_PointSize = clamp(pixels, 1.0, min(deviceMax, 64.0));
    v_point_px = gl_PointSize;

    // ---- shading -----------------------------------------------------------
    vec3 rgb;
    float visibility = 1.0;
    if (mode == MODE_NEUTRAL) {
        rgb = vec3(128.0 / 255.0, 128.0 / 255.0, 128.0 / 255.0);
    } else if (mode == MODE_CLASSIFICATION || mode == MODE_SHADED_CLASS_INSTANT) {
        int classId = int(inClassU * 255.0 + 0.5) & 255;
        rgb = ubo.classPalette[classId].rgb;
        // Class visibility rides in the palette's alpha (nkv_set_class_visibility,
        // a 256-byte LUT upload) and is forwarded into vColor.a, which
        // point.frag discards on (`vColor.a < 0.5 -> discard`). That is how the
        // Display Mode checkbox list and a PTC's hidden classes work without
        // re-uploading a single point.
        //
        // BUG FIXED HERE: this used to be gated on MODE_SHADED_CLASS_INSTANT
        // ONLY, with a comment claiming the other modes were "byte-for-byte
        // unchanged". That was precisely the defect: in the ordinary
        // MODE_CLASSIFICATION - the actual Class mode - `visibility` stayed 1.0,
        // so a class the user had unchecked still drew at full opacity and PTC
        // visibility had no effect at all. Both class modes must honour it.
        visibility = ubo.classPalette[classId].a;
        // Return-strength modulation: real LiDAR viewers keep intensity
        // visible on top of class colours (vegetation vs. facade of the same
        // class read differently). pc.size.w blends it in.
        float strength = normalize01(inIntensity, pc.intensity.x, pc.intensity.y);
        float modulation = clamp(0.78 + 0.44 * strength, 0.55, 1.35);
        rgb = mix(rgb, rgb * modulation, clamp(pc.size.w, 0.0, 1.0));
    } else if (mode == MODE_ELEVATION) {
        rgb = elevationColor(inPos.z);
    } else if (mode == MODE_INTENSITY) {
        rgb = intensityColor(inIntensity);
    } else if (mode == MODE_DEPTH) {
        // Position + camera only. Depth must render even when the intensity
        // stream was never uploaded (it needs no attribute at all), which is why
        // this branch deliberately ignores inIntensity/inColor.
        rgb = depthColor(inPos);
    } else {
        rgb = inColor.rgb;   // RGB mode: colour is data, not shading
    }

    // Colour is DATA in RGB mode and a LUT byte in the other modes; either way it
    // is a display-domain byte value that must survive this sRGB attachment
    // unchanged, exactly as VTK paints it.
    vColor = vec4(toDisplayByte(rgb * max(pc.view.z, 0.0)),
                  (visibility < 0.5) ? 0.0 : 1.0);
}
