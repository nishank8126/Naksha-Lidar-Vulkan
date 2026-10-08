"""[EXTREME DATASET DISCOVERY] - LAS/LAZ HEADER-ONLY scan.

Deliberately decodes NOTHING. Every number comes from a header read, so this is
safe against a multi-terabyte survey and answers the question the streaming
engine must be designed around: how many points does this dataset ACTUALLY
contain? That number is not assumed anywhere in this project - it is measured,
and every capacity decision is derived from it.
"""
import os
import sys
import time

# GPU bytes per point for the engine's tile format:
#   xyz float32 (12) + rgb uint8*3 (3) + class uint8 (1) + intensity float32 (4)
GPU_BYTES_PER_POINT = 20.0
# RAM bytes per point while a tile is resident/decoded.
RAM_BYTES_PER_POINT = 32.0


def scan_one(path):
    """Header-only read of one LAS/LAZ. Never reads point data."""
    import laspy
    t0 = time.perf_counter()
    with laspy.open(path) as r:
        h = r.header
        # laspy computes the header bounds lazily; reading them is a header
        # operation (header block + VLRs), NOT a point scan.
        try:
            mins = [float(v) for v in h.mins]
            maxs = [float(v) for v in h.maxs]
        except Exception:
            mins = maxs = None
        try:
            count = int(h.point_count)
        except Exception:
            count = 0
        try:
            fmt = int(h.point_format.id)
        except Exception:
            fmt = -1
        try:
            ver = f"{h.version.major}.{h.version.minor}"
        except Exception:
            ver = "?"
        try:
            scale = [float(v) for v in h.scales]
            offset = [float(v) for v in h.offsets]
        except Exception:
            scale = offset = None
        dims = {str(d.name).lower() for d in h.point_format.dimensions}
        info = {
            "path": path, "name": os.path.basename(path),
            "size": os.path.getsize(path), "mtime": os.path.getmtime(path),
            "version": ver, "format": fmt, "points": count,
            "mins": mins, "maxs": maxs, "scale": scale, "offset": offset,
            "rgb": "red" in dims, "intensity": "intensity" in dims,
            "classification": "classification" in dims,
            "gps": "gps_time" in dims, "dims": sorted(dims),
        }
    info["header_ms"] = (time.perf_counter() - t0) * 1000.0
    return info


def print_results(results, root, elapsed):
    total_points = sum(r["points"] for r in results)
    total_bytes = sum(r["size"] for r in results)
    gmins = [r["mins"] for r in results if r.get("mins")]
    gmaxs = [r["maxs"] for r in results if r.get("maxs")]
    bounds = None
    if gmins and gmaxs:
        bounds = ([min(b[i] for b in gmins) for i in range(3)],
                  [max(b[i] for b in gmaxs) for i in range(3)])

    print("=" * 72)
    print("[EXTREME DATASET DISCOVERY]")
    print("=" * 72)
    print(f"  Root: {root}\n")
    print(f"  {'file':<34}{'points':>14}{'size MB':>12}  fmt RGB I C")
    print("  " + "-" * 70)
    for r in results:
        print(f"  {r['name'][:33]:<34}{r['points']:>14,}{r['size'] / 1e6:>12,.1f}"
              f"  {r['format']:>3}  {'Y' if r['rgb'] else '-'} "
              f"{'Y' if r['intensity'] else '-'} {'Y' if r['classification'] else '-'}")
    print("  " + "-" * 70)
    print(f"  {'TOTAL':<34}{total_points:>14,}{total_bytes / 1e6:>12,.1f}")

    print("\n  Per-file detail:")
    for r in results:
        print(f"\n    {r['name']}")
        print(f"      LAS version      : {r['version']}   point format: {r['format']}")
        print(f"      points           : {r['points']:,}")
        print(f"      compressed size  : {r['size'] / 1e6:,.1f} MB")
        print(f"      XYZ scale        : {r['scale']}")
        print(f"      XYZ offset       : {r['offset']}")
        print(f"      RGB / intensity  : {'yes' if r['rgb'] else 'no'} / "
              f"{'yes' if r['intensity'] else 'no'}")
        print(f"      classification   : {'yes' if r['classification'] else 'no'}")
        print(f"      dimensions       : {', '.join(r['dims'])}")
        if r.get("mins"):
            mn, mx = r["mins"], r["maxs"]
            print(f"      bounds X         : {mn[0]:,.2f} .. {mx[0]:,.2f}")
            print(f"      bounds Y         : {mn[1]:,.2f} .. {mx[1]:,.2f}")
            print(f"      bounds Z         : {mn[2]:,.2f} .. {mx[2]:,.2f}")
        print(f"      header read      : {r['header_ms']:.1f} ms")

    if bounds:
        mn, mx = bounds
        print("\n  DATASET BOUNDS")
        print(f"    X: {mn[0]:,.2f} .. {mx[0]:,.2f}   ({mx[0] - mn[0]:,.2f} m)")
        print(f"    Y: {mn[1]:,.2f} .. {mx[1]:,.2f}   ({mx[1] - mn[1]:,.2f} m)")
        print(f"    Z: {mn[2]:,.2f} .. {mx[2]:,.2f}   ({mx[2] - mn[2]:,.2f} m)")

    print("\n" + "=" * 72)
    print(f"  TOTAL FILES                 : {len(results)}")
    print(f"  TOTAL POINTS                : {total_points:,}")
    print(f"  TOTAL COMPRESSED SIZE       : {total_bytes / 1e9:,.2f} GB")
    print(f"  ESTIMATED xyz float64       : {total_points * 24 / 1e9:,.2f} GB")
    print(f"  ESTIMATED GPU (all resident): "
          f"{total_points * GPU_BYTES_PER_POINT / 1e9:,.2f} GB"
          f" ({GPU_BYTES_PER_POINT:.0f} B/point)")
    print(f"  ESTIMATED RAM (all resident): "
          f"{total_points * RAM_BYTES_PER_POINT / 1e9:,.2f} GB"
          f" ({RAM_BYTES_PER_POINT:.0f} B/point)")
    print(f"  HEADER SCAN TIME            : {elapsed * 1000:,.1f} ms "
          f"(no point data decoded)")
    print("=" * 72)


def main():
    root = sys.argv[1] if len(sys.argv) > 1 else r"H:\TESTING CONTIUES\FUNIVIA\NT219"
    files = []
    for dirpath, _dirs, names in os.walk(root):
        for n in names:
            if n.lower().endswith((".laz", ".las")):
                files.append(os.path.join(dirpath, n))
    files.sort()

    t0 = time.perf_counter()
    results = []
    for p in files:
        try:
            results.append(scan_one(p))
        except Exception as exc:
            print(f"  !! {os.path.basename(p)}: {exc}")
    print_results(results, root, time.perf_counter() - t0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

