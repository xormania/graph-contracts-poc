#!/usr/bin/env python3
"""Require selected workflow tests to reject consequential deliberate faults.

This edits only scripts/poc.py temporarily, restores its exact original bytes in
finally, and retains each healthy control and faulty-run log. A syntax/import
failure, skipped test, missing test, timeout, or surviving fault is not red proof.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "scripts" / "poc.py"
DEADLINE = 90

FAULTS = [
    {
        "id": "ignore_expected_outcome_mismatch",
        "pattern": r"if actual is MISSING or not json_equal\(actual, expected\):",
        "replacement": "if False:  # deliberate fault: ignore observed contradictions",
        "test": "test_outcome_matrix_preserves_domain_and_execution_distinctions",
    },
    {
        "id": "accept_nonzero_provider_exit",
        "pattern": r'elif observed\["problem"\] or observed\["exit_code"\] != 0:',
        "replacement": 'elif observed["problem"]:  # deliberate fault: ignore nonzero exit',
        "test": "test_outcome_matrix_preserves_domain_and_execution_distinctions",
    },
    {
        "id": "trust_tampered_request_or_result",
        "pattern": r"if digest\(content\) != case\.get\(hash_key\):",
        "replacement": "if False:  # deliberate fault: skip saved artifact identity",
        "test": "test_repeat_rejects_tampered_saved_result",
    },
]


def run_test(test: str, log: Path) -> dict:
    command = [
        sys.executable, "-m", "unittest", "discover", "-s", "tests",
        "-p", "test_poc_workflow.py", "-k", test, "-v",
    ]
    started = time.monotonic()
    timed_out = False
    with log.open("wb") as output:
        process = subprocess.Popen(
            command, cwd=ROOT, stdout=output, stderr=subprocess.STDOUT,
            start_new_session=os.name == "posix",
        )
        try:
            process.wait(timeout=DEADLINE)
        except subprocess.TimeoutExpired:
            timed_out = True
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
            process.wait(timeout=10)
    text = log.read_text(encoding="utf-8", errors="replace")
    return {
        "returncode": process.returncode,
        "timed_out": timed_out,
        "seconds": round(time.monotonic() - started, 3),
        "log": str(log),
        "output": text,
    }


def executed_one(result: dict, test: str) -> bool:
    output = result["output"]
    return (
        not result["timed_out"]
        and re.search(r"Ran 1 test in ", output) is not None
        and test in output
        and "skipped=" not in output
        and "SyntaxError" not in output
        and "IndentationError" not in output
        and "ImportError" not in output
        and "ModuleNotFoundError" not in output
    )


def public_result(result: dict) -> dict:
    return {key: value for key, value in result.items() if key != "output"}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "target" / "prove-poc-red")
    arguments = parser.parse_args(argv)
    directory = arguments.output.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    original = SOURCE.read_bytes()
    text = original.decode("utf-8")
    outcomes = []
    manifest = {
        "schema_version": 1,
        "source": str(SOURCE.relative_to(ROOT)),
        "original_sha256": hashlib.sha256(original).hexdigest(),
        "status": "failed",
        "faults": outcomes,
        "restored": False,
    }
    failure = None
    try:
        for fault in FAULTS:
            changed, matches = re.subn(fault["pattern"], fault["replacement"], text)
            if matches != 1:
                raise RuntimeError(f"{fault['id']}: expected exactly one mutation site, found {matches}")
            ast.parse(changed, filename=str(SOURCE))
            healthy = run_test(fault["test"], directory / f"{fault['id']}.healthy.log")
            item = {
                "id": fault["id"],
                "test": fault["test"],
                "healthy": public_result(healthy),
                "status": "failed",
            }
            outcomes.append(item)
            if healthy["returncode"] != 0 or not executed_one(healthy, fault["test"]):
                raise RuntimeError(f"{fault['id']}: healthy control did not execute and pass")
            try:
                SOURCE.write_bytes(changed.encode("utf-8"))
                faulty = run_test(fault["test"], directory / f"{fault['id']}.faulty.log")
            finally:
                SOURCE.write_bytes(original)
            item["faulty"] = public_result(faulty)
            output = faulty["output"]
            if not (
                faulty["returncode"] != 0
                and executed_one(faulty, fault["test"])
                and "AssertionError" in output
                and re.search(r"^FAIL: " + re.escape(fault["test"]), output, re.MULTILINE)
                and re.search(r"^ERROR: ", output, re.MULTILINE) is None
            ):
                raise RuntimeError(f"{fault['id']}: fault was not rejected by the intended assertion")
            item["status"] = "detected_by_assertion"
            print(f"DETECTED {fault['id']}: healthy control passed, faulty assertion failed", flush=True)
        manifest["status"] = "passed"
    except (OSError, RuntimeError, SyntaxError, subprocess.SubprocessError) as error:
        failure = str(error)
        manifest["error"] = failure
    finally:
        SOURCE.write_bytes(original)
        manifest["restored"] = SOURCE.read_bytes() == original
        (directory / "manifest.json").write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
        )
    if failure:
        print(json.dumps({"status": "failed", "error": failure, "manifest": str(directory / "manifest.json")}),
              file=sys.stderr)
        return 1
    print(json.dumps({"status": "passed", "manifest": str(directory / "manifest.json")}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
