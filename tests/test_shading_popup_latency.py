from types import SimpleNamespace

import gui.app_window as app_window
from gui.app_window import NakshaApp


class _PropPicker:
    def __init__(self, actor=None, *, picked=True, error=False):
        self.actor = actor
        self.picked = picked
        self.error = error

    def PickFromListOn(self):
        pass

    def AddPickList(self, actor):
        self.pick_actor = actor

    def Pick(self, *_args):
        if self.error:
            raise RuntimeError("hardware picker unavailable")
        return int(self.picked)

    def GetActor(self):
        return self.actor


def _app():
    return SimpleNamespace(
        vtk_widget=SimpleNamespace(
            renderer=object(),
            interactor=SimpleNamespace(GetEventPosition=lambda: (20, 30)),
        )
    )


def test_shading_hit_uses_hardware_picker_without_constructing_cell_picker(
        monkeypatch):
    actor = object()
    monkeypatch.setattr(
        app_window.vtk, "vtkPropPicker", lambda: _PropPicker(actor)
    )
    monkeypatch.setattr(
        app_window.vtk,
        "vtkCellPicker",
        lambda: (_ for _ in ()).throw(AssertionError("slow picker constructed")),
    )

    assert NakshaApp._main_view_pick_hits_actor(_app(), actor, 20, 30)


def test_hardware_miss_returns_immediately_without_cell_scan(monkeypatch):
    actor = object()
    monkeypatch.setattr(
        app_window.vtk,
        "vtkPropPicker",
        lambda: _PropPicker(actor, picked=False),
    )
    monkeypatch.setattr(
        app_window.vtk,
        "vtkCellPicker",
        lambda: (_ for _ in ()).throw(AssertionError("slow picker constructed")),
    )

    assert not NakshaApp._main_view_pick_hits_actor(_app(), actor, 20, 30)


def test_cell_picker_remains_fallback_when_hardware_picker_errors(monkeypatch):
    actor = object()
    cell = _PropPicker(actor)
    cell.SetTolerance = lambda _value: None
    monkeypatch.setattr(
        app_window.vtk,
        "vtkPropPicker",
        lambda: _PropPicker(error=True),
    )
    monkeypatch.setattr(app_window.vtk, "vtkCellPicker", lambda: cell)

    assert NakshaApp._main_view_pick_hits_actor(_app(), actor, 20, 30)
