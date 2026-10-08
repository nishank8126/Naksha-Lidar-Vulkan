"""
Fast unit test for the NAKSHA_VULKAN_DISABLE_ALL_VTK_RENDER kill-switch.

No dataset and no real GPU work: this exercises the guard in
GPURenderManager._execute_render with a stub widget, so it answers
"does the switch actually stop a VTK draw, and does it stay out of
the way when Vulkan does not own the viewport?" in seconds.
"""
import os
import sys
import time

# The app prints emoji to stdout; the default Windows console codec (cp1252)
# cannot encode them and the print() would abort the run.
for _s in ("stdout", "stderr"):
    try:
        sys.__dict__[_s].reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

os.environ.setdefault("NAKSHA_VULKAN_MAIN_VIEWPORT", "1")
# Assign (not setdefault): this suite is specifically about the switch being
# ON, so an inherited value from the shell must not silently change the
# subject under test. The "flag off" case is exercised explicitly further down.
os.environ["NAKSHA_VULKAN_DISABLE_ALL_VTK_RENDER"] = "1"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PySide6.QtWidgets import QApplication          # noqa: E402
from PySide6.QtCore import QEventLoop, QTimer, QObject  # noqa: E402
from gui.gpu_render_manager import GPURenderManager   # noqa: E402

_RESULTS = []


def check(name, ok, detail=""):
    _RESULTS.append((name, ok))
    print(f"[ks] {'PASS' if ok else 'FAIL'}  {name}"
          + (f"  ({detail})" if detail else ""), flush=True)


class FakeTimer:
    def __init__(self):
        self.stopped = 0
        self.started = 0

    def stop(self):
        self.stopped += 1

    def start(self):
        self.started += 1


class FakeInteractor:
    def __init__(self):
        self.render_off = 0
        self.render_on = 0

    def EnableRenderOff(self):
        self.render_off += 1

    def EnableRenderOn(self):
        self.render_on += 1


class FakeVulkanWidget:
    def __init__(self, visible=True):
        self._visible = visible

    def isVisible(self):
        return self._visible


class FakeBackend:
    def __init__(self, active=True, visible=True):
        self.active = active
        self.vulkan_widget = FakeVulkanWidget(visible) if active else None


class FakeRenderWindow:
    def __init__(self, interactor):
        self._interactor = interactor
        self.render_calls = 0

    def GetInteractor(self):
        return self._interactor

    def Render(self):
        self.render_calls += 1


class FakeVTKWidget:
    """Stub that satisfies the real _is_widget_renderable() predicate:

    isVisible() -> True, GetRenderWindow() -> a window with an interactor.
    Without these the manager bails out before reaching the kill-switch.
    """

    def __init__(self):
        self.render_timer = FakeTimer()
        self.interactor = FakeInteractor()
        self.render_calls = 0
        self._rw = FakeRenderWindow(self.interactor)

    def isVisible(self):
        return True

    def GetRenderWindow(self):
        return self._rw

    def render(self):
        self.render_calls += 1


class FakeApp(QObject):
    # Must be a real QObject: GPURenderManager.__init__ calls
    # super().__init__(app) so Qt owns the manager's lifetime.
    def __init__(self, active=True, visible=True):
        super().__init__()
        self.render_backend = FakeBackend(active, visible)
        self.vtk_widget = FakeVTKWidget()
        self._shutdown_in_progress = False


def _draws(app):
    """Actual VTK draws issued through the manager.

    Unwrapped widgets go through the GetRenderWindow().Render() fallback,
    wrapped widgets through widget.render(); count both so the assertions
    do not depend on which path was taken.
    """
    w = app.vtk_widget
    return int(w.render_calls) + int(w._rw.render_calls)


def _pump(qapp, seconds):
    loop = QEventLoop()
    QTimer.singleShot(int(seconds * 1000), loop.quit)
    loop.exec()


