"""Rule #1 regression: small datasets MUST still take the IN_MEMORY path.

Streaming work must not regress an ordinary dataset. This asserts the mode
selector keeps existing projects on the legacy loader, and only diverts to
out-of-core when the data genuinely does not fit.

Run:  python streaming_mode_regression.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from gui.streaming.core import choose_storage_mode  # noqa: E402

# (label, points, dir)  -> expected mode
CASES = [
    ("123.las  (existing small project)",        1_200_000,     "IN_MEMORY"),
    ("classified_highprecision.laz",              8_000_000,     "IN_MEMORY"),
    ("medium aerial tile",                     26_960_750,     "IN_MEMORY"),
    ("NT219 MANDI_53.laz",                       7_546_210,     "IN_MEMORY"),
    ("NT219 primary tiles (1.14B)",          1_140_436_759,     "OUT_OF_CORE"),
    ("NT219 all files (2.49B)",              2_485_723_069,     "OUT_OF_CORE"),
]

fails = []


def main():
    print("[DATASET STORAGE MODE]")
    print("=" * 78)
    print(f"{'dataset':<38}{'points':>16}{'mode':>14}")
    print("-" * 78)
    for label, pts, expect in CASES:
        d = choose_storage_mode(pts, 28.0, r"H:\TESTING CONTIUES\FUNIVIA\NT219")
        ok = d["mode"] == expect
        print(f"{label:<38}{pts:>16,}{d['mode']:>14}   {'OK' if ok else 'MISMATCH'}")
        print(f"{'':<38}reason: {d['reason']}")
        if not ok:
            fails.append(f"{label}: expected {expect}, got {d['mode']}")
    print("-" * 78)
    print("RESULT:", "PASS" if not fails else "FAIL -> " + "; ".join(fails))
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())