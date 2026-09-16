from types import SimpleNamespace

import pytest
import vtk

from gui.grid_label_system import GridLabelManager


def _manager():
    manager = object.__new__(GridLabelManager)
    manager.app = SimpleNamespace()
    manager.original_colors = {}
    return manager


def test_follower_font_size_uses_stable_non_cumulative_scale():
    actor = vtk.vtkFollower()
    actor.SetScale(2.0, 2.0, 2.0)
    actor.text_content = "COMP"
    actor.grid_name = "COMP"
    actor._naksha_base_scale = 2.0
    actor._naksha_base_font_size = 75
    actor._naksha_label_font_size = 75
    manager = _manager()

    assert manager._apply_label_state(actor, "COMP", 150, (0, 1, 1), False)
    assert actor.GetScale()[0] == pytest.approx(8.0)
    assert manager._apply_label_state(actor, "COMP", 1, (0, 1, 1), False)
    assert actor.GetScale()[0] == pytest.approx(2.0 / 75.0)
    assert manager._capture_label_state(actor)["size"] == 1


def test_text_actor3d_font_size_changes_world_scale():
    actor = vtk.vtkTextActor3D()
    actor.SetInput("COMP")
    actor.SetScale(0.5, 0.5, 0.5)
    actor.grid_name = "COMP"
    actor._naksha_base_scale = 0.5
    actor._naksha_base_font_size = 20
    actor._naksha_label_font_size = 20
    manager = _manager()

    assert manager._apply_label_state(actor, "COMP", 40, (0, 1, 1), True)
    assert actor.GetScale()[0] == pytest.approx(2.0)
    assert manager._apply_label_state(actor, "COMP", 10, (0, 1, 1), True)
    assert actor.GetScale()[0] == pytest.approx(0.25)
    assert manager._capture_label_state(actor)["size"] == 10
