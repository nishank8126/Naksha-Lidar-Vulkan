"""Live PIXEL-level coverage oracle. Real NakshaApp, real Qt events, then for
every settled step compare, per 10 m cell fully inside the view:
  SOURCE (true LAS points) / SELECTED / ACTIVE / SUBMITTED / RENDERED (pixels)."""
import os, sys, time, math
os.environ["NAKSHA_RENDER_BACKEND"] = "vulkan"
os.environ["NAKSHA_VULKAN_MAIN_VIEWPORT"] = "1"
os.environ["NAKSHA_VULKAN_PREVIEW"] = ""      # vulkan_engine_test would default it to the split preview
ROOT = r"H:\naksha-lidar 2"
DIAG = os.path.join(ROOT, "diagnostics")
DATA = os.path.join(ROOT, "test_classified_highprecision.laz")
TAG = os.environ.get("DIAG_TAG", "run")
sys.path.insert(0, ROOT); os.chdir(ROOT)
for n in ("stdout", "stderr"):
    try: getattr(sys, n).reconfigure(encoding="utf-8", errors="replace")
    except Exception: pass

import numpy as np
from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from gui.app_window import NakshaApp
from vulkan_engine_test import _suppress_modal_dialogs, pump, wait_until
from gui.naksha_cache import stream_manager as SM
from gui.naksha_cache.visible_parity import (cells_in_bounds, cells_for_boxes,
                                             largest_gap_m, read_camera_sample)
from gui.naksha_cache.app_streaming import _viewport

CELL = 10.0
M = "@@"
import json
RESULTS = []
STAGE = os.environ.get("MIG_STAGE", "0")


from PySide6.QtCore import QObject, QEvent
_INJ = {"wheel": 0}
_SEEN = {"wheel": 0, "mouse_move": 0}


class _InputWatch(QObject):
    """Counts wheel events reaching the app so foreign (real-mouse) input is detected."""
    def eventFilter(self, obj, ev):
        t = ev.type()
        if t == QEvent.Wheel:
            _SEEN["wheel"] += 1
        return False


def contaminated():
    return _SEEN["wheel"] != _INJ["wheel"]


def mgr_of(win): return getattr(win, "naksha_stream", None)


def settle(win, secs=6.0):
    t0 = time.time(); pump(app_qt, 1.0)
    while time.time() - t0 < secs:
        if len(getattr(mgr_of(win), "_pending", {}) or {}) == 0: break
        pump(app_qt, 0.3)
    pump(app_qt, 1.5)


def wheel(rb, delta):
    _INJ["wheel"] += 1
    vw = rb.vulkan_widget
    local = QPoint(vw.width() // 2, vw.height() // 2); glob = vw.mapToGlobal(local)
    ev = QWheelEvent(QPointF(local), QPointF(glob), QPoint(0, 0), QPoint(0, delta),
                     Qt.NoButton, Qt.NoModifier, Qt.NoScrollPhase, False)
    QApplication.sendEvent(QApplication.widgetAt(glob) or vw, ev)


def drag(rb, dx, dy):
    vw = rb.vulkan_widget; p0 = QPoint(vw.width() // 2, vw.height() // 2)
    QTest.mousePress(vw, Qt.MiddleButton, Qt.NoModifier, p0)
    for i in range(1, 21):
        QTest.mouseMove(vw, QPoint(p0.x() + dx * i // 20, p0.y() + dy * i // 20), delay=5)
    QTest.mouseRelease(vw, Qt.MiddleButton, Qt.NoModifier, QPoint(p0.x() + dx, p0.y() + dy))



from pathlib import Path
from PIL import Image
import accept_visual_lod_continuity as continuity
from gui.naksha_cache import visual_quality

out = Path(ROOT) / "diagnostics" / "continuity_live"
out.mkdir(parents=True, exist_ok=True)
app_qt = QApplication.instance() or QApplication(sys.argv[:1])
_suppress_modal_dialogs()
win = NakshaApp()
win.resize(1400, 900)
win.show()
pump(app_qt, 1.5)
win.open_file(filenames=[DATA], prompt_import=False)
wait_until(app_qt, lambda: mgr_of(win) is not None, timeout=120.0)
settle(win, 8.0)
rb = win.render_backend
frames = []
steps = [("fit", lambda: win.fit_view_with_2d_lock()),
         ("zoom_in", lambda: wheel(rb, 120)),
         ("slow_pan", lambda: drag(rb, 120, 0)),
         ("rapid_pan", lambda: drag(rb, -480, 0)),
         ("zoom_out", lambda: wheel(rb, -120)),
         ("alternating", lambda: wheel(rb, 120)),
         ("alternating_pan", lambda: drag(rb, 240, 100))]
for name, action in steps:
    action()
    for phase, delay in [("moving", .08), ("settled", 1.0)]:
        pump(app_qt, delay)
        manager = mgr_of(win)
        camera, viewport = _viewport(win)
        width, height = rb.vulkan_widget.width(), rb.vulkan_widget.height()
        records = continuity.frontier_records(manager, viewport, width, height)
        seams = continuity.seam_metrics(records, continuity.adjacency(records))
        quality = visual_quality.quality_map(manager, viewport, width, height)
        frame = rb.vulkan_backend.capture_frame()
        if frame is None:
            raise RuntimeError("Native Vulkan capture returned no pixels")
        Image.fromarray(frame).save(out / f"{name}_{phase}.png")
        report = dict(step=name, phase=phase, seams=seams,
            starved_cells=int(quality["starved_cells"]),
            coverage_holes=int(quality["empty_cells"]),
            duplicate_cells=int(quality["duplicate_cover_cells"]),
            seam_candidates=int(quality["visible_tile_seam_count"]),
            frontier=dict(manager.last_screen_space_diag),
            actual_native_pixels=True)
        frames.append(report)
        print(json.dumps(report), flush=True)
(out / "acceptance.json").write_text(json.dumps(frames, indent=2), encoding="utf-8")
print("LIVE CAPTURES COMPLETE; visual inspection required", flush=True)
os._exit(0)
