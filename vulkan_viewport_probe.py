"""vulkan_viewport_probe.py - one-shot diagnostic for the NAKSHA VULKAN main
viewport: is the REAL on-screen viewport black, and why?

Reports, for the loaded 123.las scene:

  * native counters: point count, position uploads, draw calls, visibility
  * the last MVP matrix handed to the shader (a zero/garbage MVP renders
    nothing no matter how correct the swapchain is)
  * nkv_capture_frame offscreen readback metrics  (engine truth)
  * a Pillow ImageGrab of the widget rect with the window forced to the
    foreground (screen truth) - the previous run grabbed the wrong pixels
    because this process was not the foreground process
  * validation-layer error/warning counts from stderr

Usage:
    set NAKSHA_VULKAN_VALIDATION=1
    venv\Scripts\python.exe vulkan_viewport_probe.py [file.las]

Exit code 0 = viewport shows real content, 1 = it does not.
"""
from __future__ import annotations

import ctypes
import os
import sys
import time

os.environ["NAKSHA_RENDER_BACKEND"] = "vulkan"
os.environ["NAKSHA_VULKAN_MAIN_VIEWPORT"] = "1"
os.environ["NAKSHA_VULKAN_VALIDATION"] = os.environ.get("NAKSHA_VULKAN_VALIDATION", "1")
os.environ.pop("NAKSHA_VULKAN_PREVIEW", None)

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_ROOT)

import vulkan_resize_test as vt  # noqa: E402  (env must be set first)
from vulkan_resize_test import (  # noqa: E402
    _suppress_modal_dialogs,
    content_metrics,
    engine_capture,
    grab_desktop,
    pump,
)

ERR_LOG = os.path.join(PROJECT_ROOT, "probe_err.log")
OUT_DIR = os.path.join(PROJECT_ROOT, "vulkan_resize_output")
LAS_PATH = sys.argv[1] if len(sys.argv) > 1 else os.path.join(PROJECT_ROOT, "123.las")

HWND_TOPMOST = -1
HWND_NOTOPMOST = -2
SWP_NOMOVE = 0x0002
SWP_NOSIZE = 0x0001
SWP_SHOWWINDOW = 0x0040

_failures = []


def note(ok: bool, name: str, detail: str = "") -> bool:
    tag = "PASS" if ok else "FAIL"
    print(f"[probe] {tag}  {name}" + (f"  ({detail})" if detail else ""), flush=True)
    if not ok:
        _failures.append(name)
    return ok


def mvp_lines(m) -> list[str]:
    rows = []
    for r in range(4):
        rows.append("      " + " ".join(f"{m[r * 4 + c]:+9.4f}" for c in range(4)))
    return rows


def validation_summary() -> dict:
    out = {"errors": 0, "warnings": 0, "error_lines": [], "warn_lines": [], "exists": False}
    if not os.path.isfile(ERR_LOG):
        return out
    out["exists"] = True
    try:
        with open(ERR_LOG, "r", encoding="utf-8", errors="replace") as fh:
            for raw in fh:
                s = raw.strip()
                if s.startswith("[Vulkan Validation][ERROR]"):
                    out["errors"] += 1
                    out["error_lines"].append(s)
                elif s.startswith("[Vulkan Validation][WARN]"):
                    out["warnings"] += 1
                    out["warn_lines"].append(s)
                elif s.startswith("[Vulkan Validation]"):
                    out["errors"] += 1
                    out["error_lines"].append(s)
    except Exception as exc:
        out["error_lines"] = [f"<could not read log: {exc}>"]
    return out


