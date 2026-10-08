"""vulkan_qt_input_test.py - focused regression test for the PySide6 Qt enum
crash in the Vulkan viewport input handling (gui/render_backend.py).

The crash was an AttributeError raised on the FIRST mouse event reaching
_VulkanSurfaceWidget.mousePressEvent, because the module bound `QtCore` (the
MODULE) to `_Qt` while PySide6 exposes enums as scoped members of the Qt CLASS.

This test drives the REAL widget handlers with REAL Qt mouse/wheel events
(PySide6.QtTest.QTest) and asserts:

  * no AttributeError / no traceback on any button,
  * middle-drag still pans (rig.target moves, orientation does NOT),
  * left-drag still orbits (azimuth/elevation move, target does NOT),
  * shift+left-drag still pans (the modifier branch),
  * right-press is inert but does not raise,
  * wheel still dollies.

Run:  python vulkan_qt_input_test.py
Exit 0 = all passed, 1 = a failure, 3 = harness error.
"""
from __future__ import annotations

import os
import sys
import traceback

# The app prints emoji (e.g. the shading-accelerator banner) and a redirected
# stdout defaults to cp1252 on this machine, which would raise
# UnicodeEncodeError before a single Vulkan check ran. Same guard the other
# harnesses use.
for _name in ("stdout", "stderr"):
    _s = getattr(sys, _name, None)
    try:
        if _s is not None and hasattr(_s, "reconfigure"):
            _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import numpy as np

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

_CHECKS: list = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    ok = bool(ok)
    _CHECKS.append((name, ok, detail))
    print(f"[qt] {'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""),
          flush=True)
    return ok


class _OwnerStub:
    """Minimal stand-in for AppRenderBackendOwner: the widget only needs
    _camera_rig and _apply_rig_gesture() to drive navigation. `active` /
    `vulkan_backend` exist because resizeEvent() reads them."""

    def __init__(self, rig):
        self._camera_rig = rig
        self.gestures = 0
        self.active = False          # no live backend in this test
        self.vulkan_backend = None

    def _apply_rig_gesture(self):
        self.gestures += 1


def main() -> int:
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication

    import gui.render_backend as rb

    print("=" * 66)
    print("VULKAN VIEWPORT - PySide6 Qt ENUM / INPUT REGRESSION TEST")
    print("=" * 66, flush=True)

    # 1. The exact expression that used to crash, evaluated directly.
    try:
        _ = Qt.MouseButton.MiddleButton
        check("Qt.MouseButton.MiddleButton resolves", True)
    except Exception as _e:
        check("Qt.MouseButton.MiddleButton resolves", False, repr(_e))

    app_qt = QApplication.instance() or QApplication(sys.argv[:1])

    rig = rb._VulkanCameraRig()
    rig.fit_to_bounds([[0.0, 0.0, 0.0], [100.0, 100.0, 30.0]], aspect=1.6)
    owner = _OwnerStub(rig)
    widget = rb._VulkanSurfaceWidget(None, owner)
    widget.resize(400, 300)
    widget.show()
    QTest.qWaitForWindowExposed(widget, 2000)

    def pose():
        return (rig.target.copy(), rig.azimuth, rig.elevation, rig.distance)

    def drag(button, modifiers, dx, dy):
        """press, drag, release - through the real Qt event path."""
        QTest.mousePress(widget, button, modifiers, QPoint(200, 150))
        QTest.mouseMove(widget, QPoint(200 + dx, 150 + dy), delay=10)
        QTest.mouseRelease(widget, button, modifiers, QPoint(200 + dx, 150 + dy))

    # ---- middle drag: must pan ------------------------------------------
    t0, az0, el0, _ = pose()
    gestures0 = owner.gestures
    try:
        drag(Qt.MouseButton.MiddleButton, Qt.KeyboardModifier.NoModifier, 60, 0)
        moved = not bool((rig.target == t0).all())
        held = (rig.azimuth == az0) and (rig.elevation == el0)
        check("middle press+drag+release: no exception", True,
              f"gestures pushed={owner.gestures - gestures0}")
        check("middle drag pans (target moves, orientation unchanged)", moved and held,
              f"target moved={moved}  az/el held={held}  "
              f"delta={[round(float(v), 3) for v in (rig.target - t0)]}")
    except Exception as _e:
        check("middle press+drag+release: no exception", False, repr(_e))
        traceback.print_exc()

    # ---- left drag: must orbit ------------------------------------------
    t1, az1, el1, _ = pose()
    try:
        drag(Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, 60, 30)
        check("left press+drag+release: no exception", True)
        orbited = (rig.azimuth != az1 or rig.elevation != el1) and bool((rig.target == t1).all())
        check("left drag orbits (azimuth/elevation move, target unchanged)", orbited,
              f"az {az1:.2f}->{rig.azimuth:.2f}  el {el1:.2f}->{rig.elevation:.2f}  "
              f"target held={bool((rig.target == t1).all())}")
    except Exception as _e:
        check("left press+drag+release: no exception", False, repr(_e))
        traceback.print_exc()

    # ---- shift + left drag: must pan (modifier branch) ------------------
    t2, az2, el2, _ = pose()
    try:
        drag(Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ShiftModifier, 60, 0)
        panned = not bool((rig.target == t2).all())
        check("shift+left drag pans (modifier branch alive)",
              panned and rig.azimuth == az2 and rig.elevation == el2,
              f"target moved={panned}  az/el held={rig.azimuth == az2 and rig.elevation == el2}")
    except Exception as _e:
        check("shift+left drag pans (modifier branch alive)", False, repr(_e))
        traceback.print_exc()

    # ---- right press: inert, must not raise -----------------------------
    try:
        QTest.mousePress(widget, Qt.MouseButton.RightButton,
                         Qt.KeyboardModifier.NoModifier, QPoint(200, 150))
        QTest.mouseRelease(widget, Qt.MouseButton.RightButton,
                           Qt.KeyboardModifier.NoModifier, QPoint(200, 150))
        check("right press/release: no exception", True)
    except Exception as _e:
        check("right press/release: no exception", False, repr(_e))
        traceback.print_exc()

    # ---- wheel: must dolly ----------------------------------------------
    # PySide6 6.10 has no QTest.mouseWheel, so post a real QWheelEvent instead.
    d0 = rig.distance
    try:
        from PySide6.QtCore import QPointF
        from PySide6.QtGui import QWheelEvent
        wheel = QWheelEvent(
            QPointF(200.0, 150.0), QPointF(200.0, 150.0),
            QPoint(0, 0), QPoint(0, 120),
            Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
            Qt.ScrollPhase.NoScrollPhase, False)
        QApplication.sendEvent(widget, wheel)
        QTest.qWait(20)
        check("wheel dollies (distance changes)", abs(rig.distance - d0) > 1e-6,
              f"{d0:.3f} -> {rig.distance:.3f}")
    except Exception as _e:
        check("wheel dollies (distance changes)", False, repr(_e))
        traceback.print_exc()

    check("camera rig gestures reached the owner", owner.gestures >= 4,
          f"_apply_rig_gesture calls={owner.gestures}")

    # Live application: real NakshaApp + real Vulkan viewport + real mouse
    # events, i.e. exactly the reported crash path.
    if "--live" in sys.argv:
        print("-" * 66, flush=True)
        try:
            _live_app_check()
        except Exception as _e:
            check("live app boot + interaction", False, repr(_e))
            traceback.print_exc()

    failed = [c for c in _CHECKS if not c[1]]
    print("=" * 66)
    print(f"[qt] {len(_CHECKS) - len(failed)}/{len(_CHECKS)} checks passed", flush=True)
    for name, _ok, detail in failed:
        print(f"[qt]   FAILED: {name} ({detail})", flush=True)
    print("=" * 66, flush=True)
    return 0 if not failed else 1


