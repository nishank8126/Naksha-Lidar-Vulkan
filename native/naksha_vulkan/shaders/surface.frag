#version 450
// ---------------------------------------------------------------------------
// surface.frag - Shaded Class (crisp-hybrid) fragment shader.
//
// THIS FILE IS A TRANSCRIPTION OF gui/shading_display.py, NOT A NEW MODEL.
// The VTK/CPU path in gui/shading_display.py is the source of truth; every
// operation below maps to one named Python function so the two renderers cannot
// drift. vulkan_shading_parity_test.py re-derives every constant below from the
// Python module and compares this file's arithmetic against the real CPU
// functions face-by-face on the real dataset.
//
//   CPU pipeline being reproduced (per TIN face):
//     normals   _compute_face_normals()   normalize(cross(p1-p0, p2-p0)), then
//                                         the hemisphere fix
//                                         (n.z < 0 && |n.z| > 0.3 -> -n)
//     raw shade _compute_face_shade()     clip(max(N.L, ambient), 0, 1)
//     remap     _crisp_shade_chunk()      clip(targetH + (raw-rawH) * gain,
//                                             shadowFloor, 1)
//     colours   _build_crisp_base_face_colors()   cell RGB = trunc(classRGB*shade)
//     mixed     _build_static_multiclass_blend_overlays()
//                                         per-vertex class RGB, barycentric
//                                         interpolation, flat cell normal, then
//                                         VTK Phong: Ka*1 + Kd*(key + fill)
//
//   mode 4 = PASS 1, the crisp base (every face, one class colour = vertex 0)
//   mode 5 = PASS 2, the mixed-class overlay (only faces whose three displayed
//            class colours differ, each vertex keeping its true class colour)
//   mode 0..3 = the legacy Surface/ramp path (behaviour unchanged, see
//               gui/surface_mode.py); it only shares this file's output tail.
//
// OUTPUT ENCODING (byte parity with VTK)
// --------------------------------------
// VTK writes the shaded byte straight into an 8-bit RGBA buffer: what appears on
// screen is exactly trunc(classRGB * shade). The native swapchain is
// VK_FORMAT_B8G8R8A8_SRGB (Renderer::ChooseSurfaceFormat prefers it), so the
// hardware applies linear->sRGB to everything this shader writes. Writing the
// sRGB-encoded byte back as if it were linear - srgbToLinear(byte / 255) - makes
// the hardware's encode return the exact byte VTK would have written. Without
// that step the same facet renders up to ~35% brighter (0.5 -> 0.735), which was
// the single largest "same facet, different brightness" difference.
// pc.shadeParamsB.w selects it: 1 = byte parity (default), 0 = raw linear write.
// ---------------------------------------------------------------------------

layout(location = 0) in vec3 vNormal;
layout(location = 1) in vec4 vColor;
layout(location = 2) in vec3 vLocalPos;

layout(set = 0, binding = 0) uniform FrameUbo {
    mat4 mvp;
    vec4 cameraPos;
    vec4 origin;
} ubo;

layout(push_constant) uniform PushConstant {
    vec4 lightDirAmbient;
    vec4 elevRangeMode;  // x=zMin, y=zMax, z=mode, w=base light elevation (modes 4/5)
    vec4 shadeParamsA;   // x=azimuthDeg, y=sharpnessRaw, z=ambient, w=ambient floor
    vec4 shadeParamsB;   // x=keyIntensity, y=fillIntensity, z=debugStage, w=colorMode
} pc;

layout(location = 0) out vec4 outColor;

// ---- constants mirrored from gui/shading_display.py ------------------------
const float SHARP_LEGACY_MAX = 90.0;              // _SHADING_SHARPNESS_LEGACY_MAX
const float SHARP_MAX = 999.0;                    // _SHADING_SHARPNESS_MAX
const float SHARP_STRONG_MAX = 200.0;             // _SHADING_SHARPNESS_STRONG_MAX
const float SHARP_STRONG_RESPONSE = 0.90;         // _SHADING_SHARPNESS_STRONG_RESPONSE
const float BASE_LIGHT_ELEVATION_FALLBACK = 45.0; // _shading_fixed_light_elevation default
const float MIN_LIGHT_ELEVATION_DEG = 12.0;       // _shading_effective_light_elevation floor
const float AMBIENT_FLOOR_FALLBACK = 0.08;        // NAKSHA_SHADING_BLEND_AMBIENT_FLOOR default
const float GAIN_MIN = 0.35;                      // _crisp_sharpness_contrast_gain clip
const float GAIN_MAX = 6.50;

