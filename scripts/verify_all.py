"""Run the current test suite and compare failure names with a JUnit baseline.

Usage: python scripts/verify_all.py --baseline diagnostics/task_baseline_runnable.xml
Collection errors remain separate from functional failures. A passing unit
suite does not certify Vulkan visual parity; use the GUI acceptance harness too.
"""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]


def read_results(path):
    cases = ET.parse(path).getroot().iter("testcase")
    result = dict(passed=0, failed=0, skipped=0, collection_errors=0,
                  failure_names=[], collection_error_names=[])
    for case in cases:
        name = case.get("classname", "") + "::" + case.get("name", "")
        if case.find("error") is not None:
            result["collection_errors"] += 1
            result["collection_error_names"].append(name)
        elif case.find("failure") is not None:
            result["failed"] += 1
            result["failure_names"].append(name)
        elif case.find("skipped") is not None:
            result["skipped"] += 1
        else:
            result["passed"] += 1
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--output", type=Path, default=ROOT / "diagnostics/task_final")
    parser.add_argument("targets", nargs="*", default=["tests"])
    args = parser.parse_args()
    prefix = args.output.resolve()
    prefix.parent.mkdir(parents=True, exist_ok=True)
    xml = prefix.with_suffix(".xml")
    with prefix.with_suffix(".log").open("w", encoding="utf-8") as log:
        process = subprocess.run(
            [sys.executable, "-m", "pytest", *args.targets, "-q",
             "--continue-on-collection-errors", "--tb=short", f"--junitxml={xml}"],
            cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
    if not xml.is_file():
        print(f"pytest returned {process.returncode} without a result: {prefix}.log")
        return 2
    result = read_results(xml)
    result["pytest_exit_code"] = process.returncode
    if args.baseline:
        before = read_results(args.baseline)
        result["baseline"] = before
        result["new_functional_failures"] = sorted(
            set(result["failure_names"]) - set(before["failure_names"]))
        result["resolved_functional_failures"] = sorted(
            set(before["failure_names"]) - set(result["failure_names"]))
        result["new_collection_errors"] = sorted(
            set(result["collection_error_names"]) - set(before["collection_error_names"]))
    prefix.with_suffix(".json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))
    return process.returncode


if __name__ == "__main__":
    raise SystemExit(main())
