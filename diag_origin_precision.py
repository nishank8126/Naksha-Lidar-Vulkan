"""Prove the floating-origin fix restores facet integrity.

Models exactly what the engine does on upload:
    renderPos = float32(worldPos - origin)
then reports, for the real UTM-scale dataset:
  * how many triangles COLLAPSE to zero area (facets destroyed), and
  * the CPU-vs-GPU face-normal disagreement,
with the old origin (0,0,0) and the new auto-derived (data centre) origin.

The CPU normal is _compute_face_normals_fast() transcribed; the GPU normal is
faceNormal() from surface.frag. Both are the same formula, so ANY difference
comes purely from the coordinates fed in.
"""
import numpy as np

FAILS = []


def ok(name, cond, detail=""):
    print(f"[{'OK  ' if cond else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
    if not cond:
        FAILS.append(name)


def face_normals(xyz, faces):
    """cross(v1-v0, v2-v0), normalize, hemisphere fix - both sides' formula."""
    v0 = xyz[faces[:, 0]]
    v1 = xyz[faces[:, 1]]
    v2 = xyz[faces[:, 2]]
    n = np.cross(v1 - v0, v2 - v0)
    length = np.linalg.norm(n, axis=1)
    safe = length > 1e-10
    n[safe] = n[safe] / length[safe, None]
    flip = (n[:, 2] < 0) & (np.abs(n[:, 2]) > 0.3)
    n[flip] = -n[flip]
    return n


def report(tag, xyz, faces, origin):
    cpu_n = face_normals(xyz, faces)
    # What the engine actually stores, then the shader's faceNormal() on it.
    gpu_xyz = (xyz - origin).astype(np.float32).astype(np.float64)
    gpu_n = face_normals(gpu_xyz, faces)

    a = xyz[faces[:, 1]] - xyz[faces[:, 0]]
    b = xyz[faces[:, 2]] - xyz[faces[:, 0]]
    area_true = 0.5 * np.linalg.norm(np.cross(a, b), axis=1)

    ga = gpu_xyz[faces[:, 1]] - gpu_xyz[faces[:, 0]]
    gb = gpu_xyz[faces[:, 2]] - gpu_xyz[faces[:, 0]]
    area_gpu = 0.5 * np.linalg.norm(np.cross(ga, gb), axis=1)

    collapsed = int(np.sum(area_gpu <= 0.0))
    ang = np.degrees(np.arccos(np.clip((gpu_n * cpu_n).sum(axis=1), -1.0, 1.0)))

    print(f"\n--- {tag} ---")
    print(f"  origin                 : {origin}")
    print(f"  true median area (m^2) : {np.median(area_true):.6f}")
    print(f"  gpu   median area (m^2): {np.median(area_gpu):.6f}")
    print(f"  collapsed triangles    : {collapsed:,} / {len(faces):,}"
          f"  ({100.0 * collapsed / len(faces):.2f}%)")
    print(f"  normal angle err (deg) : median={np.median(ang):.3f}"
          f"  p95={np.percentile(ang, 95):.3f}  max={ang.max():.3f}")
    return collapsed, float(np.median(ang))


# ---- the REAL dataset's coordinate ranges, measured from 123.las ----------
x_lo, x_hi = 256200.0, 256349.99
y_lo, y_hi = 4779300.0, 4779449.99
z_lo, z_hi = 286.99, 342.66
print(f"real data x[{x_lo},{x_hi}] y[{y_lo},{y_hi}] z[{z_lo},{z_hi}]")
print(f"float32 ulp  x={np.spacing(np.float32(x_lo)):.6f} m"
      f"  y={np.spacing(np.float32(y_lo)):.3f} m"
      f"  z={np.spacing(np.float32(z_lo)):.3e} m")

# Build a TIN patch over the real extent at the REAL face density.
# 5,839,349 faces span x 150 m x y 150 m => ~0.0039 m^2 per triangle,
# i.e. ~62 mm edges. A 150 m / 0.062 m grid needs ~2400 x 2400 nodes.
n_side = 2400
xs = np.linspace(x_lo, x_hi, n_side)
ys = np.linspace(y_lo, y_hi, n_side)
XX, YY = np.meshgrid(xs, ys, indexing="ij")
ZZ = z_lo + 20.0 * np.sin(XX / 7.0) * np.cos(YY / 5.0)   # real relief
xyz = np.stack([XX.ravel(), YY.ravel(), ZZ.ravel()], axis=1)
I, J = np.meshgrid(np.arange(n_side - 1), np.arange(n_side - 1), indexing="ij")
I, J = I.ravel(), J.ravel()
v00 = I * n_side + J
faces = np.vstack([
    np.column_stack([v00, v00 + n_side, v00 + 1]),
    np.column_stack([v00 + n_side, v00 + n_side + 1, v00 + 1]),
])
print(f"synthetic mesh: {len(xyz):,} verts, {len(faces):,} faces "
      f"({100.0 * (x_hi - x_lo) * (y_hi - y_lo) / len(faces):.5f} m^2/face)")

c0, a0 = report("BEFORE  origin=(0,0,0)  [old behaviour]", xyz, faces,
                np.zeros(3))

centre = np.array([0.5 * (x_lo + x_hi), 0.5 * (y_lo + y_hi), 0.5 * (z_lo + z_hi)])
c1, a1 = report("AFTER   origin=data centre  [fix]", xyz, faces, centre)

print()
ok("old origin destroyed triangles", c0 > 0, f"{c0:,} collapsed")
ok("new origin destroys none", c1 == 0, f"{c1} collapsed")
ok("normal error reduced to ~0", a1 < 0.05, f"median {a1:.4f} deg")
ok("new origin is a strict improvement", a1 < a0, f"{a0:.3f} -> {a1:.4f} deg")

print("=" * 60)
print(f"{'ORIGIN FIX SELF-TEST PASS' if not FAILS else 'FAIL: ' + ', '.join(FAILS)}")
raise SystemExit(1 if FAILS else 0)