def main():
    qapp = QApplication.instance() or QApplication(sys.argv[:1])

    check("env flag is read",
          GPURenderManager.vtk_render_killswitch_requested() is True,
          "NAKSHA_VULKAN_DISABLE_ALL_VTK_RENDER=1")

    # --- Vulkan owns the viewport: draws must be blocked --------------------
    app = FakeApp(active=True, visible=True)
    grm = GPURenderManager(app)

    check("kill-switch armed on init", grm._all_vtk_render_disabled is True,
          f"_all_vtk_render_disabled={grm._all_vtk_render_disabled}")
    check("render timer stopped", app.vtk_widget.render_timer.stopped >= 1,
          f"stop() x{app.vtk_widget.render_timer.stopped}")
    check("interactor auto-render off",
          app.vtk_widget.interactor.render_off >= 1
          and app.vtk_widget.interactor.render_on == 0,
          f"off x{app.vtk_widget.interactor.render_off} "
          f"on x{app.vtk_widget.interactor.render_on}")
    check("Vulkan detected as viewport owner",
          grm.vulkan_owns_viewport() is True)

    # Drive real render requests through the throttle -> debounce -> execute path.
    for _ in range(3):
        grm.request_render(app.vtk_widget)
        _pump(qapp, 0.35)
    _pump(qapp, 0.6)

    check("VTK draw NEVER executed while Vulkan owns the viewport",
          _draws(app) == 0 and grm.render_count == 0,
          f"draws={_draws(app)} grm.render_count={grm.render_count}")
    check("blocked draws were counted",
          grm._vtk_render_calls_blocked > 0,
          f"blocked={grm._vtk_render_calls_blocked}")

    rep = grm.opengl_ownership_report()
    # The report prints one value per line (key, then value), so assert on
    # the value following the key rather than on a same-line concatenation.
    def _after(rep, key):
        lines = [ln.strip() for ln in rep.splitlines()]
        for i, ln in enumerate(lines):
            if ln == key and i + 1 < len(lines):
                return lines[i + 1]
        return None

    check("VTK main render reads OFF when the kill-switch is armed",
          _after(rep, "VTK main render:") == "OFF (suppressed)",
          f"got {_after(rep, 'VTK main render:')!r}")
    check("interactor auto-render reads OFF when the switch is armed",
          _after(rep, "Interactor auto-render:") == "OFF",
          f"got {_after(rep, 'Interactor auto-render:')!r}")
    check("ownership report mentions the kill-switch",
          "OPENGL OWNERSHIP" in rep and _after(rep, "Kill-switch requested:") == "True",
          rep.replace("\n", " | ")[:160])

    # --- Vulkan NOT the viewport: the switch must stay out of the way ------
    app2 = FakeApp(active=True, visible=False)
    grm2 = GPURenderManager(app2)
    check("kill-switch still armed (env-driven)",
          grm2._all_vtk_render_disabled is True)
    check("Vulkan NOT viewport owner -> Vulkan not counted as owner",
          grm2.vulkan_owns_viewport() is False)
    for _ in range(3):
        grm2.request_render(app2.vtk_widget)
        _pump(qapp, 0.35)
    _pump(qapp, 0.6)
    check("VTK still renders when Vulkan is not the visible viewport",
          _draws(app2) > 0,
          f"draws={_draws(app2)} grm2.render_count={grm2.render_count}")

    # --- Flag off: VTK must behave exactly as before ------------------------
    os.environ["NAKSHA_VULKAN_DISABLE_ALL_VTK_RENDER"] = "0"
    try:
        app3 = FakeApp(active=True, visible=True)
        grm3 = GPURenderManager(app3)
        check("flag off -> kill-switch disarmed",
              grm3._all_vtk_render_disabled is False)
        for _ in range(3):
            grm3.request_render(app3.vtk_widget)
            _pump(qapp, 0.35)
        _pump(qapp, 0.6)
        check("VTK renders normally with the flag off",
              _draws(app3) > 0,
              f"draws={_draws(app3)}")
    finally:
        os.environ["NAKSHA_VULKAN_DISABLE_ALL_VTK_RENDER"] = "1"

    # --- Ownership report: print it in full so the exact ON/OFF lines that
    # --- drive the component bisect are visible in the test output.
    app4 = FakeApp(active=True, visible=True)
    grm4 = GPURenderManager(app4)
    print("\n--- full report (kill-switch ON, Vulkan owns viewport) ---", flush=True)
    grm4.print_opengl_ownership_report()

    failed = [n for n, ok in _RESULTS if not ok]
    print(f"\nFinal: {'PASS' if not failed else 'FAIL'}"
          + ("" if not failed else "  failed: " + "; ".join(failed)), flush=True)
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
