#!/usr/bin/env python
"""instant_shaded_user_test.py - repeatable Instant Shaded USER harness.

    python instant_shaded_user_test.py               # launch, then you test
    python instant_shaded_user_test.py --analyze     # launch, then auto-analyze

It launches the REAL Naksha application - this repository's main.py - with
Instant Shaded DEV telemetry enabled, and prints exactly what to do and what to
run afterwards. It is not a mock, not a separate viewport, and not another
benchmark: the only thing it adds is a unique session id and a telemetry log.

THE RENDERER IS NEVER TOUCHED BY THIS SCRIPT. It only sets environment
variables and launches main.py.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
APP = ROOT / "main.py"
LOG_DIR = ROOT / "logs" / "instant_shaded"
ANALYZER = ROOT / "analyze_instant_shaded.py"

USER_TEST = """
1.  Open test_classified_highprecision.laz
2.  Wait until the Classification view is visible
3.  Zoom to the SAME camera position and leave it there
4.  Capture: Classification                      (reference)
5.  Select Shaded Class, capture FINAL          (the shipped result)
6.  Restart with NAKSHA_SHADED_DEBUG_VIEW=base,
    capture BASE   -> must match step 4's class distribution
7.  Restart with NAKSHA_SHADED_DEBUG_VIEW=normal,
    capture NORMAL -> coherent over roofs/roads?
8.  Restart with NAKSHA_SHADED_DEBUG_VIEW=lighting,
    capture LIGHT -> which surfaces are lit?
9.  Splat sweep (coverage), restarting each time:
        set NAKSHA_SHADED_SPLAT_PX=1.0 / 1.5 / 2.0 / 2.5 / 3.0
    capture each; pick the one with no holes and no blob overlap
10. If NORMAL is per-pixel RGB noise, set
        NAKSHA_SHADED_NORMAL_RADIUS_PX=4  (try 3, 4, 6)
    and re-capture NORMAL
11. Pan ~30 s, zoom in, zoom out to FIT
12. Switch Classification <-> Shaded 10 times
13. Hide/show class 2 (or another visible class)
14. Change one class palette colour
15. Change lighting (azimuth / ambient / sharpness)
16. Switch 2D -> 3D, orbit, return to 2D
17. Resize / maximize / restore
18. Close Naksha normally

ENV KNOBS (all optional, all restart-only):
    NAKSHA_SHADED_DEBUG_VIEW = base | normal | lighting | final
    NAKSHA_SHADED_SPLAT_PX   = 1.0 | 1.5 | 2.0 | 2.5 | 3.0   (0/absent = auto)
    NAKSHA_SHADED_NORMAL_RADIUS_PX = 1..8                          (default 2)
"""


def session_id() -> str:
    return f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{os.getpid()}"


def print_banner(sid: str, log_path: Path) -> None:
    bar = "=" * 60
    print(bar)
    print("NAKSHA INSTANT SHADED USER TEST")
    print(bar)
    print(f"Session:   {sid}")
    print("Renderer:  INSTANT  (NAKSHA_SHADED_RENDERER=instant)")
    print("Telemetry: ENABLED  (structured JSONL, schema v1)")
    print(f"Log:       {log_path}")
    print(f"Application: {APP}")
    print("")
    print("USER TEST:")
    print(USER_TEST)
    print("After closing run:")
    print(f'    python analyze_instant_shaded.py "{log_path}"')
    print(bar)
    print("", flush=True)


def build_env(sid: str, log_dir: Path) -> dict:
    env = dict(os.environ)
    env["NAKSHA_SHADED_RENDERER"] = "instant"
    env["NAKSHA_RENDER_BACKEND"] = env.get("NAKSHA_RENDER_BACKEND", "vulkan")
    env["NAKSHA_TEST_SESSION_ID"] = sid
    env["NAKSHA_TEST_TELEMETRY_DIR"] = str(log_dir)
    # DEV instrumentation must never leak into a normal user session.
    env.pop("NAKSHA_TEST_TELEMETRY_FAIL", None)
    return env


def write_session_start(sid: str, log_path: Path) -> None:
    """Written by the harness itself so the log is never empty, even if the
    application fails before it can start recording."""
    import json
    from datetime import timezone
    record = {
        "schema_version": 1,
        "session_id": sid,
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "event": "session_start",
        "renderer": "harness",
        "application_path": str(APP),
        "python": sys.executable,
        "normal_represent": "screen_space_reconstruction",
    }
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


def run_analyzer(log_path: Path) -> int:
    print("")
    print("=" * 60)
    print("ANALYZING")
    print("=" * 60, flush=True)
    result = subprocess.run(
        [sys.executable, str(ANALYZER), str(log_path)],
        cwd=str(ROOT), check=False)
    return int(result.returncode)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--analyze", action="store_true",
                        help="run the analyzer automatically after the app closes")
    parser.add_argument("--no-launch", action="store_true",
                        help="set up the session log but do not launch the app")
    args = parser.parse_args(argv)

    if not APP.is_file():
        print(f"ERROR: real application not found at {APP}", file=sys.stderr)
        return 3

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    sid = session_id()
    log_path = LOG_DIR / f"instant_shaded_{sid}.jsonl"
    # A stale log from a reused id would silently pollute the evidence.
    if log_path.exists():
        log_path.unlink()
    (LOG_DIR / "latest.txt").write_text(str(log_path), encoding="utf-8")
    write_session_start(sid, log_path)
    print_banner(sid, log_path)

    if args.no_launch:
        return 0

    env = build_env(sid, LOG_DIR)
    print(f"Launching {APP} ... (close Naksha normally when finished)",
          flush=True)
    try:
        proc = subprocess.run([sys.executable, str(APP)], cwd=str(ROOT), env=env,
                              check=False)
    except KeyboardInterrupt:
        print("\nHarness interrupted.", file=sys.stderr)
        return 130
    exit_code = int(proc.returncode)
    print("")
    print(f"Naksha exited with code {exit_code}.")
    print(f"Log: {log_path}")
    print(f'Analyze: python analyze_instant_shaded.py "{log_path}"')

    if args.analyze:
        return run_analyzer(log_path)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
