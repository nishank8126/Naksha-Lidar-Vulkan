"""[LARGE DATASET READINESS] - pre-flight capacity check. Measurement only.

Answers one question before anything is loaded: can this machine hold this
dataset in VRAM? Reads the LAS/LAZ header only (laspy.open), so it costs
milliseconds and never decompresses point data.
"""
import os
import subprocess
import sys

BYTES_PER_POINT = 15.0   # xyz f32 (12) + class u8 (1) + intensity i16 (2)


def header_point_count(path):
    """Point count from the header alone. Never reads point data."""
    import laspy
    with laspy.open(path) as r:
        return int(r.header.point_count)


def _nvidia_vram_bytes():
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10)
        if out.returncode == 0 and out.stdout.strip():
            return float(out.stdout.strip().splitlines()[0]) * 1024 ** 2
    except Exception:
        pass
    return None


def system_ram_bytes():
    try:
        import psutil
        vm = psutil.virtual_memory()
        return vm.total, vm.available
    except Exception:
        return None, None


def check(path, budget_gb=2.8):
    GB = 1024.0 ** 3
    n = header_point_count(path)
    need = n * BYTES_PER_POINT
    vram = _nvidia_vram_bytes()
    total_ram, avail_ram = system_ram_bytes()

    print("[LARGE DATASET READINESS]", flush=True)
    print(f"  File:            {os.path.basename(path)}")
    print(f"  Points (header): {n:,}")
    print(f"  VRAM required:   {need / GB:,.2f} GB   ({BYTES_PER_POINT:.0f} B/point)")
    if vram:
        print(f"  VRAM available:  {vram / GB:,.2f} GB")
    print(f"  Point budget:    {budget_gb:,.2f} GB")
    if total_ram and avail_ram:
        print(f"  RAM available:   {avail_ram / GB:,.1f} GB of {total_ram / GB:,.1f} GB")

    fits = need <= budget_gb * GB
    print(f"  Max resident:    {int((budget_gb * GB) / BYTES_PER_POINT):,} points")
    if fits:
        print("  VERDICT:         FITS - safe to load")
    else:
        print("  VERDICT:         TOO LARGE for full residency on this GPU")
        print(f"                  exceeds budget by {(need - budget_gb * GB) / GB:,.2f} GB")
        print("                  Suggest: streaming mode (keep visible tiles only)")
    return fits


def main():
    args = sys.argv[1:] or [r"H:\TESTING CONTIUES\FUNIVIA\NT219\MANDI_56.laz"]
    rc = 0
    for p in args:
        if not os.path.isfile(p):
            print(f"[LARGE DATASET READINESS] not found: {p}")
            rc = 1
            continue
        if not check(p):
            rc = 2
        print()
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
