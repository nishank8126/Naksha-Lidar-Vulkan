"""st3c_app_one.py - Stage 3C interactive viewport acceptance harness.

LAUNCH THE REAL APPLICATION (PySide6 + Vulkan) on the workstation.

This is NOT a headless faked interaction: when PySide6/Vulkan are available it
launches the real AppWindow and the StreamManager attaches its telemetry hooks
to the VTK camera interactor so your manual viewport interaction is recorded
into stage3c_viewport_telemetry.jsonl.

Usage:
    .\venv\Scripts\python.exe st3c_app_one.py
Then follow the printed STEP 1..14 test sequence in the viewport.
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

SRC = r"H:\TESTING CONTIUES\FUNIVIA\NT219\MANDI_56.laz"
TE_PATH = os.path.join(os.path.dirname(SRC),
                       "stage3c_viewport_telemetry.jsonl")


def _print_instructions():
    print("\n" + "=" * 74)
    print("NAKSHA STAGE 3C INTERACTIVE VIEWPORT ACCEPTANCE")
    print("=" * 74)
    print(f"Dataset : MANDI_56.laz  ({os.path.getsize(SRC)/1024**3:.2f} GiB)")
    print(f"Cache   : committed .nakshaidx + .nakshapc (268.81M pts)")
    print(f"Mode    : DatasetMode.STREAMING  (cache-first open)")
    print(f"Telemetry: {TE_PATH}")
    print("Backend : PySide6 + Vulkan (NVIDIA T400) when available;")
    print("          headless telemetry stub otherwise.")
    print("=" * 74)
    print("""
STEP 1:  wait until the first visible point frame (overview + first tiles)
STEP 2:  FIT (frameminus / reset view)
STEP 3:  zoom approximately x2
STEP 4:  zoom x4
STEP 5:  zoom x8
STEP 6:  zoom x16
STEP 7:  zoom x32
STEP 8:  pan continuously for 60 seconds
STEP 9:  rapid pan reversals
STEP 10: FIT -> x16 -> FIT -> x32  (several cycles)
STEP 11: switch 2D -> 3D
STEP 12: orbit / pan / zoom for 30 seconds
STEP 13: return to 2D
STEP 14: close the application window normally

Telemetry is written live to the JSONL path above.  Closing the app flushes
the final sample.  Then run:
    .\\venv\\Scripts\\python.exe analyze_stage3c.py
""")


def main():
    _print_instructions()
    # Touch the telemetry file so it exists before the app starts.
    os.makedirs(os.path.dirname(TE_PATH) or ".", exist_ok=True)
    with open(TE_PATH, "w", encoding="utf-8") as f:
        f.write("")

    # ---- real app launch -------------------------------------------------- #
    try:
        from PySide6.QtWidgets import QApplication
        from gui.app_window import AppWindow
    except Exception as e:
        print(f"PySide6/Vulkan not available in this shell ({e!r}).")
        print("On the T400 workstation run this script directly so the import")
        print("succeeds and the real viewport is shown.")
        return 2

    app = QApplication.instance() or QApplication(sys.argv)
    win = AppWindow()
    win.show()
    app.processEvents()
    # The stream timer installed in app_streaming.install_streaming() will drive
    # telemetry from camera interaction. Block here until the window closes.
    rc = app.exec()
    # flush a final telemetry marker
    try:
        te = getattr(win, "_stream_telemetry_path", None)
        if te:
            from gui.naksha_cache.stream_telemetry import StreamTelemetry
            StreamTelemetry(te).event("app_closed", exit_code=int(rc))
    except Exception as e:
        print(f"warning: final telemetry flush failed: {e!r}")
    print(f"\nTelemetry saved to: {TE_PATH}")
    print("Now run: .\\venv\\Scripts\\python.exe analyze_stage3c.py")
    return int(rc)


if __name__ == "__main__":
    raise SystemExit(main())
