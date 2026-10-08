"""Self-test of the pure-math helpers in vulkan_shading_parity_test.

No GUI / no Vulkan: exercises project_points, screen_barycentric,
perspective_correct, light_vector and expected_for_face against values that
are computable by hand, so an arithmetic slip in the harness is caught
without paying for a full app + GPU run.
"""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import vulkan_shading_parity_test as T  # noqa: E402

FAILS = []


def ok(name, cond, detail=""):
    print(f"[{'OK  ' if cond else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
    if not cond:
        FAILS.append(name)


# ---- light_vector is a unit vector, elevation maps to cos(zenith) ---------
for az, el in ((45.0, 45.0), (120.0, 12.0), (0.0, 85.0)):
    lv = T.light_vector(az, el)
    ok(f"light_vector unit (az={az},el={el})", abs(np.linalg.norm(lv) - 1.0) < 1e-12,
       f"norm={np.linalg.norm(lv):.12f}")
    ok(f"light_vector z=cos(90-el) (az={az},el={el})",
       abs(lv[2] - np.cos(np.radians(90.0 - el))) < 1e-12, f"z={lv[2]:.9f}")

# ---- project_points: identity MVP, 100x200 viewport, top-down rows ---------
mvp = np.eye(4)
W, H = 100, 200
pts = np.array([[-1.0, -1.0, 0.0], [0.0, 0.0, 0.0], [1.0, 1.0, 0.0]])
sx, sy, w = T.project_points(pts, mvp, np.zeros(3), W, H)
ok("project_points sx left edge", abs(sx[0] - 0.0) < 1e-9, f"{sx[0]}")
ok("project_points sx centre", abs(sx[1] - 50.0) < 1e-9, f"{sx[1]}")
ok("project_points sx right edge", abs(sx[2] - 100.0) < 1e-9, f"{sx[2]}")
ok("project_points sy top row", abs(sy[0] - 0.0) < 1e-9, f"{sy[0]}")
ok("project_points sy bottom row", abs(sy[2] - 200.0) < 1e-9, f"{sy[2]}")
ok("project_points w=1 for identity", np.allclose(w, 1.0), f"{w}")

o = np.array([10.0, 0.0, 0.0])
sx_o, _sy_o, _ = T.project_points(pts, mvp, o, W, H)
ok("project_points honours render origin", sx_o[1] < sx[1], f"{sx_o[1]:.3f} < {sx[1]:.3f}")

# ---- screen_barycentric: exact at the vertices, zero-area -> None ---------
tri = np.array([[0.0, 0.0], [100.0, 0.0], [0.0, 100.0]])
for i, (vx, vy) in enumerate(tri):
    lam = T.screen_barycentric(vx, vy, tri)
    e = np.zeros(3)
    e[i] = 1.0
    ok(f"barycentric at vertex {i}", np.allclose(lam, e, atol=1e-9), f"{np.round(lam, 9)}")
lam_c = T.screen_barycentric(25.0, 25.0, tri)
# v0=(0,0) v1=(100,0) v2=(0,100) => (25,25) solves to l=(0.5, 0.25, 0.25)
ok("barycentric analytic at (25,25)", np.allclose(lam_c, [0.5, 0.25, 0.25], atol=1e-9),
   f"{np.round(lam_c, 6)}")
ok("barycentric sums to 1", abs(sum(lam_c) - 1.0) < 1e-12, f"{sum(lam_c):.15f}")
ok("barycentric degenerate -> None",
   T.screen_barycentric(5.0, 5.0, np.array([[0.0, 0.0]] * 3)) is None)

# ---- perspective_correct -------------------------------------------------
attrs = np.array([[0.0, 0.0, 0.0], [255.0, 0.0, 0.0], [0.0, 0.0, 255.0]])
lam = np.array([0.5, 0.5, 0.0])
got = T.perspective_correct(lam, np.array([1.0, 1.0, 1.0]), attrs)
ok("perspective_correct == linear when w equal", np.allclose(got, [127.5, 0.0, 0.0]),
   f"{got}")
got2 = T.perspective_correct(np.array([1 / 3, 1 / 3, 1 / 3]),
                            np.array([1.0, 10.0, 1.0]), attrs)
ok("perspective_correct weights clip w (far vertex pulled in)",
   got2[0] < got2[2], f"R={got2[0]:.3f} < B={got2[2]:.3f}")
ok("perspective_correct degenerate w -> mean", np.allclose(
    T.perspective_correct(np.array([0.3, 0.3, 0.4]), np.zeros(3), attrs),
    attrs.mean(axis=0)))
ok("perspective_correct non-finite w -> mean", np.allclose(
    T.perspective_correct(np.array([0.3, 0.3, 0.4]),
                          np.array([np.inf, 1.0, 1.0]), attrs),
    attrs.mean(axis=0)))

# ---- expected_for_face: pure + mixed -------------------------------------
class _Cache:
    pass


cache = _Cache()
n = np.array([0.0, 0.0, 1.0])
cache.face_normals = np.array([n, n])
cache.shade = np.array([0.5, 0.5])
cache.faces = np.array([[0, 1, 2], [0, 1, 2]])

ctx = {
    "rgb_by_vertex": np.array([[200, 100, 50], [10, 220, 30], [0, 0, 255]],
                              dtype=np.uint8),
    "azimuth": 45.0, "eff_elev": 45.0,
    "ambient": 0.25, "floor": 0.08, "key": 0.85, "fill": 0.18,
}
sample = {"face": 0, "lam": np.array([1 / 3, 1 / 3, 1 / 3]), "w": np.ones(3),
          "px": 10, "py": 10}

exp_pure = T.expected_for_face("pure", sample, ctx, cache, 0.5, cache.faces[0])
ok("pure: normal stage == floor((n*0.5+0.5)*255)",
   np.allclose(exp_pure[1], np.floor((n * 0.5 + 0.5) * 255.0 + 1e-4)), f"{exp_pure[1]}")
ok("pure: final == trunc(classRGB * shade)",
   np.array_equal(exp_pure[0],
                  np.floor(np.array([200.0, 100.0, 50.0]) * 0.5 + 1e-4)),
   f"{exp_pure[0]}")
ok("pure: class-colour stage == albedo",
   np.array_equal(exp_pure[3], np.floor(np.array([200.0, 100.0, 50.0]) + 1e-4)),
   f"{exp_pure[3]}")
ok("pure: lighting stage == shade*255", np.array_equal(exp_pure[2], np.full(3, 127.0)),
   f"{exp_pure[2]}")

exp_mixed = T.expected_for_face("mixed", sample, ctx, cache, 0.0, cache.faces[0])
blend = np.clip(np.floor(T.perspective_correct(
    sample["lam"], sample["w"], ctx["rgb_by_vertex"].astype(float)) + 1e-4), 0, 255)
ok("mixed: class-colour stage == barycentric blend",
   np.allclose(exp_mixed[3], blend), f"{exp_mixed[3]} vs {blend}")
ka, kd = T.phong_params(ctx)
ok("phong_params Ka=max(ambient,floor)", abs(ka - 0.25) < 1e-12, f"Ka={ka}")
ok("phong_params Kd=1-Ka", abs(kd - 0.75) < 1e-12, f"Kd={kd}")

# ---- pick_candidates never exceeds the pool ------------------------------
rng = np.random.default_rng(0)
pool = np.arange(7)
picked = T.pick_candidates(rng, pool, 1000)
ok("pick_candidates clamps to pool size", len(picked) == 7, f"n={len(picked)}")
ok("pick_candidates no duplicates", len(set(picked.tolist())) == len(picked))
ok("pick_candidates empty pool", len(T.pick_candidates(rng, np.array([]), 10)) == 0)

# ---- barycentric_probe: normal-stage gate + blend-vs-flat discrimination --
HW = 200
images = {1: np.zeros((HW, HW, 3), np.uint8), 3: np.zeros((HW, HW, 3), np.uint8)}
tri_px = np.array([[60.0, 60.0], [150.0, 70.0], [70.0, 150.0]])


class _C2:
    xyz_final = np.array([[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0]])
    faces = np.array([[0, 1, 2]])
    face_normals = np.array([[0.0, 0.0, 1.0]])
    shade = np.array([0.5])


_saved = T.project_candidates
T.project_candidates = lambda cache, cands, mvp, origin, width, height: [{
    "face": int(cands[0]), "px": 105, "py": 90,
    "lam": np.asarray(T.screen_barycentric(105.0, 90.0, tri_px)),
    "w": np.ones(3)}]
try:
    probe = T.barycentric_probe(_C2(), ctx, images, None, None, np.array([0]))
    ok("barycentric_probe: black normal stage gates the face out", probe["n"] == 0,
       f"n={probe['n']}")

    normal_b = np.floor((np.array([0.0, 0.0, 1.0]) * 0.5 + 0.5) * 255.0 + 1e-4)
    images[1][90, 105] = normal_b.astype(np.uint8)
    blend = T.perspective_correct(
        T.screen_barycentric(105.0, 90.0, tri_px), np.ones(3),
        ctx["rgb_by_vertex"].astype(float))
    images[3][90, 105] = np.clip(np.floor(blend + 1e-4), 0, 255).astype(np.uint8)
    probe = T.barycentric_probe(_C2(), ctx, images, None, None, np.array([0]))
    ok("barycentric_probe: matching normal stage gates the face in", probe["n"] == 1,
       f"n={probe['n']}")
    # The image holds the TRUNCATED blend; the probe compares against the raw
    # float, so a perfect match still shows the sub-1-level truncation residue.
    ok("barycentric_probe: exact blend leaves only truncation residue",
       probe["blend_err"] < 1.0, f"{probe['blend_err']} levels")
    ok("barycentric_probe: blend is far from any single vertex colour",
       probe["flat_err"] > T.MAX_MAX, f"{probe['flat_err']:.1f} levels")
    ok("barycentric_probe: counts the sample as discriminating",
       probe["discriminating"] == 1, f"{probe['discriminating']}")

    # A flat-shaded (vertex-0) GPU output must be detected as a mismatch.
    images[3][90, 105] = np.array([200, 100, 50], np.uint8)
    flat = T.barycentric_probe(_C2(), ctx, images, None, None, np.array([0]))
    ok("barycentric_probe: flat-shaded output is rejected", flat["blend_err"] > T.MAX_MAX,
       f"{flat['blend_err']:.1f} levels")
finally:
    T.project_candidates = _saved

print("=" * 60)
print(f"{'SELF-TEST PASS' if not FAILS else 'SELF-TEST FAIL: ' + ', '.join(FAILS)}")
sys.exit(1 if FAILS else 0)

