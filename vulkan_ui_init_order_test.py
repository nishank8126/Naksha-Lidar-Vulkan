"""
[NAKSHA UI INIT ORDER] diagnostic.

Answers one question: is the Naksha chrome (ribbon / top bar / status bar)
actually built BEFORE the Vulkan main viewport is installed, and does the
viewer host end up smaller than the main window?

Prints the widget tree at three points: after __init__, after show(), and
after the Vulkan install settles.
"""
import os
import sys

os.environ.setdefault("NAKSHA_VULKAN_MAIN_VIEWPORT", "1")
os.environ.setdefault("NAKSHA_RENDER_BACKEND", "vulkan")
os.environ["NAKSHA_VULKAN_DISABLE_ALL_VTK_RENDER"] = "1"

for _k in ("stdout", "stderr"):
    try:
        sys.__dict__[_k].reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from PySide6.QtWidgets import QApplication          # noqa: E402
from PySide6.QtCore import QEventLoop, QTimer        # noqa: E402


def _pump(q, sec):
    loop = QEventLoop()
    QTimer.singleShot(int(sec * 1000), loop.quit)
    loop.exec()


def _geo(w):
    try:
        return f"{w.width()}x{w.height()}" if w is not None else "None"
    except Exception:
        return "?"


def _yn(app, name):
    v = getattr(app, name, None)
    return f"{_geo(v):>9}  ({'YES' if v is not None else 'NO'})"


def _report(app, label):
    rb = getattr(app, "render_backend", None)
    cw = app.centralWidget() if hasattr(app, "centralWidget") else None
    print(f"\n--- {label} ---", flush=True)
    print(f"  Main window      : {_geo(app)}", flush=True)
    print(f"  CentralWidget    : {type(cw).__name__ if cw is not None else None}",
          flush=True)
    print(f"  Ribbon           : {_yn(app, 'ribbon_container')}", flush=True)
    print(f"  Toolbar (top_bar): {_yn(app, 'top_bar')}", flush=True)
    print(f"  Status bar       : {_yn(app, 'status')}", flush=True)
    print(f"  Splitter         : {_geo(getattr(app, 'splitter', None))}", flush=True)
    print(f"  Viewer frame     : {_geo(getattr(app, 'frame', None))}", flush=True)
    try:
        host = rb.vulkan_viewer_host() if rb is not None else None
        print(f"  Viewer host      : {_geo(host)}", flush=True)
    except Exception as e:
        print(f"  Viewer host      : ERR {e!r}", flush=True)
    print(f"  Vulkan installed : "
          f"{getattr(rb, '_viewport_installed', None)}", flush=True)
    print(f"  Vulkan widget    : "
          f"{_geo(getattr(rb, 'vulkan_widget', None)) if rb is not None else 'None'}",
          flush=True)


def main():
    q = QApplication.instance() or QApplication(sys.argv[:1])
    from gui.app_window import NakshaApp

    print("=== constructing NakshaApp ===", flush=True)
    app = NakshaApp()
    print("=== __init__ returned ===", flush=True)
    _report(app, "after __init__ (window not shown yet)")

    app.resize(1400, 900)
    app.show()
    _pump(q, 1.5)
    _report(app, "after show() + event pump")

    # Let the readiness poll run to completion.
    _pump(q, 4.0)
    _report(app, "after Vulkan install poll settles")

    rb = getattr(app, "render_backend", None)
    host = rb.vulkan_viewer_host() if rb is not None else None
    try:
        smaller = host is not None and (
            int(host.height()) < int(app.height())
            or int(host.width()) < int(app.width()))
    except Exception:
        smaller = False
    print(f"\nVIEWER SMALLER THAN WINDOW: {smaller}", flush=True)
    print(f"RIBBON EXISTS: "
          f"{getattr(app, 'ribbon_container', None) is not None}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
