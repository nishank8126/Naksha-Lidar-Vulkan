"""Regression: open_file must not iterate a bare string into characters."""
import os
import pathlib

NAME = "test_classified_highprecision.laz"


def _normalize(filenames):
    """Mirror of the normalization in NakshaApp.open_file."""
    if isinstance(filenames, (str, os.PathLike)):
        return [str(filenames)]
    return [str(p) for p in filenames]


def main():
    fails = 0
    real = os.path.abspath(NAME)
    cases = (
        ("bare str", NAME, [NAME]),
        ("PathLike", pathlib.Path(real), [real]),
        ("single-item list", [NAME], [NAME]),
        ("two-item list", [NAME, "b.laz"], [NAME, "b.laz"]),
        ("tuple", (NAME,), [NAME]),
    )
    for label, given, expect in cases:
        got = _normalize(given)
        ok = got == expect
        print(f"[{'PASS' if ok else 'FAIL'}] {label}: {len(got)} file(s) -> {got}")
        if not ok:
            fails += 1

    # The original failure signature: 33 single characters.
    print(f"\n  for reference, iterating the string by hand yields "
          f"{len(list(NAME))} 'files': {list(NAME)[:5]}...")
    if len(_normalize(NAME)) == 1:
        print("[PASS] a bare str normalizes to exactly 1 file (was 33)")
    else:
        print("[FAIL] a bare str did not normalize to exactly 1 file")
        fails += 1

    print("PATH NORMALIZATION OK" if not fails else f"{fails} FAILURE(S)")
    return 1 if fails else 0



if __name__ == "__main__":
    raise SystemExit(main())