// ---- staged debug output (pc.shadeParamsB.z) -------------------------------
// 0 = final shaded colour (production), 1 = face normal, 2 = lighting factor,
// 3 = class colour only, 4 = raw N.L before the crisp remap. Select with
// RenderBackend.set_crisp_debug_mode() / NAKSHA_SHADING_DEBUG_STAGE.
const int DEBUG_FINAL = 0;
const int DEBUG_NORMAL = 1;
const int DEBUG_LIGHTING = 2;
const int DEBUG_CLASS = 3;
const int DEBUG_RAW_SHADE = 4;

// _shading_sharpness_response(value) -> (legacy, overdrive)
void sharpnessResponse(float value, out float legacy, out float overdrive) {
    value = clamp(value, 0.0, SHARP_MAX);
    legacy = clamp(value / SHARP_LEGACY_MAX, 0.0, 1.0);
    if (value <= SHARP_LEGACY_MAX) { overdrive = 0.0; return; }
    float strongSpan = max(SHARP_STRONG_MAX - SHARP_LEGACY_MAX, 1.0);
    if (value <= SHARP_STRONG_MAX) {
        float strongT = (value - SHARP_LEGACY_MAX) / strongSpan;
        overdrive = SHARP_STRONG_RESPONSE * strongT;
    } else {
        float tailSpan = max(SHARP_MAX - SHARP_STRONG_MAX, 1.0);
        float tailT = (value - SHARP_STRONG_MAX) / tailSpan;
        overdrive = SHARP_STRONG_RESPONSE + (1.0 - SHARP_STRONG_RESPONSE) * tailT;
    }
    overdrive = clamp(overdrive, 0.0, 1.0);
}

// _crisp_sharpness_contrast_gain(sharpness_value)
float contrastGain(float legacy, float overdrive) {
    return clamp(0.55 + 0.90 * legacy + 5.0 * overdrive, GAIN_MIN, GAIN_MAX);
}

// _shading_effective_light_elevation(app, sharpness_overdrive)
float effectiveLightElevation(float base, float overdrive) {
    return clamp(base - (base - MIN_LIGHT_ELEVATION_DEG) * overdrive,
                 MIN_LIGHT_ELEVATION_DEG, 85.0);
}

// Same compass-flipped light-vector construction used by both
// _compute_face_shade (through _compute_shading) and
// _configure_nakshatech_color_blend_lighting.
vec3 lightVectorFromAzimuthElevation(float azimuthDeg, float elevationDeg) {
    float zenith = radians(90.0 - elevationDeg);
    float azMath = radians(360.0 - azimuthDeg + 90.0);
    return vec3(sin(zenith) * cos(azMath), sin(zenith) * sin(azMath), cos(zenith));
}

// _compute_face_normals(). The mesh is uploaded with one flat cell normal
// repeated on all three vertices of a face, so a per-vertex transform here is
// the per-face transform. The hemisphere fix is mandatory: the CPU flips
// downward-facing normals (n.z < 0 && |n.z| > 0.3) and a face lit by an
// unflipped normal is a completely different brightness - exactly the kind of
// facet difference this port exists to remove. A zero-length normal stays zero,
// matching the CPU's division by max(len, 1e-10); normalize(0) would be NaN.
vec3 faceNormal(vec3 n) {
    float len = length(n);
    if (len <= 1e-10) return vec3(0.0);
    vec3 u = n / len;
    if (u.z < 0.0 && abs(u.z) > 0.3) u = -u;
    return u;
}

