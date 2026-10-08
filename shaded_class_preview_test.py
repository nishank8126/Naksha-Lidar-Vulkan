"""Shaded Class STAGE 1 preview: real backend, mock Vulkan seam.

Runs the actual _compute_shading_geometry_backend through the real preview
plumbing and asserts the upload reaches the seam with sane arguments. VTK is
never touched, so this also proves no VTK actor is created.
"""
import time
from types import SimpleNamespace

import numpy as np
import laspy
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QEventLoop, QTimer

import gui.shading_display as sd


class MockVulkan:
    def __init__(self):
        self.calls = []
        self._n = 0

    def set_shaded_class_surface(self, xyz, faces, vclass, lut, mixed):
        self._n += 1
        self.calls.append({
            "points": len(xyz), "faces": len(faces),
            "vclass": np.asarray(vclass), "lut": np.asarray(lut),
            "mixed": np.asarray(mixed),
            "xyz_dtype": np.asarray(xyz).dtype,
            "faces_dtype": np.asarray(faces).dtype,
        })
        return True

    def set_crisp_shading_parameters(self, *a):
        return True

    def get_surface_upload_count(self):
        return self._n

    def last_error(self):
        return ""


def main() -> int:
    app_qt = QApplication.instance() or QApplication([])
    las = laspy.read("test_classified_highprecision.laz")
    xyz = np.column_stack([np.asarray(las.x, np.float64),
                           np.asarray(las.y, np.float64),
                           np.asarray(las.z, np.float64)])
    for dim in ("classification", "class"):
        if dim in las.point_format.dimension_names:
            classes = np.asarray(getattr(las, dim))
            break
    else:
        classes = np.zeros(len(xyz), np.uint8)
    classes = np.nan_to_num(classes).astype(np.uint8)[:len(xyz)]
    print(f"loaded points={len(xyz):,} classes={len(np.unique(classes))} distinct")

    vk = MockVulkan()
    rb = SimpleNamespace(active=True, vulkan_backend=vk)
    app = SimpleNamespace(
        render_backend=rb,
        class_palette={int(c): {"color": (10 * (i % 25), 200 - 5 * i, 90)}
                       for i, c in enumerate(np.unique(classes)[:8])},
        last_shade_azimuth=45.0, last_shade_angle=45.0, shade_ambient=0.25,
        shading_quality="normal")
    sd._get_shading_visibility = lambda a: set(int(c) for c in np.unique(classes))
    sd._shading_sharpness_angle = lambda a: 45.0
    sd._shading_sharpness_response = lambda x: (1.0, 0.0)
    sd._shading_key_fill_intensities = lambda a, s, o: (1.0, 0.35)
    sd._push_vulkan_shading_parity = lambda a, v: None

    t0 = time.perf_counter()
    started = sd._start_shaded_class_async_preview(
        app, xyz, classes, set(int(c) for c in np.unique(classes)),
        45.0, 45.0, 0.25, 0.10, 0.0, "normal")
    print(f"started={started}")

    # Drain the worker thread's event loop.
    loop = QEventLoop()
    QTimer.singleShot(180000, loop.quit)
    w = app._shaded_preview_worker
    if w is not None:
        w.finished.connect(loop.quit)
    loop.exec()
    app_qt.processEvents()
    elapsed = (time.perf_counter() - t0) * 1000.0

    # Profile the preview build's own stage timings - this is what the
    # unexplained ~1.8s needs to be attributed to.
    pt = sd._SHADED_PREVIEW_STATE.get("preview_timings") or {}
    if pt:
        print("\n--- SHADED PREVIEW STAGE TIMINGS ---")
        for k, v in sorted(pt.items(), key=lambda kv: -kv[1])[:12]:
            print(f"   {k:<28} {v * 1000.0:>9.1f} ms")
    print(f"\nelapsed={elapsed:.1f}ms  uploads={len(vk.calls)}")
    sd.print_shaded_class_performance_report()

    fails = 0
    print(f"elapsed={elapsed:.1f}ms  uploads={len(vk.calls)}")
    sd.print_shaded_class_preview_report()
    if not vk.calls:
        print("[FAIL] no upload reached the Vulkan seam")
        return 1
    c = vk.calls[-1]
    for label, cond, detail in (
        ("uploaded under 1s wall", elapsed < 1000.0, f"{elapsed:.1f}ms"),
        ("points reduced vs input", c["points"] < len(xyz),
         f"{c['points']:,} of {len(xyz):,}"),
        ("faces generated", c["faces"] > 0, f"{c['faces']:,}"),
        ("xyz float64", c["xyz_dtype"] == np.float64, str(c["xyz_dtype"])),
        ("faces int32", c["faces_dtype"] == np.int32, str(c["faces_dtype"])),
        ("vertex class length matches points",
         len(c["vclass"]) == c["points"], f"{len(c['vclass'])} vs {c['points']}"),
        ("lut has 256 rows", len(c["lut"]) == 256, str(len(c["lut"]))),
    ):
        print(f"[{'PASS' if cond else 'FAIL'}] {label}: {detail}")
        if not cond:
            fails += 1
    print("SHADED PREVIEW OK" if not fails else f"{fails} FAILURE(S)")
    return 1 if fails else 0

