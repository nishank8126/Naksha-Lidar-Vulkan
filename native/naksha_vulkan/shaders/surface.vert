#version 450

layout(location = 0) in vec3 inPos;
layout(location = 1) in vec3 inNormal;
layout(location = 2) in vec4 inColor;

layout(set = 0, binding = 0) uniform FrameUbo {
    mat4 mvp;
    vec4 cameraPos;
    vec4 origin;
} ubo;

layout(push_constant) uniform PushConstant {
    vec4 lightDirAmbient;
    vec4 elevRangeMode;
    vec4 shadeParamsA;   // x=azimuthDeg, y=sharpnessRaw, z=ambient, w=unused (crisp-hybrid modes 4/5 only)
    vec4 shadeParamsB;   // x=keyIntensity, y=fillIntensity, z/w=unused
} pc;

layout(location = 0) out vec3 vNormal;
layout(location = 1) out vec4 vColor;
layout(location = 2) out vec3 vLocalPos;

void main() {
    gl_Position = ubo.mvp * vec4(inPos, 1.0);
    vNormal = inNormal;
    vColor = inColor;
    vLocalPos = inPos;
}
