"""Prove the STEP 5 invariant actually catches the historical false MISS.

Copies the real module into a scratch package, re-introduces the original bug
(the success path that never sets status="HIT"), and shows the invariant
rejects the resulting self-contradictory report.
"""
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

SRC_PKG = os.path.join(ROOT, "gui", "naksha_cache")
MOD = "normal_streaming.py"

OLD = '            rep.status = "HIT"'
NEW = '            pass  # BUG SIMULATION: status left at the default "MISS"'
NOINV = "    assert_report_invariant(reader, rep)\n"

src = open(os.path.join(SRC_PKG, MOD), encoding="utf-8").read()
assert OLD in src, "success-path HIT assignment not found - module changed?"
assert NOINV in src, "invariant call not found - module changed?"


def load(variant_src, tag):
    """Copy the module into a scratch package and import it under `tag`."""
    tmp = tempfile.mkdtemp(prefix="nsbug_")
    pkg = os.path.join(tmp, "nspkg")
    os.makedirs(pkg)
    open(os.path.join(pkg, "__init__.py"), "w").close()
    for fname in ("__init__.py", "format.py", "normals.py"):
        cand = os.path.join(SRC_PKG, fname)
        if os.path.isfile(cand):
            shutil.copy(cand, os.path.join(pkg, fname))
    with open(os.path.join(pkg, MOD), "w", encoding="utf-8") as fh:
        fh.write(variant_src)
    sys.path.insert(0, tmp)
    for m in [k for k in list(sys.modules) if k.startswith("nspkg")]:
        del sys.modules[m]
    import importlib
    return importlib.import_module("nspkg.normal_streaming")


def rep3_ok(rep):
    return (rep.status == "HIT" and rep.blocks == 1339
            and rep.stored_normals == 28860837
            and rep.bytes == 115491712 and rep.reason == "")


DS = os.path.join(ROOT, "test_classified_highprecision.laz")

# 1. Bug present, invariant PRESENT -> must raise.
bug_with_inv = src.replace(OLD, NEW, 1)
mod = load(bug_with_inv, "a")
try:
    mod.open_normal_cache(DS, expected_source_points=26960750)
except AssertionError as exc:
    print("INVARIANT CAUGHT THE REGRESSION:")
    print(f"  AssertionError: {exc}")
    caught = True
else:
    print("FAIL: regression not caught")
    caught = False

# 2. Bug present, invariant REMOVED -> reproduces the historical bad report.
bug_no_inv = bug_with_inv.replace(NOINV, "", 1)
mod2 = load(bug_no_inv, "b")
_r, rep = mod2.open_normal_cache(DS, expected_source_points=26960750)
print("\nWithout the invariant, the same code returned silently:")
print(f"  status={rep.status} blocks={rep.blocks:,} "
      f"stored_normals={rep.stored_normals:,} reason={rep.reason!r}")

# 3. Real, unpatched module -> HIT.
mod3 = load(src, "c")
_r, rep = mod3.open_normal_cache(DS, expected_source_points=26960750)
print(f"\nREAL MODULE -> status={rep.status} blocks={rep.blocks:,} "
      f"stored_normals={rep.stored_normals:,} bytes={rep.bytes:,} "
      f"reason={rep.reason!r}")

ok = caught and rep3_ok(rep)
print(f"\nRESULT: {'PASS' if ok else 'FAIL'}")
sys.exit(0 if ok else 1)