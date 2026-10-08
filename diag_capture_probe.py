"""Stage 1: prove the Vulkan capture path itself is sound, in isolation.

Everything downstream depends on this, so it deliberately does NOT use the
5.8M-face terrain. It builds a tiny synthetic mesh (known triangles, known
normals, known class colours), uploads it through the SAME
nkv_set_shaded_class_surface + nkv_capture_frame path production uses, and
checks in the order the task requires:

    Geometry -> Normals -> Class colours -> Lighting -> Final shading

A synthetic mesh is what makes this diagnostic: every expected pixel value is
known analytically, so a failure points at capture/mapping rather than at
terrain occlusion.
"""
import ctypes
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ["NAKSHA_RENDER_BACKEND"] = "vulkan"
os.environ["NAKSHA_VULKAN_MAIN_VIEWPORT"] = "1"
os.environ["NAKSHA_VULKAN_PREVIEW"] = ""

for _name in ("stdout", "stderr"):
    _s = getattr(sys, _name, None)
    try:
        if _s is not None and hasattr(_s, "reconfigure"):
            _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

RESULTS = []


def stage(name, ok, detail=""):
    RESULTS.append((name, bool(ok)))
    print(f"[{'OK  ' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
    return ok


# ---- CPU reference formulas, transcribed from shading_display.py ------------
def face_normal(p0, p1, p2):
    n = np.cross(p1 - p0, p2 - p0)
    ln = np.linalg.norm(n)
    if ln <= 1e-10:
        return np.zeros(3)
    u = n / ln
    if u[2] < 0.0 and abs(u[2]) > 0.3:
        u = -u
    return u


def light_vector(azimuth_deg, elevation_deg):
    zenith = np.radians(90.0 - elevation_deg)
    az_math = np.radians(360.0 - azimuth_deg + 90.0)
    return np.array([np.sin(zenith) * np.cos(az_math),
                     np.sin(zenith) * np.sin(az_math),
                     np.cos(zenith)])


def encode_normal_bytes(n):
    return np.floor(np.clip(n * 0.5 + 0.5, 0.0, 1.0) * 255.0 + 1e-4)


def build_scene():
    """A few large, well-separated triangles with DISTINCT normals/colours."""
    ang = np.radians([20.0, 110.0, 200.0, 290.0])
    r = 40.0
    xyz, faces = [], []
    for k, a in enumerate(ang):
        cx, cy = r * np.cos(a), r * np.sin(a)
        dz = [0.0, 6.0, 12.0, 18.0][k]      # tilt -> four different normals
        base = len(xyz)
        xyz += [[cx - 18.0, cy - 18.0, 0.0],
                [cx + 18.0, cy - 18.0, 0.0],
                [cx, cy + 18.0, dz]]
        faces.append([base, base + 1, base + 2])
    xyz = np.asarray(xyz, dtype=np.float64)
    faces = np.asarray(faces, dtype=np.int32)
    # vertex_class_id is PER VERTEX (V,), and all three vertices of a face carry
    # that face's class - which is exactly what makes a pure face pure.
    classes = np.array([10, 20, 30, 40], dtype=np.uint8)
    vclass = np.repeat(classes, 3).astype(np.uint8)
    lut = np.zeros((256, 3), dtype=np.uint8)
    lut[classes] = [[220, 30, 30], [30, 220, 30], [30, 30, 220], [240, 240, 30]]
    return xyz, faces, vclass, classes, lut


def main():
    from PySide6.QtWidgets import QApplication
    from gui.app_window import NakshaApp
    from vulkan_engine_test import _suppress_modal_dialogs, pump

    xyz, faces, vclass, fclass, lut = build_scene()
    normals = np.array([face_normal(xyz[f[0]], xyz[f[1]], xyz[f[2]]) for f in faces])

    _suppress_modal_dialogs()
    app_qt = QApplication.instance() or QApplication(sys.argv[:1])
    win = NakshaApp()
    win.resize(1200, 900)
    win.show()
    pump(app_qt, 1.0)

    rb = getattr(win, "render_backend", None)
    backend = getattr(rb, "vulkan_backend", None)
    if backend is None:
        stage("vulkan backend available", False)
        return 1

    if not backend.set_shaded_class_surface(xyz, faces, vclass, lut,
                                            np.zeros(0, dtype=np.int32)):
        stage("synthetic upload", False)
        return 1
    stage("synthetic upload", True, f"{len(faces)} triangles")

    lo, hi = xyz.min(axis=0), xyz.max(axis=0)
    # nkv_set_camera_ortho's `centre` is the point the view is centred on, and
    # the engine subtracts it when building the ortho box. My first attempt
    # passed a centre but still measured mvp[0][3] == -0.0 (no translation at
    # all) and every triangle landed at |ndc| ~ 26, i.e. far off-screen. The
    # camera call is made BEFORE the upload below re-derives the render origin,
    # so the origin the camera was built against and the origin the vertices
    # were stored with did not match. Fix: upload first, read the origin back,
    # THEN frame the camera in that same render space.
    backend.request_render()
    pump(app_qt, 0.3)

    fn0 = backend._dll.nkv_get_render_origin
    fn0.restype = ctypes.c_int
    fn0.argtypes = [ctypes.c_uint64, ctypes.POINTER(ctypes.c_double)]
    o0 = (ctypes.c_double * 3)()
    fn0(ctypes.c_uint64(backend._handle), o0)
    origin = np.array([o0[0], o0[1], o0[2]], dtype=np.float64)
    print(f"[info] render origin (post-upload) = {origin}")

    # Centre is expressed in RENDER space (what the camera expects).
    centre = [(lo[0] - origin[0] + hi[0] - origin[0]) * 0.5,
              (lo[1] - origin[1] + hi[1] - origin[1]) * 0.5,
              (hi[2] - origin[2]) + 20.0]
    half = float(np.max(hi - lo)) * 0.75
    backend.request_render()
    pump(app_qt, 0.5)

    # The app installs a VTK camera observer that re-pushes its own pose, which
    # can overwrite a directly-set native ortho camera between the call and the
    # next frame. Re-assert the framing, then verify the MVP actually changed
    # before trusting it: mvp[0][0] must be ~1/halfW, not an arbitrary value.
    fnm0 = backend._dll.nkv_get_last_mvp
    fnm0.restype = ctypes.c_int
    fnm0.argtypes = [ctypes.c_uint64, ctypes.POINTER(ctypes.c_float)]

    def read_mvp():
        b = (ctypes.c_float * 16)()
        fnm0(ctypes.c_uint64(backend._handle), b)
        return np.array(b, dtype=np.float64).reshape(4, 4).T

    before = read_mvp()
    # PROVEN: calling nkv_set_camera_ortho directly has NO effect here (mvp is
    # byte-identical before/after) because the app's installed VTK camera
    # observer re-pushes its own pose on every frame. So drive the camera
    # through VTK - the observer's own source of truth - and let the app's
    # adopt path push it to the engine.
    plotter = win.vtk_widget
    plotter.camera.parallel_projection = True
    plotter.camera.position = (centre[0] - origin[0], centre[1] - origin[1],
                               centre[2] - origin[2] + 4.0 * half)
    plotter.camera.focal_point = (centre[0] - origin[0], centre[1] - origin[1],
                                  centre[2] - origin[2])
    plotter.camera.up = (0.0, 1.0, 0.0)
    plotter.camera.parallel_scale = half
    plotter.reset_camera_clipping_range()
    plotter.render()
    rb.resync_camera(present=True)
    backend.request_render()
    pump(app_qt, 0.4)
    after = read_mvp()
    print(f"[info] mvp[0][0] before={before[0, 0]:.6f} "
          f"after={after[0, 0]:.6f}  (expect ~{1.0 / half:.6f})")

    fn = backend._dll.nkv_get_render_origin
    fn.restype = ctypes.c_int
    fn.argtypes = [ctypes.c_uint64, ctypes.POINTER(ctypes.c_double)]
    out = (ctypes.c_double * 3)()
    fn(ctypes.c_uint64(backend._handle), out)
    origin = np.array([out[0], out[1], out[2]], dtype=np.float64)
    print(f"[info] render origin (used) = {origin}")

    fnm = backend._dll.nkv_get_last_mvp
    fnm.restype = ctypes.c_int
    fnm.argtypes = [ctypes.c_uint64, ctypes.POINTER(ctypes.c_float)]
    mbuf = (ctypes.c_float * 16)()
    fnm(ctypes.c_uint64(backend._handle), mbuf)
    mvp = np.array(mbuf, dtype=np.float64).reshape(4, 4).T
    print("[info] mvp =\n", np.round(mvp, 5))
    # capture() refreshes this on every call so the projection always matches
    # the frame the pixels came from.
    state = {"mvp": mvp}

    def capture(stage_id):
        """Capture one debug stage.

        The MVP is read AFTER request_render but BEFORE capture_frame.
        nkv_capture_frame re-renders into the current frame slot, so reading
        the MVP after the capture returns the NEXT slot's camera - which is why
        the pixels stop lining up (measured: every triangle went off-screen
        the moment the MVP was re-read post-capture).
        """
        backend.set_crisp_debug_mode(stage_id)
        backend.request_render()
        pump(app_qt, 0.1)
        state['mvp'] = read_mvp()
        img = backend.capture_frame()
        backend.set_crisp_debug_mode(0)
        return None if img is None else np.ascontiguousarray(img[..., :3])

    def project(p, w, h):
        p = np.asarray(p, dtype=np.float64) - origin
        c = state['mvp'] @ np.append(p, 1.0)
        ndc = c[:3] / c[3]
        return ((ndc[0] * 0.5 + 0.5) * w, (ndc[1] * 0.5 + 0.5) * h)

    def project_ndc(p):
        p = np.asarray(p, dtype=np.float64) - origin
        c = state['mvp'] @ np.append(p, 1.0)
        return (c[:3] / c[3]), float(c[3])

    def at(img, cen):
        """Read the pixel at a projected centroid, or None if off-screen.

        Bounds are checked explicitly: numpy happily wraps a negative index, so
        an off-screen centroid would silently read the far edge of the frame and
        look like a plausible-but-wrong pixel.
        """
        ix, iy = [int(v) for v in project(cen, img.shape[1], img.shape[0])]
        if not (0 <= ix < img.shape[1] and 0 <= iy < img.shape[0]):
            return None
        return img[iy, ix].astype(int)

    AZ, ELEV, AMB, FLOOR, SHARP = 45.0, 45.0, 0.25, 0.08, 45.0
    L = light_vector(AZ, ELEV)
    gain = float(np.clip(0.55 + 0.90 * min(SHARP / 90.0, 1.0), 0.35, 6.50))
    sh_floor = max(AMB, FLOOR)
    raw_h = max(float(np.sin(np.radians(ELEV))), AMB)
    tgt_h = max(float(np.sin(np.radians(45.0))), sh_floor)

    def cpu_shade(n):
        ndl = float(n @ L)
        raw = min(max(max(ndl, 0.0), AMB), 1.0)
        return min(max(tgt_h + (raw - raw_h) * gain, sh_floor), 1.0)

    print("\n" + "=" * 70)
    print("STAGE 1  GEOMETRY / CAPTURE")
    print("=" * 70)
    img = capture(0)
    if img is None:
        stage("capture_frame returns an image", False)
        return 1
    stage("capture_frame returns an image", True, f"shape={img.shape}")
    lit = img.max(axis=2) > 8
    stage("framebuffer is not blank", bool(lit.mean() > 0.01),
          f"lit fraction = {lit.mean():.4f}")
    stage("framebuffer has multiple colours",
          len(np.unique(img.reshape(-1, 3), axis=0)) > 3,
          f"{len(np.unique(img.reshape(-1, 3), axis=0))} distinct")

    nonzero_all = True
    for f in range(len(faces)):
        cen = xyz[faces[f]].mean(axis=0)
        ndc, wclip = project_ndc(cen)
        v = at(img, cen)
        ok = v is not None and int(v.max()) > 8
        nonzero_all &= ok
        print(f"   tri {f}: ndc={np.round(ndc, 4).tolist()} w={wclip:.4g} "
              f"rgb={'OFF-SCREEN' if v is None else v.tolist()} "
              f"{'non-zero' if ok else 'FAIL'}")
    stage("known visible triangles return non-zero pixels", nonzero_all)


    print("\n" + "=" * 70)
    print("STAGE 2  NORMALS")
    print("=" * 70)
    nimg = capture(1)
    if nimg is None:
        stage("normal debug stage captures", False)
    else:
        allok = True
        for f in range(len(faces)):
            got = at(nimg, xyz[faces[f]].mean(axis=0))
            exp = encode_normal_bytes(normals[f]).astype(int)
            ok = got is not None and bool(np.all(np.abs(got - exp) <= 3))
            allok &= ok
            print(f"   tri {f}: gpu={'OFF' if got is None else got.tolist()} "
                  f"cpu={exp.tolist()} {'MATCH' if ok else 'MISMATCH'}")
        stage("face normals match CPU", allok)

    print("\n" + "=" * 70)
    print("STAGE 3  CLASS COLOURS")
    print("=" * 70)
    cimg = capture(3)
    if cimg is None:
        stage("class debug stage captures", False)
    else:
        allok = True
        for f in range(len(faces)):
            got = at(cimg, xyz[faces[f]].mean(axis=0))
            exp = lut[fclass[f]].astype(int)
            ok = got is not None and bool(np.all(np.abs(got - exp) <= 3))
            allok &= ok
            print(f"   tri {f}: gpu={'OFF' if got is None else got.tolist()} "
                  f"cpu={exp.tolist()} {'MATCH' if ok else 'MISMATCH'}")
        stage("class colours match CPU", allok)

    print("\n" + "=" * 70)
    print("STAGE 4  LIGHTING")
    print("=" * 70)
    limg = capture(2)
    if limg is None:
        stage("lighting debug stage captures", False)
    else:
        allok = True
        for f in range(len(faces)):
            got = at(limg, xyz[faces[f]].mean(axis=0))
            shade = cpu_shade(normals[f])
            exp = int(np.floor(shade * 255.0 + 1e-4))
            ok = got is not None and abs(int(got.max()) - exp) <= 3
            allok &= ok
            print(f"   tri {f}: gpu={'OFF' if got is None else int(got.max())} "
                  f"cpu={exp} (shade={shade:.4f}, gain={gain:.3f}) "
                  f"{'MATCH' if ok else 'MISMATCH'}")
        stage("lighting factor matches CPU", allok)

    print("\n" + "=" * 70)
    print("STAGE 5  FINAL SHADED")
    print("=" * 70)
    fimg = capture(0)
    if fimg is None:
        stage("final stage captures", False)
    else:
        allok = True
        for f in range(len(faces)):
            got = at(fimg, xyz[faces[f]].mean(axis=0))
            shade = cpu_shade(normals[f])
            exp = np.floor(np.clip(lut[fclass[f]].astype(float) * shade,
                                   0, 255) + 1e-4).astype(int)
            ok = got is not None and bool(np.all(np.abs(got - exp) <= 3))
            allok &= ok
            print(f"   tri {f}: gpu={'OFF' if got is None else got.tolist()} "
                  f"cpu={exp.tolist()} (shade={shade:.4f}) "
                  f"{'MATCH' if ok else 'MISMATCH'}")
        stage("final shaded colour matches CPU", allok)

    print("\n" + "=" * 70)
    passed = sum(1 for _n, ok in RESULTS if ok)
    for n, ok in RESULTS:
        print(f"  {'PASS' if ok else 'FAIL'}  {n}")
    print(f"\n{passed}/{len(RESULTS)} stages passed")
    print("=" * 70)
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())