// _compute_shading(): raw = clip(max(N.L, ambient), 0, 1).
// The ambient clamp is NOT cosmetic. cache.shade bakes it in before
// _crisp_shade_chunk re-centres around the horizontal pivot, so a facet pointing
// away from the sun sits at "ambient", not at its negative dot product. Above
// Sharpness 90 (where dark facets fall below the shadow floor) the two differ by
// up to (sin(baseElev) - sin(effElev)) * gain on every back-facing facet.
float rawShade(float ndl, float ambient) {
    return clamp(max(ndl, ambient), 0.0, 1.0);
}

// np.clip(rgb * sh, 0, 255).astype(np.uint8) TRUNCATES; rounding here would be a
// systematic +1 level against the CPU. The epsilon absorbs the fp32 round-trip
// through the UNORM8 colour attribute (1/255 is not exactly representable).
vec3 truncateToByte(vec3 value255) {
    return floor(clamp(value255, 0.0, 255.0) + 1e-4) / 255.0;
}

// Exact sRGB transfer function, used to pre-compensate the attachment encode.
vec3 srgbToLinear(vec3 c) {
    c = clamp(c, 0.0, 1.0);
    vec3 lo = c / 12.92;
    vec3 hi = pow(max(c + 0.055, 0.0) / 1.055, vec3(2.4));
    return mix(lo, hi, step(vec3(0.04045), c));
}

// The CPU's 0..1 byte-space value -> what this sRGB attachment must be given so
// that the framebuffer ends up holding exactly that byte.
vec3 present(vec3 colour) {
    colour = clamp(colour, 0.0, 1.0);
    if (pc.shadeParamsB.w < 0.5) return colour;   // legacy raw write
    return srgbToLinear(truncateToByte(colour * 255.0));
}

// Shared staged-output tail for the crisp modes: the debug stage isolates one
// factor of the pipeline (normal / lighting / class colour / raw dot) so a
// mismatch can be attributed to a single stage instead of guessed at.
vec3 stageOutput(int stage, vec3 n, vec3 albedo, float shade, float rawShadeValue) {
    if (stage == DEBUG_NORMAL) return present(n * 0.5 + 0.5);
    if (stage == DEBUG_LIGHTING) return present(vec3(shade));
    if (stage == DEBUG_RAW_SHADE) return present(vec3(rawShadeValue));
    if (stage == DEBUG_CLASS) return present(albedo);
    return present(albedo * shade);
}

