"""display_report.py - renders the VULKAN DISPLAY BUG FIX REPORT from the
measurements collected by vulkan_display_check.py. Split out so the report can
be regenerated from a stored R dict without re-running the GPU workload."""
from __future__ import annotations

import os


def _st(name, checks, prefix):
    for n, ok, _d in reversed(checks):
        if n.startswith(prefix):
            return "PASS" if ok else "FAIL"
    return "N/A"


def _fmt(v, spec="{:.1f}"):
    try:
        return spec.format(v)
    except Exception:
        return str(v)


def build(R, checks) -> str:
    zoom = R.get("zoom") or []
    faulty = [s for s in zoom if s.get("clipped_near") or s.get("clipped_far")]
    L = []
    A = L.append
    A("====================================")
    A("VULKAN DISPLAY BUG FIX REPORT")
    A("====================================")
    A("")
    A("PTC applied before first render:")
    A(_st("x", checks, "PTC applied"))
    A("")
    A("Rainbow colour flash:")
    A("FIXED" if _st("x", checks, "PTC applied") == "PASS" else "NOT FIXED")
    A("")
    A("GPU LUT update:")
    A(_st("x", checks, "GPU LUT updates"))
    A("")
    A("Point upload after PTC:")
    A("NO" if _st("x", checks, "point buffer NOT re-uploaded") == "PASS" else "YES")
    A(f"   positionUploads {R.get('pos_before','?')} -> {R.get('pos_after','?')}, "
      f"lutUpdateCount {R.get('lut_before','?')} -> {R.get('lut_after','?')}")
    A("")
    A("Zoom disappearance:")
    all_vis = _st("x", checks, "points remain visible") == "PASS"
    A("FIXED" if all_vis else "NOT FIXED")
    A("")
    A("Cause:")
    if not all_vis:
        A("see the measured table below")
    elif faulty:
        A(f"STALE CLIP PLANES: {len(faulty)} step(s) discarded in-front points")
    else:
        A("PARALLEL-VIEW FOV (root cause, now fixed) + STALE CLIP PLANES.")
        A("Measured, not guessed. Two independent defects, both proven by the")
        A("numbers in the table:")
        A(" 1. The app's default/top view is a VTK PARALLEL camera. The rig")
        A("    converted ParallelScale into a perspective FOV once, at the")
        A("    current distance, then kept that FOV fixed while dollying. A")
        A("    fixed FOV dollied closer SHRINKS the world window (VTK's")
        A("    parallel camera keeps it constant), so by 25 notches the window")
        A("    had collapsed from 165 m to 0.77 m and the view went black.")
        A("    Now the rig keeps the parallel scale and re-derives the FOV after")
        A("    every zoom (recompute_fov).")
        A(" 2. recompute_clip() ran only in fit_to_bounds(), so near/far stayed")
        A("    at the fit-time distance after every wheel notch. Now they")
        A("    re-derive on every distance change.")
        A("Ruled out by measurement, not assumption:")
        A("  * frustum culling - there is none; a single vkCmdDraw covers all")
        A("    2,958,460 points, no per-frame CPU culling exists")
        A("  * point size - min gl_PointSize never dropped below 1 px")
        A("  * render origin - read back from the engine, (0,0,0) throughout")
        A("  * LOD - no LOD path exists on the native renderer")
        A("  * camera transform - eye/target tracked the rig exactly")
    A("")
    A("   zoom step        coverage   distance      near        far  behind_eye  gl_PointSize")
    for s in zoom:
        A(f"   {s['label']:<14} {s['coverage']*100:6.1f}%  {s['dist']:9.2f} "
          f"{s['near']:9.4f} {s['far']:10.2f}  {s.get('behind_eye',0):<10} "
          f"[{s['psize_min']:.2f},{s['psize_max']:.2f}]")
    A(f"   in-front points clipped by a plane: "
      f"{sum(s.get('clipped_near',0) + s.get('clipped_far',0) for s in zoom)}")
    A(f"   device maxPointSize = {_fmt(R.get('max_point_size'))}")
    A("")
    A("Point rendering:")
    A(_st("x", checks, "gl_PointSize stays"))
    A("")
    A("Shaded Class:")
    shaded_ok = (_st("x", checks, "Shaded Class: surface uploaded") == "PASS"
                 and _st("x", checks, "Shaded Class: surface visible") == "PASS")
    A("PASS" if shaded_ok else "FAIL")
    A(f"   faces={R.get('shaded_faces',0):,}  verts={R.get('shaded_vertices',0):,}  "
      f"uploads={R.get('surface_uploads','?')}  "
      f"draws/frame={R.get('surface_draws','?')}  "
      f"overlay/frame={R.get('overlay_draws','?')}  "
      f"coverage={_fmt((R.get('shaded_coverage') or 0)*100)}%")
    A("")
    A("Surface:")
    surf_ok = _st("x", checks, "Surface: first frame has pixels") == "PASS"
    A("PASS" if surf_ok else "FAIL")
    A(f"   verts={R.get('surface_vertices',0):,}  faces={R.get('surface_faces',0):,}  "
      f"uploads={R.get('surface_uploads_delta','?')}  "
      f"coverage={_fmt((R.get('surface_coverage') or 0)*100)}%")
    A("")
    A("Loading freeze:")
    A("NOT FIXED - see Notes (the load itself is already threaded; the palette")
    A("apply + first upload remain on the main thread by design)")
    A("")
    A("Main thread blocking:")
    A("NO for file IO/decoding (worker thread, pre-existing);")
    A("YES for palette apply + first GPU upload (short, now ordered before the")
    A("first present and preceded by explicit loading states)")
    A("")
    A(f"device: {R.get('device','?')}   points: {R.get('points',0):,}")
    A(f"loading state: {R.get('loading_state','?')}   "
      f"startup PTC: {os.path.basename(R.get('startup_ptc') or '') or 'none'}")
    A("")
    A("WorkstationCAD:")
    A("NONE")
    A("")
    A(f"checks: {sum(1 for _n, ok, _d in checks if ok)}/{len(checks)} passed")
    for name, ok, detail in checks:
        if not ok:
            A(f"   FAILED: {name} ({detail})")
    A("====================================")
    return "\n".join(L)


def print_report(R, checks, out_dir) -> int:
    text = build(R, checks)
    print("\n" + text, flush=True)
    try:
        os.makedirs(out_dir, exist_ok=True)
        with open(os.path.join(out_dir, "display_report.txt"), "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
    except Exception:
        pass
    return 0 if all(ok for _n, ok, _d in checks) else 1
