#version 450
// Legacy VTK Surface uses CPU-baked, flat cell RGB with lighting disabled.
// Keep one shared position per vertex and fetch that exact cell byte here.
layout(set=1,binding=0,std430) readonly buffer CellColors { uint rgba[]; } cells;
layout(push_constant) uniform Draw { uint firstTriangle; } draw;
layout(location=0) out vec4 outColor;
void main() {
    uint packed = cells.rgba[draw.firstTriangle + uint(gl_PrimitiveID)];
    vec3 rgb = vec3(packed & 255u, (packed >> 8u) & 255u, (packed >> 16u) & 255u) / 255.0;
    vec3 linearRGB = mix(rgb / 12.92, pow((rgb + 0.055) / 1.055, vec3(2.4)), greaterThan(rgb, vec3(0.04045)));
    outColor = vec4(linearRGB, 1.0);
}
