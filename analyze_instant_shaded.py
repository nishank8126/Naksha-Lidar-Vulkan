"""analyze_instant_shaded.py - deterministic acceptance analyzer for an
Instant Shaded user-test session.

    python analyze_instant_shaded.py                 # latest completed session
    python analyze_instant_shaded.py "<log>.jsonl"   # explicit path always wins

It reads a JSONL telemetry log AFTER the fact and turns it into one verdict.

CORE RULE
=========
The renderer result and the telemetry health are separate verdicts. A missing,
dropped or failed telemetry field can NEVER turn a working renderer into FAIL;
at worst it produces WARN (incomplete evidence). Only an authoritatively
measured violation of a hard acceptance requirement produces FAIL.

NULL vs 0
=========
``0``   = measured, and nothing happened.
``null``= counter unavailable on this path.
A null is NOT read as non-zero, as a failure, or as "an upload happened".

EXIT CODES
==========
    0  PASS    all mandatory renderer requirements positively verified
    1  FAIL    a hard acceptance requirement is positively violated
    2  WARN    renderer looks good but required evidence is unavailable
    3  INPUT   analyzer / schema / input error (nothing was concluded)
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

SCHEMA_VERSION = 1

EXIT_PASS, EXIT_FAIL, EXIT_WARN, EXIT_INPUT_ERROR = 0, 1, 2, 3

PASS, WARN, FAIL, NOT_TESTED = "PASS", "WARN", "FAIL", "NOT_TESTED"

# A WARM switch is the acceptance-critical one: everything resident, no
# preparation. Counters required to be exactly 0 there.
WARM_ZERO_COUNTERS = (
    "delaunay_delta",
    "surface_rebuild_delta",
    "topology_rebuild_delta",
    "xyz_upload_delta",
    "full_recolor_delta",
    "normal_upload_delta",
    "mesh_upload_delta",
)

SECTION_NAMES = (
    "renderer_handoff", "geometry_purity", "normal_residency",
    "palette_lut", "lighting", "performance", "stability", "telemetry_health",
)


class AnalyzerError(Exception):
    """Input/schema problem -> exit 3. Never a renderer verdict."""


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def resolve_log_path(explicit: Optional[str], root: Optional[Path] = None) -> Path:
    if explicit:
        return Path(explicit).expanduser()
    root = root or Path(__file__).resolve().parent
    log_dir = root / "logs" / "instant_shaded"
    pointer = log_dir / "latest.txt"
    if pointer.is_file():
        try:
            text = pointer.read_text(encoding="utf-8").strip()
            if text and Path(text).is_file():
                return Path(text)
        except OSError:
            pass
    logs = sorted(log_dir.glob("instant_shaded_*.jsonl"))
    if not logs:
        raise AnalyzerError(
            f"no session log found. Looked in {log_dir}. "
            "Run 'python instant_shaded_user_test.py' first, or pass a path.")
    # Deterministic: newest by mtime, then by name so ties are stable.
    return sorted(logs, key=lambda p: (p.stat().st_mtime, p.name))[-1]


def load_events(path: Path) -> List[Dict[str, Any]]:
    if not path.is_file():
        raise AnalyzerError(f"log file not found: {path}")
    events: List[Dict[str, Any]] = []
    bad_lines = 0
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise AnalyzerError(f"cannot read {path}: {exc}") from exc
    for lineno, line in enumerate(raw.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            bad_lines += 1
            continue
        if not isinstance(obj, dict):
            bad_lines += 1
            continue
        events.append(obj)
    if not events:
        raise AnalyzerError(f"no JSON events found in {path}")
    versions = {e.get("schema_version") for e in events}
    unsupported = sorted(v for v in versions if v != SCHEMA_VERSION)
    if unsupported:
        raise AnalyzerError(
            f"unsupported schema_version {unsupported}; this analyzer reads "
            f"schema_version {SCHEMA_VERSION} only. Refusing to parse an "
            "incompatible format.")
    return events, bad_lines  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Small helpers - deliberately null-aware
# ---------------------------------------------------------------------------

def as_num(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def is_warm(switch: Dict[str, Any]) -> bool:
    """A switch is WARM when its preparation is already done. Cold preparation
    (first-time resource/normal work) must never be scored as recurring
    mode-switch work."""
    if switch.get("cold") is True:
        return False
    resident = switch.get("normal_resident")
    if resident is None:
        return switch.get("cold") is False
    return bool(resident)


def percentile(values: List[float], q: float) -> Optional[float]:
    """Deterministic linear-interpolation percentile; None when empty."""
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return round(ordered[0], 4)
    pos = (len(ordered) - 1) * q
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return round(ordered[lo], 4)
    frac = pos - lo
    return round(ordered[lo] * (1.0 - frac) + ordered[hi] * frac, 4)


def median(values: List[float]) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    return round(ordered[mid] if n % 2 else (ordered[mid - 1] + ordered[mid]) / 2.0, 4)


def _agg(samples: List[float]) -> Dict[str, Any]:
    """Frame-timing aggregates. Reports INSUFFICIENT DATA rather than
    inventing values."""
    if len(samples) < 2:
        return {"status": "INSUFFICIENT DATA", "samples": len(samples)}
    med_ms = median(samples)
    p95_ms = percentile(samples, 0.95)
    p99_ms = percentile(samples, 0.99)
    fps = [(1000.0 / s) for s in samples if s > 0]
    med_fps = median(fps)
    one_pct_low = percentile(fps, 0.01)
    return {
        "status": "OK",
        "samples": len(samples),
        "median_frame_ms": med_ms,
        "p95_frame_ms": p95_ms,
        "p99_frame_ms": p99_ms,
        "median_fps": med_fps,
        "fps_1pct_low": one_pct_low,
        "frames_over_33_3ms": sum(1 for s in samples if s > 33.3),
        "frames_over_50ms": sum(1 for s in samples if s > 50.0),
        "frames_over_100ms": sum(1 for s in samples if s > 100.0),
    }


# ---------------------------------------------------------------------------
# Section analysis
# ---------------------------------------------------------------------------

def _analyze(events: List[Dict[str, Any]], bad_lines: int) -> Dict[str, Any]:
    sections: Dict[str, str] = {name: NOT_TESTED for name in SECTION_NAMES}
    warnings: List[str] = []
    failures: List[str] = []
    metrics: Dict[str, Any] = {}

    by_event: Dict[str, List[Dict[str, Any]]] = {}
    for e in events:
        by_event.setdefault(str(e.get("event")), []).append(e)

    switches = by_event.get("instant_shaded_switch", [])
    warm = [s for s in switches if is_warm(s)]
    cold = [s for s in switches if not is_warm(s)]
    ok_switches = [s for s in switches if s.get("operation_success") is True]

    session_id = next((str(e.get("session_id")) for e in events
                       if e.get("session_id")), "UNKNOWN")
    dataset = next((e.get("dataset_path") for e in events
                    if e.get("dataset_path")), None)
    point_count = next((as_num(e.get("point_count")) for e in events
                        if e.get("point_count") is not None), None)
    metrics.update({
        "session_id": session_id, "dataset_path": dataset,
        "point_count": point_count, "instant_switches": len(switches),
        "warm_switches": len(warm), "cold_switches": len(cold),
    })

    # ---- RENDERER_HANDOFF ---------------------------------------------
    if not switches:
        sections["renderer_handoff"] = FAIL
        failures.append("RENDERER_HANDOFF: no instant_shaded_switch event - no "
                        "Instant Shaded activation was ever recorded")
    else:
        failed = [s for s in switches if s.get("operation_success") is False]
        legacy = [s for s in ok_switches if s.get("renderer") != "instant_shaded"]
        fallback = [e for e in events if e.get("fallback_triggered") is True]
        if not ok_switches:
            sections["renderer_handoff"] = FAIL
            failures.append("RENDERER_HANDOFF: every instant_shaded_switch "
                            "reported operation_success=false")
        elif failed:
            sections["renderer_handoff"] = FAIL
            failures.append(f"RENDERER_HANDOFF: {len(failed)} instant handoff(s) "
                            "reported operation_success=false")
        elif fallback:
            sections["renderer_handoff"] = FAIL
            failures.append("RENDERER_HANDOFF: renderer fell back away from "
                            "Instant Shaded")
        elif legacy:
            sections["renderer_handoff"] = FAIL
            failures.append("RENDERER_HANDOFF: a successful switch reported a "
                            "non-instant renderer")
        else:
            sections["renderer_handoff"] = PASS

    # ---- GEOMETRY_PURITY (warm switches only) --------------------------
    if not warm:
        sections["geometry_purity"] = NOT_TESTED
        warnings.append("GEOMETRY_PURITY: no WARM instant switch recorded")
    else:
        violations, unavailable = [], []
        for name in WARM_ZERO_COUNTERS:
            for s in warm:
                value = as_num(s.get(name))
                if value is None:
                    unavailable.append(name)
                elif value > 0:
                    violations.append(f"{name}={value:g}")
        metrics["warm_counter_violations"] = sorted(set(violations))
        metrics["warm_counters_unavailable"] = sorted(set(unavailable))
        if violations:
            sections["geometry_purity"] = FAIL
            failures.append("GEOMETRY_PURITY: warm switch reported non-zero "
                            + ", ".join(sorted(set(violations))))
        elif unavailable:
            sections["geometry_purity"] = WARN
            warnings.append("GEOMETRY_PURITY: observed gates passed but evidence "
                            "incomplete for " + ", ".join(sorted(set(unavailable))))
        else:
            sections["geometry_purity"] = PASS

    # ---- NORMAL_RESIDENCY ----------------------------------------------
    cache_events = by_event.get("normal_cache_status", [])
    metrics["normal_cache_state"] = (cache_events[-1].get("state")
                                     if cache_events else "UNKNOWN")
    metrics["normal_represent"] = next(
        (e.get("normal_represent") for e in events if e.get("normal_represent")),
        None)
    warm_normal = [as_num(s.get("normal_upload_delta")) for s in warm]
    measured = [v for v in warm_normal if v is not None]
    metrics["normal_upload_delta_max"] = max(measured) if measured else None
    if switches:
        if measured and max(measured) > 0:
            sections["normal_residency"] = FAIL
            failures.append("NORMAL_RESIDENCY: normal upload during a warm switch")
        elif warm_normal and len(measured) == len(warm_normal):
            sections["normal_residency"] = PASS
        else:
            sections["normal_residency"] = WARN
            warnings.append("NORMAL_RESIDENCY: warm normal-upload counter "
                            "unavailable (null is not a failure)")
    else:
        sections["normal_residency"] = NOT_TESTED

    # ---- PALETTE_LUT / LIGHTING (optional evidence) --------------------
    lut_events = by_event.get("class_lut_update", [])
    if lut_events:
        sizes = [as_num(e.get("lut_upload_bytes")) for e in lut_events]
        known = [s for s in sizes if s is not None]
        metrics["class_lut_updates"] = len(lut_events)
        metrics["lut_upload_bytes_max"] = max(known) if known else None
        moved = [e for e in lut_events
                 if (as_num(e.get("xyz_upload_delta")) or 0) > 0]
        if moved:
            sections["palette_lut"] = FAIL
            failures.append("PALETTE_LUT: a palette update moved the XYZ counter")
        elif not known:
            sections["palette_lut"] = WARN
            warnings.append("PALETTE_LUT: lut_upload_bytes unavailable")
        else:
            sections["palette_lut"] = PASS
    else:
        sections["palette_lut"] = NOT_TESTED

    light_events = by_event.get("lighting_update", [])
    if light_events:
        metrics["lighting_updates"] = len(light_events)
        moved = [e for e in light_events
                 if (as_num(e.get("xyz_upload_delta")) or 0) > 0]
        sections["lighting"] = FAIL if moved else PASS
        if moved:
            failures.append("LIGHTING: a lighting update moved the XYZ counter")
    else:
        sections["lighting"] = NOT_TESTED

    # ---- PERFORMANCE ---------------------------------------------------
    frame_ms = [as_num(e.get("frame_ms")) for e in
                by_event.get("instant_shaded_frame", [])]
    frame_ms = [f for f in frame_ms if f is not None and f > 0]
    perf = _agg(frame_ms)
    metrics["performance"] = perf
    if perf["status"] != "OK":
        sections["performance"] = WARN
        warnings.append("PERFORMANCE: insufficient frame samples - no values "
                        "invented")
    elif perf["median_fps"] is not None and perf["median_fps"] < 30.0:
        # Slow FPS is a PERFORMANCE warning, never an architecture failure.
        sections["performance"] = WARN
        warnings.append(f"PERFORMANCE: median {perf['median_fps']:.1f} FPS below "
                        "30 - reported separately from architecture")
    else:
        sections["performance"] = PASS

    # ---- STABILITY -----------------------------------------------------
    fatal = [e for e in events
             if e.get("fatal") is True
             or str(e.get("kind", "")).lower() in ("vulkan_validation_error",
                                                   "device_lost", "fatal")]
    shutdown = by_event.get("application_shutdown", [])
    exit_code = as_num(shutdown[-1].get("exit_code")) if shutdown else None
    crashed = exit_code is not None and exit_code not in (0, None)
    metrics["exit_code"] = exit_code
    metrics["fatal_events"] = len(fatal)
    metrics["crashed"] = bool(crashed)
    if fatal:
        sections["stability"] = FAIL
        failures.append(f"STABILITY: {len(fatal)} fatal/validation event(s)")
    elif crashed:
        sections["stability"] = FAIL
        failures.append(f"STABILITY: abnormal termination, exit_code={exit_code:g}")
    elif shutdown:
        sections["stability"] = PASS
    else:
        sections["stability"] = WARN
        warnings.append("STABILITY: no application_shutdown event (log may have "
                        "been cut short)")

    # ---- TELEMETRY_HEALTH (never changes the renderer verdict) ---------
    if bad_lines:
        sections["telemetry_health"] = WARN
        warnings.append(f"TELEMETRY_HEALTH: {bad_lines} unparseable line(s)")
    elif not by_event.get("session_end"):
        sections["telemetry_health"] = WARN
        warnings.append("TELEMETRY_HEALTH: no session_end event; session ended "
                        "without closing the log")
    else:
        sections["telemetry_health"] = PASS

    # ---- OVERALL (precedence: any FAIL -> FAIL, any WARN -> WARN) -------
    if any(sections[n] == FAIL for n in SECTION_NAMES):
        overall = FAIL
    elif any(sections[n] == WARN for n in SECTION_NAMES):
        overall = WARN
    else:
        overall = PASS

    return {
        "schema_version": SCHEMA_VERSION,
        "session_id": session_id,
        "overall": overall,
        "sections": sections,
        "counts": {
            "events": len(events), "bad_lines": bad_lines,
            "instant_switches": len(switches), "warm_switches": len(warm),
            "cold_switches": len(cold), "class_lut_updates": len(lut_events),
            "lighting_updates": len(light_events), "frame_samples": len(frame_ms),
        },
        "metrics": metrics,
        "warnings": warnings,
        "failures": failures,
    }


# ---------------------------------------------------------------------------
# Human-readable report
# ---------------------------------------------------------------------------

def _fmt(value: Any, default: str = "null") -> str:
    if value is None:
        return default
    if isinstance(value, bool):
        return "YES" if value else "NO"
    if isinstance(value, float):
        return f"{value:,.2f}".rstrip("0").rstrip(".")
    if isinstance(value, int):
        return f"{value:,}"
    return str(value)


def _violated(result: Dict[str, Any], counter: str) -> bool:
    for entry in result["metrics"].get("warm_counter_violations") or []:
        if entry.split("=")[0] == counter:
            return True
    return False


def render_report(result: Dict[str, Any], log_path: Path) -> str:
    metrics = result["metrics"]
    counts = result["counts"]
    sections = result["sections"]
    lines: List[str] = []
    add = lines.append
    add("=" * 60)
    add("NAKSHA INSTANT SHADED ACCEPTANCE")
    add("=" * 60)
    add(f"Session:  {result['session_id']}")
    add(f"Log:      {log_path}")
    add(f"Dataset:  {metrics.get('dataset_path') or 'null'}")
    add(f"Points:   {_fmt(metrics.get('point_count'))}")
    add(f"Normals:  {metrics.get('normal_represent') or 'null'}")
    add("")

    titles = [
        ("renderer_handoff", "RENDERER HANDOFF"),
        ("geometry_purity", "GEOMETRY PURITY"),
        ("normal_residency", "NORMAL RESIDENCY"),
        ("palette_lut", "PALETTE LUT"),
        ("lighting", "LIGHTING"),
        ("performance", "PERFORMANCE"),
        ("stability", "STABILITY"),
        ("telemetry_health", "TELEMETRY HEALTH"),
    ]
    for key, title in titles:
        add(title)
        add(sections[key])
        if key == "renderer_handoff":
            add("")
            add(f"Instant switches: {counts['instant_switches']}")
            add(f"Warm switches:    {counts['warm_switches']}")
            add(f"Cold switches:    {counts['cold_switches']}")
        elif key == "geometry_purity":
            add("")
            for counter in WARM_ZERO_COUNTERS:
                add(f"{counter}: "
                    + ("VIOLATED" if _violated(result, counter) else "0 (observed)"))
            unavailable = sorted(set(metrics.get("warm_counters_unavailable") or []))
            if unavailable:
                add(f"unavailable (null): {', '.join(unavailable)}")
        elif key == "normal_residency":
            add("")
            add(f"Cache: {metrics.get('normal_cache_state')}")
            add("Warm normal uploads: "
                + _fmt(metrics.get("normal_upload_delta_max"), "0 (observed)"))
        elif key == "performance":
            perf = metrics.get("performance", {})
            add("")
            add(f"Samples: {perf.get('samples', 0)}")
            if perf.get("status") == "OK":
                add(f"Median frame: {_fmt(perf.get('median_frame_ms'))} ms")
                add(f"P95 frame:    {_fmt(perf.get('p95_frame_ms'))} ms")
                add(f"P99 frame:    {_fmt(perf.get('p99_frame_ms'))} ms")
                add(f"Median FPS:   {_fmt(perf.get('median_fps'))}")
                add(f"1% low FPS:   {_fmt(perf.get('fps_1pct_low'))}")
                add(f"frames >33.3ms: {perf.get('frames_over_33_3ms')}")
                add(f"frames >50ms:  {perf.get('frames_over_50ms')}")
                add(f"frames >100ms: {perf.get('frames_over_100ms')}")
            else:
                add("PERFORMANCE: INSUFFICIENT DATA")
        elif key == "stability":
            add("")
            add(f"Crash: {_fmt(metrics.get('crashed'))}")
            add(f"Exit code: {_fmt(metrics.get('exit_code'))}")
            add(f"Fatal Vulkan errors: {metrics.get('fatal_events', 0)}")
        add("")

    add("-" * 60)
    add("OVERALL")
    add("-" * 60)
    add(result["overall"])
    add("")
    for failure in result["failures"]:
        add(f"FAIL  {failure}")
    for warning in result["warnings"]:
        add(f"WARN  {warning}")
    if not result["failures"] and not result["warnings"]:
        add("No failures, no warnings.")
    add("")
    add("Renderer result and telemetry health are separate verdicts: a missing or")
    add("failed telemetry field can never turn a working renderer into FAIL.")
    add("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def analyze(log_path_arg: Optional[str] = None,
            write_summary: bool = True,
            root: Optional[Path] = None) -> Tuple[Dict[str, Any], Path, int]:
    """Reads the log AFTER the fact and returns (result, log_path, exit_code).

    Deterministic: no wall-clock, no application state and no filesystem
    ordering beyond an explicit latest.txt / name sort enters the verdict, so
    the same log always produces the same result. ``root`` only relocates the
    default log directory (used by tests); it is not consulted when an explicit
    log path is given.
    """
    path = resolve_log_path(log_path_arg, root)
    events, bad_lines = load_events(path)
    result = _analyze(events, bad_lines)
    if write_summary:
        summary = path.with_name(path.stem + "_summary.json")
        try:
            summary.write_text(json.dumps(result, indent=2, sort_keys=True,
                                          default=str), encoding="utf-8")
        except OSError:
            pass
    exit_code = {PASS: EXIT_PASS, FAIL: EXIT_FAIL, WARN: EXIT_WARN}[result["overall"]]
    return result, path, exit_code


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Analyze an Instant Shaded user-test telemetry session.")
    parser.add_argument("log", nargs="?",
                        help="session .jsonl (default: last completed session)")
    args = parser.parse_args(argv)
    try:
        result, path, exit_code = analyze(args.log)
    except AnalyzerError as exc:
        print(f"ANALYZER ERROR: {exc}", file=sys.stderr)
        return EXIT_INPUT_ERROR
    print(render_report(result, path))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())

