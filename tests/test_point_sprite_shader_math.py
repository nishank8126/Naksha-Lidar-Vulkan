import inspect
import math

import pytest

from gui import unified_actor_manager as actors


@pytest.mark.parametrize("point_size,core_size", [(1.0, 1.0), (2.5, 2.5), (4.1, 2.5), (9.0, 3.0)])
def test_squared_radius_border_test_matches_original_math(point_size, core_size):
    for x in (-0.51, -0.5, -0.33, 0.0, 0.24, 0.49, 0.5, 0.51):
        for y in (-0.51, -0.5, -0.17, 0.0, 0.31, 0.49, 0.5, 0.51):
            old_distance = math.hypot(x, y) * point_size
            old_inside = old_distance <= point_size * 0.5
            old_border = old_distance >= core_size * 0.5

            radial_sq = (x * x) + (y * y)
            new_inside = radial_sq <= 0.25
            new_border = (
                radial_sq * point_size * point_size
                >= 0.25 * core_size * core_size
            )

            assert new_inside is old_inside
            assert new_border is old_border


def test_active_unified_shader_avoids_fragment_square_root():
    shader_builder = inspect.getsource(actors._attach_view_shader_context)

    assert "float radial_sq = dot(uv25, uv25);" in shader_builder
    assert "float dist_px = length(uv25)" not in shader_builder
    assert "_shaders_finalized_v29" in shader_builder


def test_main_point_shader_uses_camera_range_invariant_border_depth():
    shader_builder = inspect.getsource(actors._attach_view_shader_context)

    # VTK declares registered custom uniforms automatically. Re-declaring
    # these names in the replacement produces a GLSL declaration conflict.
    assert '"uniform float naksha_near;\\n"' not in shader_builder
    assert '"uniform float naksha_far;\\n"' not in shader_builder
    assert '"float world_to_ndc_bias(float world_bias) {\\n"' in shader_builder
    assert "gl_FragDepth" in shader_builder
    assert "_BORDER_DEPTH_BIAS_STRUCTURED_WORLD" in shader_builder
    assert "world_to_ndc_bias({_BORDER_DEPTH_BIAS_STRUCTURED_WORLD})" in shader_builder
    assert actors._BORDER_DEPTH_BIAS_STRUCTURED_WORLD == pytest.approx(0.25)


def test_cut_actor_uses_unified_shader_for_source_class_weights():
    from gui.cross_section.cut_section_controller import CutSectionController

    builder = inspect.getsource(CutSectionController._build_cut_unified_actor)

    assert "cut_ctx = ViewShaderContext(slot_idx=5)" in builder
    assert "cut_ctx.load_from_palette(palette, border_pct, actual_pt_size)" in builder
    assert "_attach_view_shader_context(actor, cut_ctx, actor_name)" in builder
    assert "actor._naksha_disable_custom_shader = True" not in builder


def test_cut_palette_is_bound_to_the_data_owning_source_view():
    from gui.cross_section.cut_section_controller import CutSectionController

    finalizer = inspect.getsource(CutSectionController._finalize_dynamic_cut_section)
    sync = inspect.getsource(CutSectionController.sync_palette_from_source_view)

    assert "self._cut_source_view_index = int(active_view)" in finalizer
    assert "'_cut_source_view_index'" in sync
