#version 450
// ---------------------------------------------------------------------------
// Fullscreen triangle for the Instant Shaded Class lighting pass.
//
// Deliberately has NO vertex input: gl_VertexIndex 0,1,2 generates the
// (-1,-1) (3,-1) (-1,3) triangle that covers the viewport exactly once, so the
// pass needs no vertex buffer, no vertex bindings and no per-frame uploads.
//
// The UV convention is the framebuffer's own: (0,0) is the top-left of the
// image and (1,1) the bottom-right, which is what sampling the offscreen
// colour/depth targets expects (no Y flip - Vulkan clip space is already
// Y-down).
// ---------------------------------------------------------------------------

layout(location = 0) out vec2 vUV;

void main() {
    vec2 corner = vec2(float((gl_VertexIndex << 1) & 2),
                       float(gl_VertexIndex & 2));          // (0,0) (2,0) (0,2)
    vUV = corner;
    gl_Position = vec4(corner * 2.0 - 1.0, 0.0, 1.0);       // (-1,-1) (3,-1) (-1,3)
}