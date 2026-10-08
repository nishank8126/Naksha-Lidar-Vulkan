"""vulkan_3d_probe.py - why does the 3D (Shift+P / perspective) path render a
black frame while the 2D path renders content?

Reuses the harness in vulkan_resize_test.py, stops after the 3D capture, and
dumps describe_camera() (NAKSHA_VULKAN_CAMERA_DEBUG=1) at every interesting
step so the engine's ACTUAL projection, near/far and eye are on record.

Usage:
    set NAKSHA_VULKAN_VALIDATION=1
    venv\\Scripts\\python.exe vulkan_3d_probe.py 2> probe3d_err.log
"""
from __future__ import annotations

import os
import sys
import time

os.environ.setdefault("NAKSHA_VULKAN_CAMERA_DEBUG", "1")

import numpy as np  # noqa: F401

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from vulkan_resize_test import (  # noqa: E402
    LAS_PATH, OUT_DIR, check, report, pump, wait_until,
    engine_capture, grab_desktop, _suppress_modal_dialogs,
)


def main() -> int:
    from PySide6.QtCore import QPoint, Qt, QEvent
    from PySide6.QtGui import QKeyEvent, QMouseEvent
    from PySide6.QtWidgets import QApplication

    from gui.app_window import NakshaApp
    from gui.dialogs.load_pointcloud_dialog import DEFAULT_IMPORT_OPTIONS
    from gui import render_backend as rb_mod
    from gui.render_backend import BackendState

    if not os.path.isfile(LAS_PATH):
        print(f"[3d] LAS not found: {LAS_PATH}", flush=True)
        return 1

    _suppress_modal_dialogs()
    app = QApplication.instance() or QApplication(sys.argv[:1])
    print("[3d] constructing NakshaApp ...", flush=True)
    win = NakshaApp()
    win.show()
    win.raise_()
    win.activateWindow()
    pump(app, 1.5)

    rb = getattr(win, "render_backend", None)
    if rb is None or not rb.active:
        print(f"[3d] backend not active: {getattr(rb, 'state', None)}", flush=True)
        return report()

    win.open_file(filenames=[LAS_PATH], import_options=dict(DEFAULT_IMPORT_OPTIONS),
                  prompt_import=False)
    loaded = wait_until(app, lambda: (
        getattr(win, "data", None) is not None and win.data.get("xyz") is not None
        and getattr(win, "_file_loader_worker", None) is None), timeout=900)
    check("LAS loaded", loaded)
    if not loaded:
        return report()
    pump(app, 2.0)

    win.set_display_mode("shaded_class")
    first = wait_until(app, lambda: rb.state == BackendState.ACTIVE
                       and rb_mod.get_present_count() > 0, timeout=120)
    check("first Vulkan frame presented", first,
          f"present={rb_mod.get_present_count()}")
    if not first:
        return report()

    os.makedirs(OUT_DIR, exist_ok=True)
    win.raise_()
    win.activateWindow()
    pump(app, 0.5)

    def cam_line(label: str) -> str:
        line = rb.describe_camera(label)
        print(f"[3d] {line}", flush=True)
        return line

    def send_key(widget, key, modifiers):
        QApplication.sendEvent(widget, QKeyEvent(QEvent.Type.KeyPress, key, modifiers))
        app.processEvents()

    def drag(widget, button, modifiers, p0, p1):
        g0 = widget.mapToGlobal(p0)
        QApplication.sendEvent(widget, QMouseEvent(
            QEvent.Type.MouseButtonPress, p0, p0, g0, button, button, modifiers))
        for step in (0.34, 0.67, 1.0):
            p = QPoint(int(p0.x() + (p1.x() - p0.x()) * step),
                       int(p0.y() + (p1.y() - p0.y()) * step))
            g = widget.mapToGlobal(p)
            QApplication.sendEvent(widget, QMouseEvent(
                QEvent.Type.MouseMove, p, p, g, Qt.MouseButton.NoButton, button,
                modifiers))
            app.processEvents()
        g1 = widget.mapToGlobal(p1)
        QApplication.sendEvent(widget, QMouseEvent(
            QEvent.Type.MouseButtonRelease, p1, p1, g1, button, button, modifiers))
        app.processEvents()

    w = rb.vulkan_widget
    c = QPoint(w.width() // 2, w.height() // 2)

    cam_line("2d-before-shift-P")
    eng, em = engine_capture(rb, os.path.join(OUT_DIR, "3d_01_2d.png"))
    check("2D: engine renders content", eng is not None and em.get("std", 0) > 2.0,
          f"std={em.get('std', -1):.2f} {em}")

    # ---- Shift+P : VTK -> perspective --------------------------------------
    before = rb_mod.get_present_count()
    send_key(win, Qt.Key.Key_P, Qt.KeyboardModifier.ShiftModifier)
    pump(app, 1.5)
    vtk_ok = wait_until(app, lambda: not win.vtk_widget.renderer.GetActiveCamera()
                        .GetParallelProjection(), timeout=10)
    wait_until(app, lambda: rb_mod.get_present_count() > before, timeout=15)
    check("Shift+P -> VTK perspective", bool(vtk_ok))
    pump(app, 1.0)
    cam_line("3d-after-shift-P")
    print(f"[3d] engine projection right after Shift+P: "
          f"{rb.vulkan_backend.get_camera_projection()}", flush=True)

    eng, em = engine_capture(rb, os.path.join(OUT_DIR, "3d_02_after_shiftP.png"))
    check("3D right after Shift+P: engine renders content",
          eng is not None and em.get("std", 0) > 2.0, f"{em}")

    # ---- orbit --------------------------------------------------------------
    before = rb_mod.get_present_count()
    drag(w, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
         c, c + QPoint(110, -70))
    ok = wait_until(app, lambda: rb_mod.get_present_count() > before, timeout=15)
    check("3D: orbit produces new frames", ok,
          f"{before} -> {rb_mod.get_present_count()}")
    pump(app, 1.0)
    cam_line("3d-after-orbit")
    eng, em = engine_capture(rb, os.path.join(OUT_DIR, "3d_03_after_orbit.png"))
    check("3D after orbit: engine renders content",
          eng is not None and em.get("std", 0) > 2.0, f"{em}")

    # ---- pan (the step that blacked the frame out in the resize test) -------
    before = rb_mod.get_present_count()
    drag(w, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ShiftModifier,
         c, c + QPoint(-80, 55))
    ok = wait_until(app, lambda: rb_mod.get_present_count() > before, timeout=15)
    check("3D: pan produces new frames", ok,
          f"{before} -> {rb_mod.get_present_count()}")
    pump(app, 1.0)
    cam_line("3d-after-pan")
    eng, em = engine_capture(rb, os.path.join(OUT_DIR, "3d_03b_after_pan.png"))
    check("3D after pan: engine renders content",
          eng is not None and em.get("std", 0) > 2.0, f"{em}")

    # ---- what does the engine actually hold? --------------------------------
    is_ortho, scale = rb.vulkan_backend.get_camera_projection()
    print(f"[3d] ENGINE projection={'ORTHO' if is_ortho == 1 else 'PERSP'} "
          f"parallel_scale={scale}", flush=True)
    check("engine switched to perspective", is_ortho == 0,
          f"is_ortho={is_ortho} scale={scale}")

    # ---- and back to 2D ------------------------------------------------------
    before = rb_mod.get_present_count()
    send_key(win, Qt.Key.Key_F, Qt.KeyboardModifier.ShiftModifier)
    pump(app, 1.5)
    wait_until(app, lambda: rb_mod.get_present_count() > before, timeout=15)
    pump(app, 1.0)
    cam_line("back-to-2d")
    eng, em = engine_capture(rb, os.path.join(OUT_DIR, "3d_04_back_2d.png"))
    check("back in 2D: engine renders content",
          eng is not None and em.get("std", 0) > 2.0, f"{em}")

    return report()


if __name__ == "__main__":
    _code = 3
    try:
        _code = main()
    except Exception:
        import traceback
        traceback.print_exc()
        _code = 3
    finally:
        sys.exit(_code)
