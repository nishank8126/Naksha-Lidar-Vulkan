"""Real GUI button/cache acceptance; separate process for each cold/warm run."""
import os
import sys
import json
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
os.environ["NAKSHA_RENDER_BACKEND"] = "vulkan"
os.environ["NAKSHA_VULKAN_MAIN_VIEWPORT"] = "1"
os.environ["NAKSHA_VULKAN_PREVIEW"] = ""
os.environ["NAKSHA_LATENCY_PROBE"] = "1"
for stream in (sys.stdout, sys.stderr):
    stream.reconfigure(encoding="utf-8", errors="replace")

from PySide6.QtCore import QSettings, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QAbstractButton
from PIL import Image
from gui.app_window import NakshaApp
from vulkan_engine_test import _suppress_modal_dialogs, pump, wait_until
from gui.naksha_cache import normal_builder, surface_streaming


def main():
    paths = json.loads((ROOT / "diagnostics/multi_source_gui/sources.json").read_text())
    tag = sys.argv[1] if len(sys.argv) > 1 else "cold"
    out = ROOT / "diagnostics/multi_source_gui" / tag
    out.mkdir(parents=True, exist_ok=True)
    QSettings.setDefaultFormat(QSettings.IniFormat)
    QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, str(out / "settings"))
    app = QApplication.instance() or QApplication(sys.argv[:1])
    _suppress_modal_dialogs()
    calls = {"normal_generation": 0, "triangulation": 0}
    normal_function = normal_builder.tile_normals
    surface_function = surface_streaming.triangulate_identity_block
    def normal(*args, **kwargs):
        calls["normal_generation"] += 1
        return normal_function(*args, **kwargs)
    def surface(*args, **kwargs):
        calls["triangulation"] += 1
        return surface_function(*args, **kwargs)
    normal_builder.tile_normals = normal
    surface_streaming.triangulate_identity_block = surface
    window = NakshaApp()
    window.resize(1400, 900)
    window.show()
    pump(app, 1)
    window.open_file(filenames=paths, prompt_import=False)
    if not wait_until(app, lambda: getattr(window, "naksha_stream", None) is not None, timeout=120):
        raise RuntimeError("multi-source GUI did not install StreamManager")
    manager = window.naksha_stream
    backend = window.render_backend.vulkan_backend
    pump(app, 2)
    window.ribbon_manager.show_ribbon("view")
    pump(app, .3)
    results = []
    for label, canonical in (("Class", "class"), ("Shading", "shaded"), ("Surface", "surface")):
        buttons = [b for b in window.findChildren(QAbstractButton)
                   if b.property("ribbonText") == label and b.property("ribbonSection") == "Display"]
        if not buttons:
            raise RuntimeError(f"actual {label} GUI button not found")
        button = buttons[0]
        before = dict(calls)
        before_xyz = int(getattr(manager.adapter, "arena_xyz_bytes", 0))
        started = time.perf_counter()
        if not button.isVisible():
            raise RuntimeError(f"{label} button is hidden")
        QTest.mouseClick(button, Qt.LeftButton)
        active = wait_until(app, lambda: manager.display_mode == canonical
            and getattr(manager, "_pending_display_mode", None) is None, timeout=120)
        elapsed = (time.perf_counter() - started) * 1000
        if not active:
            raise RuntimeError(f"{label} failed: {getattr(manager, 'surface_error', '')}; "
                               f"normal={getattr(manager, 'normal_build_status', None)}")
        pump(app, .5)
        frame = backend.capture_frame()
        if frame is None:
            raise RuntimeError("Vulkan returned no pixels")
        Image.fromarray(frame).save(out / f"{label}.png")
        proof = backend.get_surface_draw_proof()
        if canonical == "surface" and not proof.get("indexed_triangles_drawn"):
            raise RuntimeError(f"Surface lacks filled indexed triangle proof: {proof}")
        result = dict(button=label, actual_button_clicked=True,
            mode=manager.display_mode, activation_ms=elapsed,
            normal_generation=calls["normal_generation"]-before["normal_generation"],
            triangulation=calls["triangulation"]-before["triangulation"],
            xyz_upload_bytes=int(getattr(manager.adapter, "arena_xyz_bytes", 0))-before_xyz,
            native_draw_proof=proof, screenshot=str(out / f"{label}.png"))
        results.append(result)
        print("[GUI ACCEPTANCE]", json.dumps(result), flush=True)
    # Wait for publication: visible-first activation may precede completion.
    from gui.naksha_cache.normal_streaming import open_normal_cache
    def normal_published():
        reader, report = open_normal_cache(str(manager.idx.path), verify_source=False)
        return report.status == "HIT"
    if not wait_until(app, normal_published, timeout=120):
        raise RuntimeError("NORMAL_DATA was not committed")
    report = dict(tag=tag, source_count=len(manager.idx.sources),
        total_points=manager.idx.total_points, one_stream_manager=True,
        file=str(manager.idx.path), runtime_decode=manager.reader.stats if hasattr(manager, "reader") else {},
        results=results, calls=calls, app_data_arrays=len(window.data),
        tick_errors=int(getattr(manager, "frame_tick_error_count", 0)))
    if tag == "warm" and (calls["normal_generation"] or calls["triangulation"]):
        raise RuntimeError("cache HIT recomputed derived data")
    (out / "acceptance.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print("[SINGLE FILE GUI PASS]", json.dumps(report), flush=True)
    manager.close()
    os._exit(0)


if __name__ == "__main__":
    main()
