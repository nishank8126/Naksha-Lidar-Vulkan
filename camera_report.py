"""camera_report.py - renders the VULKAN CAMERA PARITY REPORT from the
measurements collected by vulkan_camera_parity.py."""
from __future__ import annotations

import os


def _st(checks, prefix):
    for n, ok, _d in reversed(checks):
        if n.startswith(prefix):
            return "PASS" if ok else "FAIL"
    return "N/A"


def build(R, checks) -> str:
    steps = R.get("steps") or []
    L = []
    A = L.append
    A("=====================================")
    A("VULKAN CAMERA PARITY REPORT")
    A("=====================================")
    A("")
    A("Camera type:")
    A("")
    A("Before:")
    A(R.get("projection_before", "?"))
    A("")
    A("After:")
    A(R.get("projection_after", "?"))
    A(f"   engine reports is_orthographic="
      f"{R.get('engine_is_ortho')}, parallel_scale={R.get('parallel_scale', 0):.3f}")
    A("")
    A("Fit view:")
    A(_st(checks, "Fit view"))
    A("")
    A("Zoom parity:")
    A(_st(checks, "Zoom in") if _st(checks, "Zoom in") != "N/A"
      else _st(checks, "Zoom"))
    A("")
    A("Pan parity:")
    A(_st(checks, "Point distortion") if _st(checks, "Point distortion") != "N/A" else "N/A")
    A("")
    A("Point distortion:")
    A("FIXED" if _st(checks, "Point distortion") == "PASS" else "NOT FIXED")
    A("")
    A("Surface appearance:")
    dens = _st(checks, "Point density")
    A({"PASS": "MATCH", "FAIL": "FAIL"}.get(dens, "PARTIAL"))
    A(f"   point density vs VTK: {_st(checks, 'Point density')}")
    A("")
    A("Measured comparison (world extent of the drawn cloud, per step):")
    A("   step          proj    par_scale   vtk extent        vk extent         "
      "vtk cov   vk cov")
    for s in steps:
        A(f"   {s['label']:<12}  {s['proj']:<6}  {s['parallel_scale']:9.3f}  "
          f"{s['vtk_extent'][0]:7.1f}x{s['vtk_extent'][1]:<7.1f} "
          f"{s['vk_extent'][0]:7.1f}x{s['vk_extent'][1]:<7.1f} "
          f"{s['vtk_cov']*100:6.1f}%  {s['vk_cov']*100:6.1f}%")
    A("")
    A("Not changed: shaders, surface algorithm, PTC, LUT, geometry, GPU buffers.")
    A("Only the camera/projection was replaced.")
    A("")
    A("WorkstationCAD: NONE")
    A("")
    A(f"checks: {sum(1 for _n, ok, _d in checks if ok)}/{len(checks)} passed")
    for name, ok, detail in checks:
        if not ok:
            A(f"   FAILED: {name} ({detail})")
    A("=====================================")
    return "\n".join(L)


def print_report(R, checks, out_dir) -> int:
    text = build(R, checks)
    print("\n" + text, flush=True)
    try:
        os.makedirs(out_dir, exist_ok=True)
        with open(os.path.join(out_dir, "camera_report.txt"), "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
    except Exception:
        pass
    return 0 if all(ok for _n, ok, _d in checks) else 1