void main() {
    int mode = int(pc.elevRangeMode.z + 0.5);
    int stage = int(pc.shadeParamsB.z + 0.5);
    vec3 albedo = vColor.rgb;

    if (mode == 3) {              // colours already fully baked by the CPU
        outColor = vec4(present(albedo), 1.0);
        return;
    }

    vec3 n;
    if (mode == 0) {
        n = normalize(cross(dFdx(vLocalPos), dFdy(vLocalPos)));
    } else if (mode == 4 || mode == 5) {
        n = faceNormal(vNormal);   // flat cell normal + the CPU hemisphere fix
    } else {
        n = normalize(vNormal);
    }
    // The two-sided flip is a legacy-path behaviour only: VTK's flat-interpolated
    // cell normal is used exactly as supplied for the crisp modes, so flipping it
    // there would light the terrain from the wrong side.
    if (!gl_FrontFacing && mode != 4 && mode != 5) n = -n;

    if (mode == 2) {
        float lo = pc.elevRangeMode.x;
        float hi = max(pc.elevRangeMode.y, lo + 1e-6);
        float t = clamp((vLocalPos.z + ubo.origin.z - lo) / (hi - lo), 0.0, 1.0);
        outColor = vec4(present(mix(vec3(0.1, 0.4, 0.85), vec3(1.0, 0.8, 0.0),
                                   smoothstep(0.8, 1.0, t))), 1.0);
        return;
    }

    // ---- mode 4: crisp base (PASS 1) == _crisp_shade_chunk ----------------
    if (mode == 4) {
        float azimuth = pc.shadeParamsA.x;
        float sharpnessRaw = pc.shadeParamsA.y;
        float ambientUser = clamp(pc.shadeParamsA.z, 0.0, 0.95);
        float ambientFloor = (pc.shadeParamsA.w >= 0.0)
            ? pc.shadeParamsA.w : AMBIENT_FLOOR_FALLBACK;
        float baseElev = (pc.elevRangeMode.w > 0.0)
            ? pc.elevRangeMode.w : BASE_LIGHT_ELEVATION_FALLBACK;

        float legacy, overdrive;
        sharpnessResponse(sharpnessRaw, legacy, overdrive);
        float gain = contrastGain(legacy, overdrive);
        float effElev = effectiveLightElevation(baseElev, overdrive);
        float shadowFloor = max(ambientUser, ambientFloor);

        vec3 L = lightVectorFromAzimuthElevation(azimuth, effElev);
        float raw = rawShade(dot(n, L), ambientUser);   // == cache.shade[face]

        // A horizontal normal gives N.L == sin(elevation). Re-centre there so a
        // flat face keeps its brightness and only real TIN slope/aspect is
        // amplified; Ambient stays the shadow floor.
        float rawHorizontal = max(sin(radians(effElev)), ambientUser);
        float targetHorizontal = max(sin(radians(baseElev)), shadowFloor);
        float shade = clamp(targetHorizontal + (raw - rawHorizontal) * gain,
                            shadowFloor, 1.0);

        outColor = vec4(stageOutput(stage, n, albedo, shade, raw), 1.0);
        return;
    }

    // ---- mode 5: mixed-class overlay (PASS 2) ----------------------------
    // _configure_nakshatech_color_blend_lighting() drives a real VTK light rig:
    //   renderer.SetAmbient(1,1,1)             -> scene ambient = white
    //   prop.SetAmbient(effective_ambient)     -> Ka
    //   prop.SetDiffuse(1 - effective_ambient) -> Kd
    //   key light  = unit(azimuth, effElev),     intensity = key
    //   fill light = (-Lx, -Ly, max(Lz, 0.20)),  intensity = fill
    // VTK reuses the scalar RGB as both the ambient and the diffuse material
    // colour, so
    //   shade = clamp(Ka*1 + Kd*(key*max(N.Lkey,0) + fill*max(N.Lfill,0)), 0, 1)
    //   pixel = trunc(barycentric(vertex RGB) * shade)
    // albedo is the rasterizer's barycentric interpolation of the three vertices'
    // true class colours (vColor varies per-vertex on this buffer, unlike PASS 1
    // where all three vertices share one colour).
    if (mode == 5) {
        float azimuth = pc.shadeParamsA.x;
        float sharpnessRaw = pc.shadeParamsA.y;
        float ambientUser = clamp(pc.shadeParamsA.z, 0.0, 0.95);
        float ambientFloor = (pc.shadeParamsA.w >= 0.0)
            ? pc.shadeParamsA.w : AMBIENT_FLOOR_FALLBACK;
        float baseElev = (pc.elevRangeMode.w > 0.0)
            ? pc.elevRangeMode.w : BASE_LIGHT_ELEVATION_FALLBACK;
        float keyIntensity = pc.shadeParamsB.x;
        float fillIntensity = pc.shadeParamsB.y;

        float legacy, overdrive;
        sharpnessResponse(sharpnessRaw, legacy, overdrive);
        float effElev = effectiveLightElevation(baseElev, overdrive);
        float Ka = max(ambientUser, ambientFloor);   // prop.SetAmbient(effective_ambient)
        float Kd = max(0.0, 1.0 - Ka);               // prop.SetDiffuse(1 - effective_ambient)

        vec3 Lkey = lightVectorFromAzimuthElevation(azimuth, effElev);
        float fillZ = max(Lkey.z, 0.20);
        vec3 Lfill = normalize(vec3(-Lkey.x, -Lkey.y, fillZ));

        float diff = keyIntensity * max(dot(n, Lkey), 0.0) +
                     fillIntensity * max(dot(n, Lfill), 0.0);
        float shade = clamp(Ka + Kd * diff, 0.0, 1.0);

        outColor = vec4(stageOutput(stage, n, albedo, shade, diff), 1.0);
        return;
    }

    // ---- legacy Surface/ramp path (mode 0/1) - arithmetic unchanged ------
    vec3 L = normalize(pc.lightDirAmbient.xyz);
    float diff = max(dot(n, L), 0.0);
    float amb = pc.lightDirAmbient.w;
    outColor = vec4(present(albedo * (amb + (1.0 - amb) * diff)), 1.0);
}
