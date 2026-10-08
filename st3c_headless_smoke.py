"""st3c_headless_smoke.py - headless acceptance of the Stage 3C streaming path.

Runs the FULL computable pipeline against the real MANDI_56 cache using the
HeadlessTileRenderer adapter -- NO GPU / NO PySide6/Vulkan required:
cache-first open -> DatasetMode.STREAMING -> overview first frame ->
hierarchy query -> screen-space LOD -> async NKPC tile read ->
selective SoA decode -> RAM cache -> draw-range computation -> telemetry.

Asserts every Stage 3C hard gate:
* source LAZ body never decoded
* no monolithic upload (streaming_full_point_upload_attempts == 0)
* position reuploads == 0 (camera-only frame uploads nothing)
* app.data never materializes the full point set
* RAM/GPU budgets respected; never-blank (overview present first)
"""
import os, sys, time, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
from gui.naksha_cache.dataset_mode import cache_first_open, DatasetMode
from gui.naksha_cache.reader import NakshaPointCacheReader
from gui.naksha_cache.stream_telemetry import StreamTelemetry
from gui.naksha_cache.stream_renderer_adapter import HeadlessTileRenderer
from gui.naksha_cache.stream_manager import NakshaStreamManager
from naksha_lod_gate import Camera2D

SRC = r"H:\TESTING CONTIUES\FUNIVIA\NT219\MANDI_56.laz"
TE_PATH = os.path.join(os.path.dirname(SRC), "stage3c_viewport_telemetry.jsonl")
fails = []


def check(cond, msg):
    print(("  PASS: " if cond else "  FAIL: ") + msg)
    if not cond:
        fails.append(msg)


class App:
    pass


def main():
    print("=" * 74)
    print("[STAGE 3C HEADLESS SMOKE] real cache, no GPU")
    v = cache_first_open(SRC)
    check(v.ok, f"cache_first_open ok ({v.reason})")
    if not v.ok:
        print("FATAL: cache not valid")
        return 1
    print(f"  source points={v.total_points:,} | NKPC={v.pc_bytes/1024**3:.2f} GiB"
        f" | bounds_min={tuple(round(x,2) for x in v.bounds_min)}")
    reader = NakshaPointCacheReader(SRC, verify_crc=False, load_edits=False)
    check(reader.pc_path.endswith(".laz.nakshapc"),
        f"reader opens ONLY cache: {os.path.basename(reader.pc_path)}")
    check(reader.stats["blocks_read"] == 0 and reader.stats["bytes_read"] == 0,
        "no point-body reads yet (reader idle until tile requested)")
    te = StreamTelemetry(TE_PATH)
    adapter = HeadlessTileRenderer(v.total_points)
    app = App()
    app.dataset_mode = DatasetMode.STREAMING
    app.data = {}
    mgr = NakshaStreamManager(app, reader, adapter, te)
    info = mgr.open_first_frame()
    check(info["overview_block"] == v.overview_block_id,
          f"first paint is overview block #{info['overview_block']}")
    check(info["overview_points"] > 0,
        f"first frame has points ({info['overview_points']:,} pts)")
    check(adapter.uploads == 1,
        f"first-frame upload = overview ({adapter.uploads})")
    check(adapter.full_point_upload_attempts == 0,
        "no monolithic upload on open")
    check(len(app.data) == 0, "app.data stays empty (no full materialization)")
    bmin, bmax = v.bounds_min, v.bounds_max
    cx = (bmin[0] + bmax[0]) / 2.0
    cy = (bmin[1] + bmax[1]) / 2.0
    hw = (bmax[0] - bmin[0]) / 2.0
    hh = (bmax[1] - bmin[1]) / 2.0
    cam = Camera2D(cx, cy, hw, 1920, 1080)
    vp = (cx, cy, hw, hh)
    info = mgr.on_camera_changed(cam, vp, 1)
    check(not info.get("dropped", False), "FIT not dropped (fresh generation)")
    check(info["visible_nodes"] > 0,
        f"FIT sees visible nodes ({info['visible_nodes']})")
    check(info["requested_points"] > 0,
        f"FIT requests points ({info['requested_points']:,})")
    check(adapter.full_point_upload_attempts == 0,
        "no monolithic upload on FIT")
    time.sleep(1.5)
    before = adapter.position_reuploads
    mgr.on_frame(cam, vp)
    check(adapter.position_reuploads == 0, "position-only frame: 0 position reuploads")
    cam2 = Camera2D(cx, cy, hw / 2, 1920, 1080)
    vp2 = (cx, cy, hw / 2, hh / 2)
    info2 = mgr.on_camera_changed(cam2, vp2, 2)
    check(info2["requested_points"] <= info["requested_points"],
        f"x2 zoom requests <= FIT (got {info2['requested_points']:,})")
    check(adapter.full_point_upload_attempts == 0,
        "no monolithic upload on x2")
    lods_report = []
    for i, frac in enumerate([4, 8, 16, 32], start=3):
        c = Camera2D(cx, cy, hw / frac, 1920, 1080)
        vi = (cx, cy, hw / frac, hh / frac)
        inf = mgr.on_camera_changed(c, vi, 10 + i)
        time.sleep(0.6)
        mgr._drain_done()
        lods_report.append((frac, inf.get("requested_points", 0),
                            inf.get("selections", 0)))
    print("  camera ladder:")
    for frac, pts, sel in lods_report:
        print(f"    x{frac}: requested={pts:,} selections={sel}")
    check(os.path.isfile(TE_PATH), "telemetry JSONL written")
    if os.path.isfile(TE_PATH):
        lines = open(TE_PATH, encoding="utf-8").read().splitlines()
        check(len(lines) > 0, f"telemetry has samples ({len(lines)})")
        ev = json.loads(lines[-1])  # check a camera_change/frame sample
        for f in ("dataset_mode", "camera_generation", "drawn_points",
                "ram_resident_bytes", "gpu_resident_bytes",
                "streaming_full_point_upload_attempts",
                "position_reuploads", "request_queue_depth"):
            check(f in ev, f"telemetry field present: {f}")
    st = reader.stats
    check(st["bytes_read"] < v.pc_bytes,
        f"reader bytes_read < NKPC size: {st['bytes_read']:,}"
        f" < {v.pc_bytes:,}")
    check(st["corrupt"] == 0, "no corrupt blocks")
    check(mgr.ram_bytes <= mgr.ram_budget,
        f"RAM under budget ({mgr.ram_bytes/1024**2:.1f} MB)")
    check(adapter.resident_bytes <= mgr.gpu_budget,
        f"GPU under budget ({adapter.resident_bytes/1024**2:.1f} MB)")
    time.sleep(2.0); mgr._drain_done()  # final drain of async reads
    time.sleep(2.0); mgr._drain_done()  # final drain of async reads

    time.sleep(2.0); mgr._drain_done()  # final drain
    mgr.close()
    print("=" * 74)
    if fails:
        print("RESULT: FAIL  (%d checks failed)" % len(fails))
        for f in fails:
            print("  - " + f)
        return 1
    print("RESULT: PASS  (all Stage 3C hard gates verified headlessly)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


