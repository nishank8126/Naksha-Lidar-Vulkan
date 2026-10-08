"""Regression test: _build_static_multiclass_blend_overlays return arity.

Under the Vulkan-owned LiDAR path the function skipped all VTK actor work and
returned a bare list, while every caller unpacks a 2-tuple. With an empty mixed
face set that raised:
    ValueError: not enough values to unpack (expected 2, got 0)
"""
import numpy as np
from types import SimpleNamespace

import gui.shading_display as sd
import gui.unified_actor_manager as uam

# Force the Vulkan-owned branch (this is the path that used to crash).
uam._vulkan_owns_lidar = lambda app: True


class _Cache:
    xyz_final = np.zeros((10, 3), dtype=np.float64)
    faces = np.array([[0, 1, 2]], dtype=np.int32)


def main() -> int:
    app = SimpleNamespace(vtk_widget=object(), _shading_static_blend_overlays=None)
    vc = {1, 2}
    fails = 0

    cases = (
        ("no vtk_widget (early return)", None, np.array([0], dtype=np.int64)),
        ("empty mixed_faces", "widget", np.array([], dtype=np.int64)),
        ("one mixed face (Vulkan skip)", "widget", np.array([0], dtype=np.int64)),
        ("many mixed faces (Vulkan skip)", "widget", np.arange(64, dtype=np.int64)),
    )
    for label, widget, faces in cases:
        a = SimpleNamespace(vtk_widget=(object() if widget else None),
                            _shading_static_blend_overlays=None)
        res = sd._build_static_multiclass_blend_overlays(a, _Cache(), vc, vc, faces)
        try:
            # Exactly what both real call sites do.
            actors, verts = res
        except ValueError as exc:
            print(f"[FAIL] {label}: {exc}")
            fails += 1
            continue
        ok = (isinstance(res, tuple) and len(res) == 2
              and isinstance(actors, (int, np.integer))
              and isinstance(verts, (int, np.integer)))
        print(f"[{'PASS' if ok else 'FAIL'}] {label}: "
              f"actors={actors} vertices={verts} "
              f"overlays={getattr(a, '_shading_static_blend_overlays', None)}")
        if not ok:
            fails += 1

    print("UNPACK OK - no ValueError on the Vulkan-owned path" if not fails
          else f"{fails} FAILURE(S)")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
