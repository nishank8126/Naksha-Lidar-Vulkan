"""DEV-only stage captures of a recorded production Surface mesh.

Run after verify_phase8_gui.py has saved surface_mesh.npz and camera evidence.
Never called by production UI; FINAL uses the original baked cell bytes.
"""
import argparse
import ctypes
import importlib.util
import json
import sys
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evidence", type=Path)
    parser.add_argument("--dll", type=Path, default=ROOT / "native/naksha_vulkan/build_msvc/naksha_vulkan_diagnostics.dll")
    args = parser.parse_args()
    output = args.evidence / "debug_modes"
    output.mkdir(parents=True, exist_ok=True)
    with np.load(args.evidence / "surface_mesh.npz", allow_pickle=False) as mesh:
        positions, faces, colors, counts = (mesh[k] for k in ("positions", "indices", "colors", "counts"))
    metadata = json.loads((args.evidence / "vtk_surface_reference.json").read_text())
    camera = metadata["camera"]
    spec = importlib.util.spec_from_file_location("surface_capture", ROOT / "diagnostics/visual_parity/capture_surface_drawproof_and_parity.py")
    harness = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(harness)
    harness.DLL_PATH = args.dll.resolve()
    hwnd = harness._create_diag_window(camera["viewport_width"], camera["viewport_height"])
    native = harness._Native(hwnd)
    native.create_renderer(camera["viewport_width"], camera["viewport_height"])
    from gui.render_backend import VulkanRenderBackend
    from gui.surface_mode import _compute_surface_face_colors
    backend = VulkanRenderBackend.__new__(VulkanRenderBackend)
    backend._dll, backend._handle = native.dll, native.handle
    indexed = native.dll.nkv_set_surface_indexed_blocks
    indexed.argtypes = [ctypes.c_uint64, ctypes.c_void_p, ctypes.c_uint64,
                        ctypes.c_void_p, ctypes.c_uint64, ctypes.c_void_p,
                        ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint64, ctypes.c_uint64]
    indexed.restype = ctypes.c_int
    for name in ("nkv_set_dataset_revision", "nkv_set_surface_revision"):
        function = getattr(native.dll, name)
        function.argtypes = [ctypes.c_uint64, ctypes.c_uint64]
        function.restype = ctypes.c_int
    for name in ("nkv_set_streaming_surface_active", "nkv_set_surface_debug_wireframe"):
        function = getattr(native.dll, name)
        function.argtypes = [ctypes.c_uint64, ctypes.c_int]
        function.restype = ctypes.c_int
    promote = native.dll.nkv_activate_pending_surface
    promote.argtypes = [ctypes.c_uint64, ctypes.POINTER(ctypes.c_int)]
    promote.restype = ctypes.c_int
    try:
        near, far = camera["clipping_range"]
        native.set_camera_ortho(camera["focal_point"], (0., 0., -1.), camera["parallel_scale"], near, far)
        native.set_clear_color(0., 0., 0., 1.)
        native.set_point_cloud_visible(False)
        native.dll.nkv_set_dataset_revision(native.handle, 1)
        for revision, mode in enumerate(("WIREFRAME", "SOLID", "NORMALS", "ELEVATION", "FINAL"), 1):
            stage_colors = colors.copy()
            if mode in ("WIREFRAME", "SOLID"):
                stage_colors[:] = 192
            elif mode == "NORMALS":
                for start in range(0, len(faces), 100000):
                    f = faces[start:start+100000]
                    normals = np.cross(positions[f[:, 1]]-positions[f[:, 0]], positions[f[:, 2]]-positions[f[:, 0]])
                    normals /= np.maximum(np.linalg.norm(normals, axis=1)[:, None], 1e-12)
                    stage_colors[start:start+len(f)] = np.clip((normals+1)*127.5, 0, 255).astype(np.uint8)
            elif mode == "ELEVATION":
                lo, hi = metadata["elevation_percentiles"]
                stage_colors = _compute_surface_face_colors(positions, faces, metadata["azimuth"], metadata["light_elevation"], 1., lo, hi)
            native.dll.nkv_set_surface_revision(native.handle, revision)
            if not backend.set_surface_indexed_blocks(positions, faces, stage_colors, counts, 1, revision):
                raise RuntimeError(f"{mode}: upload failed")
            stale = ctypes.c_int()
            if not promote(native.handle, ctypes.byref(stale)):
                raise RuntimeError(f"{mode}: stale/promotion failure")
            if not native.dll.nkv_set_streaming_surface_active(native.handle, 1):
                raise RuntimeError(f"{mode}: activation failed")
            if not native.dll.nkv_set_surface_debug_wireframe(native.handle, mode == "WIREFRAME"):
                raise RuntimeError(f"{mode}: raster diagnostic unsupported")
            for _ in range(3):
                if native.render() != 2:
                    raise RuntimeError(f"{mode}: frame not presented")
            proof = backend.get_surface_draw_proof()
            assert proof["point_draw_calls"] == 0 and proof["indexed_draw_calls"] > 0
            assert proof["polygon_mode"] == ("LINE" if mode == "WIREFRAME" else "FILL")
            frame = native.capture_frame()
            if frame is None:
                raise RuntimeError(f"{mode}: capture failed")
            stem = "SURFACE_DEBUG_" + mode
            Image.fromarray(frame["rgba"]).save(output / (stem + ".png"))
            (output / (stem + ".camera.json")).write_text(json.dumps(dict(camera, display_mode=stem), indent=2))
            (output / (stem + ".drawproof.json")).write_text(json.dumps(proof, indent=2))
            print(stem, proof["indexed_draw_calls"], proof["triangle_count"], flush=True)
    finally:
        native.dll.nkv_destroy_renderer(native.handle)
        destroy = ctypes.WinDLL("user32").DestroyWindow
        destroy.argtypes = [ctypes.c_void_p]
        destroy(hwnd)


if __name__ == "__main__":
    main()