def _live_app_check() -> bool:
    """Boot the real app with Vulkan, then send real mouse events to the LIVE
    Vulkan viewport widget - the exact path that used to raise
    AttributeError on the first click."""
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication

    import gui.app_window as aw

    ok = True
    win = aw.NakshaApp()
    win.resize(1200, 800)
    win.show()
    for _ in range(30):
        QTest.qWait(100)

    rb = getattr(win, "render_backend", None)
    backend = getattr(rb, "vulkan_backend", None) if rb is not None else None
    check("live app: Vulkan runtime AVAILABLE",
          backend is not None and bool(getattr(rb, "active", False)),
          f"state={getattr(rb, 'state', None)}")
    ok &= backend is not None and bool(getattr(rb, "active", False))
    if backend is not None:
        print(f"[qt]   device={backend.get_device_name()}  api={backend.get_api_version_string()}",
              flush=True)
        check("live app: device + API reported",
              bool(backend.get_device_name()) and bool(backend.get_api_version_string()),
              f"{backend.get_device_name()} / {backend.get_api_version_string()}")

    viewport = getattr(rb, "vulkan_widget", None) if rb is not None else None
    check("live app: Vulkan viewport widget installed", viewport is not None,
          "the native surface is the main viewport")
    if viewport is None:
        return False

    errors = []
    for label, button, mods, dx, dy in (
        ("middle", Qt.MouseButton.MiddleButton, Qt.KeyboardModifier.NoModifier, 50, 0),
        ("left", Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, 40, 25),
        ("shift+left", Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ShiftModifier, 40, 0),
        ("right", Qt.MouseButton.RightButton, Qt.KeyboardModifier.NoModifier, 0, 0),
    ):
        try:
            QTest.mouseMove(viewport, QPoint(600, 400))
            QTest.mousePress(viewport, button, mods, QPoint(600, 400))
            QTest.mouseMove(viewport, QPoint(600 + dx, 400 + dy), delay=10)
            QTest.mouseRelease(viewport, button, mods, QPoint(600 + dx, 400 + dy))
            QTest.qWait(60)
            check(f"live app: {label} mouse on Vulkan viewport: no crash", True)
        except Exception as _e:
            errors.append(f"{label}: {_e!r}")
            check(f"live app: {label} mouse on Vulkan viewport: no crash", False, repr(_e))
            ok = False

    # a click storm across every button, which is what surfaced the crash
    try:
        for _ in range(25):
            for b in (Qt.MouseButton.LeftButton, Qt.MouseButton.MiddleButton,
                      Qt.MouseButton.RightButton):
                QTest.mousePress(viewport, b, Qt.KeyboardModifier.NoModifier, QPoint(600, 400))
                QTest.mouseMove(viewport, QPoint(605, 405), delay=1)
                QTest.mouseRelease(viewport, b, Qt.KeyboardModifier.NoModifier, QPoint(605, 405))
        QTest.qWait(100)
        check("live app: 75-event click storm: no crash", True, "25 x 3 buttons")
    except Exception as _e:
        check("live app: 75-event click storm: no crash", False, repr(_e))
        ok = False

    try:
        win.close()
    except Exception:
        pass
    return ok and not errors



if __name__ == "__main__":
    try:
        _CODE = main()
    except BaseException:
        traceback.print_exc()
        _CODE = 3
    finally:
        sys.stdout.flush()
    raise SystemExit(_CODE)
