#version 450
layout(location=0) in vec3 position;
layout(set=0,binding=0) uniform FrameUbo {
    mat4 mvp; vec4 cameraPos; vec4 origin;
} frame;
void main() { gl_Position = frame.mvp * vec4(position, 1.0); }