def main() -> int:
    from PySide6.QtWidgets import QApplication

    from gui.app_window import NakshaApp
    from gui.dialogs.load_pointcloud_dialog import DEFAULT_IMPORT_OPTIONS

    if not os.path.isfile(LAS_PATH):
        print(f"[probe] LAS not found: {LAS_PATH}", flush=True)
        return 1

    _suppress_modal_dialogs()
    app = QApplication.instance() or QApplication(sys.argv[:1])

    print("[probe] constructing NakshaApp ...", flush=True)
    win = NakshaApp()
    win.show()
    win.raise_()
    win.activateWindow()
    pump(app, 1.5)

    rb = win.render_backend
    vb = getattr(rb, "vulkan_backend", None)
    if vb is None or not rb.active:
        print(f"[probe] backend not active: state={rb.state} err={rb.last_error!r}", flush=True)
        return 1
    note(True, "vulkan backend active", f"state={rb.state}")

    t0 = time.monotonic()
    win.open_file(filenames=[LAS_PATH], import_options=DEFAULT_IMPORT_OPTIONS,
                  prompt_import=False)
    dll, handle = vb._dll, ctypes.c_uint64(vb._handle)
    pump(app, 1.0)
    while time.monotonic() - t0 < 30.0:
        pump(app, 0.5)
        if int(dll.nkv_get_point_count(handle)) > 0:
            break
    pump(app, 3.0)

    # ---- native counters --------------------------------------------------
    point_count = int(dll.nkv_get_point_count(handle))
    pos_uploads = int(dll.nkv_get_point_position_upload_count(handle))
    draw_calls = int(dll.nkv_get_point_draw_call_count(handle))
    visible = int(dll.nkv_get_point_cloud_visible(handle))
    stats = vb.get_frame_stats()
    note(point_count > 0, "point cloud uploaded to engine",
         f"points={point_count:,} pos_uploads={pos_uploads} draws={draw_calls} "
         f"visible={visible} stats(rendered,skipped,recreates)={stats}")

    # ---- last MVP ---------------------------------------------------------
    mvp = (ctypes.c_float * 16)()
    ok_mvp = int(dll.nkv_get_last_mvp(handle, mvp)) == 1
    finite = ok_mvp and all(abs(mvp[i]) < 1e9 for i in range(16)) and \
        any(abs(mvp[i]) > 1e-6 for i in range(16))
    print("[probe] last MVP:", flush=True)
    for line in mvp_lines(mvp):
        print(line, flush=True)
    note(finite, "last MVP is non-degenerate", f"ok={ok_mvp}")

    # ---- camera state: VTK vs shared rig vs what the engine actually holds
    try:
        print(rb.describe_camera("probe"), flush=True)
    except Exception as exc:
        print(f"[probe] describe_camera failed: {exc}", flush=True)

    # ---- screen grabs are only believed when they can be self-validated ---
    widget = rb.vulkan_widget
    hwnd = int(win.winId())
    u32 = ctypes.windll.user32

    def to_foreground() -> bool:
        win.raise_()
        win.activateWindow()
        u32.SetWindowPos(hwnd, ctypes.c_void_p(HWND_TOPMOST), 0, 0, 0, 0,
                         SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)
        u32.SetForegroundWindow(hwnd)
        pump(app, 0.8)
        return int(u32.GetForegroundWindow()) == hwnd

    fg_ok = to_foreground() or to_foreground()
    print(f"[probe] foreground={fg_ok} "
          f"(fg=0x{int(u32.GetForegroundWindow()):x} hwnd=0x{hwnd:x})", flush=True)
    print(f"[probe] widget size={widget.width()}x{widget.height()} "
          f"extent={vb._last_extent}", flush=True)

    # ---- render matrix: isolate WHERE the black frame comes from ----------
    os.makedirs(OUT_DIR, exist_ok=True)

    def shot(tag: str) -> dict:
        try:
            vb.request_render()
        except Exception:
            dll.nkv_render(handle)
        pump(app, 0.6)
        arr = vb.capture_frame()
        m = content_metrics(arr[..., :3]) if arr is not None else {}
        draws = int(dll.nkv_get_point_draw_call_count(handle))
        print(f"[probe] shot {tag}: draws={draws} {m}", flush=True)
        if arr is not None:
            from PySide6.QtGui import QImage
            qimg = QImage(arr.data, arr.shape[1], arr.shape[0],
                          arr.shape[1] * 4, QImage.Format.Format_RGBA8888).copy()
            qimg.save(os.path.join(OUT_DIR, f"probe_{tag}.png"))
        return m

    def screen(tag: str) -> dict:
        _arr, m = grab_desktop(app, widget, os.path.join(OUT_DIR, f"probe_{tag}.png"))
        print(f"[probe] screen {tag}: {m}", flush=True)
        return m

    def dump_mvp(tag: str) -> None:
        m = (ctypes.c_float * 16)()
        dll.nkv_get_last_mvp(handle, m)
        ok, scale = vb.get_camera_projection()
        print(f"[probe] MVP {tag} (engine ortho={ok} scale={scale}):", flush=True)
        for line in mvp_lines(m):
            print(line, flush=True)

    # (1) self-validate the screen grab: a blue clear colour must come back blue
    vb.set_clear_color(0.0, 0.0, 0.30, 1.0)
    blue = shot("clear_blue")
    note(blue.get("bg", [0, 0, 0])[2] > 60,
         "engine capture carries the clear colour (capture path live)",
         f"bg={blue.get('bg')}")
    blue_screen = screen("screen_blue")
    trusted = bool(blue_screen) and blue_screen.get("bg", [0, 0, 0])[2] > 60
    note(trusted, "screen grab is trustworthy (blue clear colour came back blue)",
         str(blue_screen))

    # (2) black clear + real scene: what the user actually sees
    vb.set_clear_color(0.0, 0.0, 0.0, 1.0)
    base = shot("baseline")
    dump_mvp("after baseline render")
    note(base.get("std", 0.0) > 2.0 or base.get("non_bg_pct", 0.0) > 0.5,
         "engine offscreen readback has content", str(base))
    black_screen = screen("screen_black")
    if trusted:
        note(black_screen.get("std", 0.0) > 2.0 or
             black_screen.get("non_bg_pct", 0.0) > 0.5,
             "screen pixels over the viewport have content", str(black_screen))
    else:
        print("[probe] SKIP   screen content check (grab not trusted this run)",
              flush=True)

    # (3) colour pipelines: is the point cloud drawn but pure black?
    for mode, tag in ((3, "mode_elevation"), (1, "mode_class"),
                      (2, "mode_intensity"), (0, "mode_rgb")):
        vb.set_display_mode(mode)
        m = shot(tag)
        note(m.get("std", 0.0) > 2.0 or m.get("non_bg_pct", 0.0) > 0.5,
             f"display mode {mode} ({tag}) renders points", str(m))
    vb.set_point_size(6.0)
    big = shot("pointsize6")
    note(big.get("std", 0.0) > 2.0 or big.get("non_bg_pct", 0.0) > 0.5,
         "6 px point size renders points", str(big))

    # ---- validation -------------------------------------------------------
    v = validation_summary()
    note(v["exists"], "validation stderr captured", f"file={ERR_LOG}")
    note(v["errors"] == 0, "validation ERROR count = 0",
         f"errors={v['errors']} warnings={v['warnings']}")
    for line in v["error_lines"][:20]:
        print(f"[probe]   VAL-ERR: {line}", flush=True)

    u32.SetWindowPos(hwnd, ctypes.c_void_p(HWND_NOTOPMOST), 0, 0, 0, 0,
                     SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)

    print(f"\n[probe] failures={len(_failures)}: {_failures}", flush=True)
    for name in _failures:
        print(f"[probe]   FAILED: {name}", flush=True)
    print(f"[probe] artifacts: {OUT_DIR}", flush=True)
    return 1 if _failures else 0


if __name__ == "__main__":
    code = 3
    try:
        code = main()
    except Exception:
        import traceback
        traceback.print_exc()
        code = 3
    finally:
        sys.exit(code)
