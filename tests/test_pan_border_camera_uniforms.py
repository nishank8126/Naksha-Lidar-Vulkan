from gui.unified_actor_manager import _update_actor_camera_uniforms


class _Uniforms:
    def __init__(self):
        self.values = {}

    def SetUniformf(self, name, value):
        self.values[name] = value


class _ShaderProperty:
    def __init__(self, uniforms):
        self.uniforms = uniforms

    def GetFragmentCustomUniforms(self):
        return self.uniforms


class _Camera:
    def __init__(self, clipping_range):
        self.clipping_range = clipping_range

    def GetClippingRange(self):
        return self.clipping_range


class _Renderer:
    def __init__(self, camera):
        self.camera = camera

    def GetActiveCamera(self):
        return self.camera


class _Actor:
    def __init__(self, renderer, uniforms, visible=True):
        self._naksha_renderer = renderer
        self.shader_property = _ShaderProperty(uniforms)
        self.visible = visible

    def GetVisibility(self):
        return int(self.visible)

    def GetShaderProperty(self):
        return self.shader_property


def test_pan_clipping_range_is_pushed_to_fragment_shader():
    uniforms = _Uniforms()
    camera = _Camera((2.5, 900.0))
    actor = _Actor(_Renderer(camera), uniforms)

    assert _update_actor_camera_uniforms(actor)
    assert uniforms.values == {"naksha_near": 2.5, "naksha_far": 900.0}

    camera.clipping_range = (10.0, 1200.0)
    assert _update_actor_camera_uniforms(actor)
    assert uniforms.values == {"naksha_near": 10.0, "naksha_far": 1200.0}


def test_hidden_actor_camera_uniform_update_is_skipped():
    uniforms = _Uniforms()
    actor = _Actor(_Renderer(_Camera((1.0, 100.0))), uniforms, visible=False)

    assert not _update_actor_camera_uniforms(actor)
    assert uniforms.values == {}
