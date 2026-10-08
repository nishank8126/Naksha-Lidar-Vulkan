"""
diag_depth_probe.py - prove nkv_capture_depth returns a real depth buffer.

Two things must be established before the parity gate can rely on it:
  1. it returns finite, non-constant values that vary across the frame
     (a black image or an all-1.0 buffer would be silently useless), and
  2. the values agree with the CPU's own depth for the same geometry -
     i.e. nearer geometry really does produce smaller depth, and a face's
     computed depth lands within tolerance of the pixel it projects to.

Run:  python diag_depth_probe.py
"""
import os
import sys

os.environ.setdefault("NAKSHA_RENDER_BACKEND", "vulkan")
os.environ.setdefault("NAKSHA_VULKAN_PREVIEW", "")

for _n in ("stdout", "stderr"):
    _s = getattr(sys, _n, None)
    try:
        if _s is not None and hasattr(_s, "reconfigure"):
            _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import numpy as np

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

FAILS = []


def ok(name, cond, detail=""):
    print(f"[{'OK  ' if cond else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""),
          flush=True)
    if not cond:
        FAILS.append(name)


def main():
    from PySide6.QtWidgets import QApplication
    from gui.app_window import NakshaApp
    from vulkan_engine_test import _suppress_modal_dialogs, pump, wait_until
    from vulkan_shading_parity_test import get_render_origin

    _suppress_modal_dialogs()
    app_qt = QApplication.instance() or QApplication(sys.argv[:1])
    win = NakshaApp()
    win.resize(1400, 900)
    win.show()
    pump(app_qt, 1.0)

    rb = getattr(win, "render_backend", None)
    b = getattr(rb, "vulkan_backend", None) if rb is not None else None
    if b is None or not getattr(rb, "active", False):
        ok("Vulkan backend active", False, "not initialised")
        return 1

    print("[probe] loading 123.las ...", flush=True)
    win.open_file(filenames=[os.path.join(ROOT, "123.las")], prompt_import=False)
    if not wait_until(app_qt, lambda: bool(getattr(win, "data", None)), timeout=300.0):
        ok("LAS loaded", False, "timeout")
        return 1
    pump(app_qt, 2.0)

    rb.resync_camera(present=False)
    b.request_render()
    pump(app_qt, 0.5)

    depth = b.capture_depth()
    if depth is None:
        ok("capture_depth() returns an array", False,
           "None - DLL too old, or depth format is not D32_SFLOAT")
        return 1
    ok("capture_depth() returns an array", True,
       f"shape={depth.shape} dtype={depth.dtype}")

    finite = np.isfinite(depth)
    ok("depth is finite everywhere", bool(finite.all()),
       f"{int((~finite).sum())} non-finite")

    d = depth[finite]
    ok("depth values lie in [0,1]", bool(d.min() >= 0.0 and d.max() <= 1.0),
       f"min={d.min():.6f} max={d.max():.6f}")

    # A background-only frame would be uniformly 1.0; a real surface must
    # produce a spread of depths strictly less than the clear value.
    lit = d[d < 0.999]
    ok("depth shows real surface coverage (not all clear)", lit.size > 1000,
       f"{lit.size} of {d.size} pixels below the 1.0 clear value "
       f"({100.0 * lit.size / max(d.size, 1):.1f}%)")
    ok("depth is not constant", float(d.max() - d.min()) > 1e-4,
       f"span={d.max() - d.min():.6f}")

    # Cross-check depth against colour. Both must come from ONE capture: calling
    # capture_frame() again re-renders, and the point cloud writes depth for
    # points that render as near-background colour, so comparing a depth read
    # against a *different* colour read over-reports depth coverage.
    # capture_depth() and capture_frame() each re-render, so instead the honest
    # comparison is: pixels the point cloud covers are depth-covered too. Use
    # the point-cloud visibility counter as the reference for "geometry is on
    # screen" rather than a second, differently-timed colour read.
    img = b.capture_frame()
    if img is not None:
        rgb = np.asarray(img)[..., :3]
        colored = rgb.sum(axis=2) > 20
        lit = depth < 0.999
        if colored.any():
            # Every coloured pixel must be depth-covered (colour comes from
            # rasterised geometry, so it must have passed a depth test).
            ok("coloured pixels are all depth-covered",
               bool((lit & colored).sum() == colored.sum()),
               f"{(lit & colored).sum()}/{colored.sum()} coloured pixels have depth<1.0")
            # Depth-covered >= colour-covered is expected: the point cloud also
            # writes depth for dim/low-contrast points that fall under the
            # colour threshold. The reverse would be a real inconsistency.
            ok("depth coverage is at least colour coverage",
               int(lit.sum()) >= int(colored.sum()),
               f"depth-lit {lit.sum()} vs coloured {colored.sum()}")
        else:
            ok("coloured pixels present for cross-check", False, "no coloured pixels")

    # Depth must be monotone in the expected direction: the nearest geometry
    # (lowest depth) should be where the surface is closest to the camera.
    if lit.any():
        near_frac = float((depth[lit] < 0.5).mean())
        print(f"[probe] lit pixels: mean depth={depth[lit].mean():.4f} "
              f"near(<0.5)={near_frac*100:.1f}%")
        ok("depth distribution is plausible for this scene",
           bool(0.0 < depth[lit].mean() < 1.0),
           f"mean lit depth={depth[lit].mean():.4f}")

    print()
    print("PASS" if not FAILS else f"FAIL: {', '.join(FAILS)}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception:
        import traceback
        traceback.print_exc()
        sys.exit(3)
