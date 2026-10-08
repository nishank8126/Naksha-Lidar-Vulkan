"""Block codec verification: SoA round-trip, uint16 RGB, selective reads.

Covers the properties the format exists to guarantee:

  Part 6  uint16 RGB survives encode -> decode as 49920, NOT 0 - the permanent
          regression for the bug that rendered a whole real-colour cloud black.
  Part 5  a classification-only read touches only classification bytes and
          never reads RGB off disk.
  Part 7  world = origin + local * scale reconstructs within source precision.
  Part 8  quantisation error reported per axis.
  Part 4  an independent block is readable without parsing earlier blocks.
  Part 40 a deliberately corrupted block fails its CRC instead of decoding.
  Part 9  Morton edge cases: min, max, duplicates, no overflow, no NaN.

Run: python naksha_block_verify.py
"""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from gui.naksha_cache import format as F   # noqa: E402

fails = []


def check(name, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}"
          + (f" - {detail}" if detail else ""))
    if not ok:
        fails.append(name)


def main():
    print("\n[NAKSHA BLOCK CODEC VERIFICATION]")
    pc = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                      "_blk_test.nakshapc")
    rng = np.random.default_rng(12345)
    n = 50_000

    x = 682890.0 + rng.random(n) * 80.0
    y = 3504331.0 + rng.random(n) * 60.0
    z = 1062.0 + rng.random(n) * 40.0
    cls = rng.integers(0, 30, n).astype(np.uint8)
    inten = rng.integers(0, 65535, n).astype(np.uint16)
    # PART 6: uint16 RGB including the exact value that used to be destroyed.
    rgb16 = rng.integers(0, 65535, (n, 3)).astype(np.uint16)
    rgb16[0] = (49920, 49920, 49920)
    rgb16[1] = (65535, 0, 32768)
    src_id = (np.arange(n, dtype=np.uint64) * 7 + 1000)

    bmin = np.array([x.min(), y.min(), z.min()])
    bmax = np.array([x.max(), y.max(), z.max()])
    local, origin, scale = F.quantize_xyz(x, y, z, bmin, bmax)

    mask = (F.ATTR_XYZ | F.ATTR_CLASSIFICATION | F.ATTR_INTENSITY
            | F.ATTR_RGB | F.ATTR_SOURCE_ID | F.ATTR_RETURN_NUMBER)
    streams = {
        F.ATTR_XYZ: local,
        F.ATTR_CLASSIFICATION: cls,
        F.ATTR_INTENSITY: inten,
        F.ATTR_RGB: rgb16,
        F.ATTR_SOURCE_ID: src_id,
        F.ATTR_RETURN_NUMBER: np.full(n, 1, np.uint8),
    }

    w = F.BlockWriter(pc)
    w.add(0, 0, 0, streams, mask, 16, origin, scale)
    w.add(1, 1, 0, streams, mask, 16, origin, scale)   # 2nd independent block
    w.close()
    d = w.directory()
    check("PART 4: block directory written", d.size == 2,
          f"{d.size} entries at offsets {list(map(int, d['file_offset']))}")

    with open(pc, "rb") as fh:
        hdr, out, nb = F.read_block(fh, d[0])
        got = out[F.ATTR_RGB]
        check("PART 6: uint16 RGB preserved through encode+decode",
              got.dtype == np.uint16 and int(got[0, 0]) == 49920,
              f"dtype {got.dtype}, point 0 RGB {got[0].tolist()}")
        check("PART 6: ALL RGB values bit-exact",
              np.array_equal(got, rgb16),
              f"{int((got != rgb16).sum())} differing of {n * 3} values")
        check("PART 6: uint16 max and mid values survive",
              int(got[1, 0]) == 65535 and int(got[1, 2]) == 32768,
              f"point 1 RGB {got[1].tolist()}")
        _, cls_only, nb_cls = F.read_block(
            fh, d[0], only_attrs=(F.ATTR_XYZ, F.ATTR_CLASSIFICATION))
        _, all_streams, nb_all = F.read_block(fh, d[0])
        rgb_bytes = int(d[0]["point_count"]) * 3 * 2
        check("PART 5: classification-only read skips RGB bytes",
              nb_cls < nb_all - rgb_bytes,
              f"{nb_cls:,} B vs {nb_all:,} B full (RGB stream {rgb_bytes:,} B)")
        check("PART 5: selective read returns only requested streams",
              set(cls_only.keys()) == {F.ATTR_XYZ, F.ATTR_CLASSIFICATION},
              f"got {sorted(F.ATTR_NAMES[k] for k in cls_only)}")
        check("PART 5: classification intact in selective read",
              np.array_equal(cls_only[F.ATTR_CLASSIFICATION], cls))

        world = F.block_world_xyz(hdr, out[F.ATTR_XYZ])
        max_err = np.abs(world - np.column_stack((x, y, z))).max(axis=0)
        check("PART 8: XYZ error within source LAS quantisation (1 mm)",
              float(max_err.max()) <= 1e-3,
              f"max X {max_err[0]:.2e} Y {max_err[1]:.2e} Z {max_err[2]:.2e} m")
        check("PART 7: world = origin + local*scale identity holds",
              np.allclose(F.dequantize_xyz(local, origin, scale), world))
        check("PART 10: source id stream round-trips exactly",
              np.array_equal(out[F.ATTR_SOURCE_ID], src_id))

        _, out2, _ = F.read_block(fh, d[1])
        check("PART 4: second block readable independently",
              int(out2[F.ATTR_XYZ].shape[0]) == n,
              f"block 1 holds {out2[F.ATTR_XYZ].shape[0]:,} points")

        # ---- Part 40: a corrupted block must be DETECTED, not decoded ----
        # Corrupt INSIDE THE PAYLOAD (after header + stream directory). An
        # offset below that damages only fields the payload CRC does not
        # cover, so the block would still decode and corruption would go
        # UNDETECTED.
        target = (int(d[0]["file_offset"]) + F.PC_BLOCK_HEADER.itemsize
                  + F.PC_STREAM.itemsize * len(F.STREAM_ORDER) + 200)
        with open(pc, "r+b") as cf:
            cf.seek(target)
            b = cf.read(1)
            cf.seek(target)
            cf.write(bytes([b[0] ^ 0xFF]))
        detected = False
        try:
            with open(pc, "rb") as fh2:
                F.read_block(fh2, d[0], verify_crc=True)
        except ValueError as exc:
            detected = "CRC" in str(exc)
        check("PART 40: corrupted block fails CRC validation", detected,
              "CRC mismatch raised" if detected else "NOT detected")

    print()
    for label, xs, ys in [
            ("min coords", [0.0], [0.0]),
            ("max coords", [1.0], [1.0]),
            ("duplicates", [0.5] * 8, [0.5] * 8),
            ("large local values", [1e9, -1e9], [1e9, -1e9]),
            ("zero span", [0.0, 0.0], [0.0, 0.0])]:
        c = F.morton2d(xs, ys, 0.0, 1.0, 0.0, 1.0, bits=32)
        ok = c.dtype == np.uint64 and not np.any(np.isnan(c.astype(np.float64)))
        check(f"PART 9: Morton {label} - no overflow, no NaN", ok,
              f"dtype {c.dtype}, max {int(c.max()) if c.size else 0}")
    # BOTH axes must be 1 to reach 2^64-1: X interleaves into EVEN bit positions
    # and Y into ODD ones, so x=1,y=0 legitimately yields only even bits (~2^62).
    # Asserting 2^64-1 for that input would fail against a CORRECT implementation,
    # so the previous assertion was testing the wrong property.
    c = F.morton2d([1.0], [1.0], 0.0, 1.0, 0.0, 1.0, bits=32)
    check("PART 9: Morton max code equals 2^64-1, not wrapped",
          int(c[0]) == (1 << 64) - 1 and c.dtype == np.uint64,
          f"max {int(c[0])}, dtype {c.dtype}")
    cx = F.morton2d([1.0], [0.0], 0.0, 1.0, 0.0, 1.0, bits=32)
    check("PART 9: X-only input lands on even bits, below 2^63",
          int(cx[0]) < (1 << 63), f"max {int(cx[0])}")
    try:
        F.morton_check_bits(33)
        rejected = False
    except ValueError:
        rejected = True
    check("PART 9: out-of-range bits rejected instead of wrapping", rejected)
    dup = F.morton2d([0.5] * 4, [0.5] * 4, 0.0, 1.0, 0.0, 1.0, bits=16)
    check("PART 9: identical coordinates give identical codes",
          int(np.unique(dup).size) == 1)

    os.remove(pc)
    print(f"\n  RESULT: {'PASS' if not fails else 'FAIL -> ' + '; '.join(fails)}")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
