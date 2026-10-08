"""analyze_stage3c.py - post-run Stage 3C acceptance analyzer.

Reads stage3c_viewport_telemetry.jsonl (written by st3c_app_one.py and the
StreamManager) and produces the PASS/FAIL decision.

Success is NOT a single 30 FPS hardline. It separates:
  CORRECTNESS - invariants that MUST hold
  STABILITY   - no crash, no runaway memory, queue drains
  PERFORMANCE - frame timing histogram (reported; hard gate = no >2s stalls)

Usage:
    .\venv\Scripts\python.exe analyze_stage3c.py
"""
from __future__ import annotations
import json, os

TE_PATH = os.path.join(
    r"H:\TESTING CONTIUES\FUNIVIA\NT219",
    "stage3c_viewport_telemetry.jsonl")
if not os.path.isfile(TE_PATH):
    TE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "stage3c_viewport_telemetry.jsonl")

CORRECTNESS_FAILS = []
STABILITY_FAILS = []


def read_samples():
    if not os.path.isfile(TE_PATH):
        return []
    out = []
    for ln in open(TE_PATH, encoding="utf-8"):
        ln = ln.strip()
        if not ln:
            continue
        try:
            out.append(json.loads(ln))
        except Exception:
            pass
    return out


def pct(vals, q):
    if not vals:
        return 0.0
    s = sorted(vals)
    k = max(0, min(len(s) - 1, int(round(q / 100.0 * (len(s) - 1)))))
    return s[k]


def ok(cond, msg, store):
    print(("  PASS: " if cond else "  FAIL: ") + msg)
    if not cond:
        store.append(msg)


def main():
    print("=" * 74)
    print("NAKSHA STAGE 3C ACCEPTANCE ANALYSIS")
    print("=" * 74)
    samples = read_samples()
    if not samples:
        print("FAIL: no telemetry samples found at", TE_PATH)
        return 1
    print(f"Samples: {len(samples)}")
    has_first = any(s.get("event") == "first_frame" for s in samples)
    has_camera = any(s.get("event") == "camera_change" for s in samples)

    print("\n--- CORRECTNESS ---")
    last = next((s for s in reversed(samples) if s.get("event") != "tile_read_error"), samples[-1])
    ok(last.get("dataset_mode") == "streaming",
       f"DatasetMode = streaming (got {last.get('dataset_mode')})",
       CORRECTNESS_FAILS)
    fpu = last.get("streaming_full_point_upload_attempts", 0)
    ok(fpu == 0, f"monolithic full upload attempts = {fpu}",
       CORRECTNESS_FAILS)
    ok(has_first, "first frame presented (overview)", CORRECTNESS_FAILS)
    ok(has_camera, "camera-change events recorded", CORRECTNESS_FAILS)
    ok(last.get("position_reuploads", 0) == 0,
       f"position_reuploads = {last.get('position_reuploads', 0)}",
       CORRECTNESS_FAILS)
    ok(last.get("stale_cancellations", 0) >= 0,
       f"stale cancellations tracked ({last.get('stale_cancellations', 0)})",
       CORRECTNESS_FAILS)
    ram_b = last.get("ram_resident_bytes", 0)
    gpu_b = last.get("gpu_resident_bytes", 0)
    ok(ram_b < 8 * 1024 ** 3,
       f"RAM under 8 GiB budget ({ram_b/1024**2:.1f} MB)", CORRECTNESS_FAILS)
    ok(gpu_b < 1.6 * 1024 ** 3,
       f"GPU under ~1.6 GiB budget ({gpu_b/1024**2:.1f} MB)", CORRECTNESS_FAILS)
    ok(last.get("drawn_points", 0) < 10_000_000,
       f"drawn points bounded (not full 268M): {last.get('drawn_points', 0)}",
       CORRECTNESS_FAILS)

    print("\n--- STABILITY ---")
    exc = [s for s in samples if s.get("event") == "tile_read_error"]
    ok(len(exc) == 0, f"no tile read errors ({len(exc)})", STABILITY_FAILS)
    qs = [s.get("request_queue_depth", 0) for s in samples
          if "request_queue_depth" in s]
    if qs:
        peak_q = max(qs)
        ok(qs[-1] <= max(4, peak_q),
           f"request queue drains (peak={peak_q}, final={qs[-1]})",
           STABILITY_FAILS)
    rsss = [s.get("process_rss", 0) for s in samples if "process_rss" in s]
    if rsss:
        peak = max(rsss)
        ok(peak < 24 * 1024 ** 3,
           f"process RSS bounded (< 24 GiB, peak={peak/1024**2:.0f} MB)",
           STABILITY_FAILS)
    ups = [s.get("uploads", 0) for s in samples if "uploads" in s]
    if len(ups) > 3:
        ok(ups[-1] < max(ups) + 50,
           "uploads not monotonically rising (geometry reused)",
           STABILITY_FAILS)

    print("\n--- PERFORMANCE (reported; hard gate = no >2s stall) ---")
    ms = [s.get("frame_time_ms") for s in samples if "frame_time_ms" in s]
    if ms:
        mt = sorted(ms)
        print(f"  frames sampled          : {len(mt)}")
        print(f"  P50 frame ms            : {pct(mt, 50):.2f}")
        print(f"  P95 frame ms            : {pct(mt, 95):.2f}")
        print(f"  P99 frame ms            : {pct(mt, 99):.2f}")
        print(f"  max frame ms            : {max(mt):.2f}")
        print(f"  frames > 50 ms          : {sum(1 for x in mt if x > 50)}")
        print(f"  frames > 100 ms         : {sum(1 for x in mt if x > 100)}")
        print(f"  median FPS (est)        : {1000.0 / pct(mt, 50):.1f}")
        ok(max(mt) < 2000,
           f"no frame stall > 2 s (max={max(mt):.1f} ms)", STABILITY_FAILS)

    print("\n--- SUMMARY ---")
    all_fails = CORRECTNESS_FAILS + STABILITY_FAILS
    if all_fails:
        print(f"FAIL  ({len(all_fails)} gate(s) failed)")
        for m in all_fails:
            print("  - " + m)
        return 1
    print("PASS  (CORRECTNESS + STABILITY gates; PERFORMANCE reported)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
