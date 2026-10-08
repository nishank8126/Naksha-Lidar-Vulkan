"""Regression: loading overlay lifecycle (EMPTY/LOADING/POINTS_READY/READY).

The overlay is a CHILD widget of the main window, so a parent activation can
re-raise it unless it has been explicitly detached. This exercises the state
machine and asserts the overlay can never be shown once loading has finished.
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QWidget

from gui.progress_dialog import LoadingProgressDialog


def main():
    QApplication.instance() or QApplication([])
    fails = 0
    parent = QWidget()

    # ---- 1. the widget refuses to show once loading is done -------------
    dlg = LoadingProgressDialog(parent)
    dlg.show()
    dlg.mark_loading_done()
    dlg.setParent(None)
    dlg.hide()
    dlg.show()                      # must be a no-op
    reappeared = dlg.isVisible()
    ok = not reappeared
    print(f"[{'PASS' if ok else 'FAIL'}] show() after mark_loading_done() "
          f"re-shows: {reappeared}")
    if not ok:
        fails += 1

    # ---- 2. the state table maps stages to the right data state -------
    from gui.app_window import NakshaApp

    class Fake:
        _set_loading_state = NakshaApp._set_loading_state
        _teardown_loading_overlay = NakshaApp._teardown_loading_overlay
        _vulkan_loading_state = "IDLE"
        _data_state = "EMPTY"
        _load_progress = None
        _load_timeline = None

        def setWindowTitle(self, *_):
            pass

    f = Fake()
    for stage, expect in (
        ("POINT CLOUD LOADING", "LOADING"),
        ("PTC APPLYING", "LOADING"),
        ("VULKAN INITIALIZING", "LOADING"),
        ("POINTS_READY", "POINTS_READY"),
        ("READY", "READY"),
    ):
        try:
            f._set_loading_state(stage)
        except Exception as exc:
            print(f"[FAIL] {stage}: raised {exc!r}")
            fails += 1
            continue
        got = f._data_state
        ok = got == expect
        print(f"[{'PASS' if ok else 'FAIL'}] {stage:<22} -> {got} (expect {expect})")
        if not ok:
            fails += 1

    ok = f._data_state == "READY"
    print(f"[{'PASS' if ok else 'FAIL'}] final state is READY: {f._data_state}")
    if not ok:
        fails += 1

    print("LOAD UI STATE MACHINE OK" if not fails else f"{fails} FAILURE(S)")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
