#!/usr/bin/env python3
"""Calibrate selected behavioral tests with temporary, isolated source faults.

Each healthy control runs before its mutant. A mutant counts as detected only
when the named Rust test runs and fails an assertion; compilation failures,
missing tests, timeouts, and surviving mutants fail this calibration. Original
source bytes are restored in a finally block. This script never changes Git refs.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
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
TIMEOUT_SECONDS = 90


@dataclass(frozen=True)
class Mutation:
    name: str
    path: str
    pattern: str
    replacement: str
    test: str


MUTATIONS = (
    Mutation(
        "output_overlap_is_not_coverage",
        "src/composition.rs",
        r"let\s+output_fits\s*=\s*primitive\.contract\.output\.min\s*>=\s*request\.required\.output\.min\s*&&\s*primitive\.contract\.output\.max\s*<=\s*request\.required\.output\.max\s*;",
        "let output_fits = primitive.contract.output.min <= request.required.output.max && primitive.contract.output.max >= request.required.output.min;",
        "directional_matching_rejects_overlapping_output_guarantees",
    ),
    Mutation(
        "partial_input_claimed_as_complete",
        "src/composition.rs",
        r"let\s+overlap\s*=\s*intersection\(\s*&request\.required\.input\s*,\s*&primitive\.contract\.input\s*,?\s*\)\s*;",
        "let overlap = intersection(&request.required.input, &primitive.contract.input).map(|_| request.required.input.clone());",
        "partial_reuse_retains_exact_remaining_input_cases",
    ),
    Mutation(
        "assumption_polarity_reversed",
        "src/composition.rs",
        r"\.filter\(\s*\|assumption\|\s*!established\.contains\(\s*assumption\.as_str\(\)\s*\)\s*\)",
        ".filter(|assumption| established.contains(assumption.as_str()))",
        "unestablished_assumptions_do_not_count_as_coverage",
    ),
    Mutation(
        "coupling_obligations_ignored",
        "src/composition.rs",
        r"let\s+complete\s*=\s*residual\.is_empty\(\)\s*&&\s*coupling_findings\s*\.iter\(\)\s*\.all\(\s*\|finding\|\s*finding\.missing_obligations\.is_empty\(\)\s*\)\s*;",
        "let complete = residual.is_empty();",
        "valid_parts_do_not_discharge_missing_coupling_obligations",
    ),
    Mutation(
        "missing_observation_claimed_satisfied",
        "src/evaluation.rs",
        r'\(\s*Status::Unknown\s*,\s*"No observation exists at this pointer"\s*,?\s*\)',
        '(Status::Satisfied, "No observation exists at this pointer")',
        "native_evaluation_distinguishes_missing_fact_and_violation",
    ),
    Mutation(
        "derived_kind_obligation_removed",
        "src/graph.rs",
        r'let\s+mut\s+rules\s*=\s*vec!\[\s*serde_json::json!\(\{\s*"id":\s*"node-kind"\s*,\s*"pointer":\s*format!\("\{node_pointer\}/kind"\)\s*,\s*"kind":\s*"equals"\s*,\s*"expected":\s*&node\.kind\s*,?\s*\}\)\s*\]\s*;',
        "let mut rules: Vec<serde_json::Value> = Vec::new();",
        "candidate_derivation_preserves_old_expectation_after_graph_change",
    ),
)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def run_test(test: str, log: Path) -> dict:
    command = [
        "cargo", "test", "--locked", "--test", "behavior", test,
        "--", "--exact", "--nocapture",
    ]
    environment = os.environ.copy()
    environment.setdefault("CARGO_TARGET_DIR", str(ROOT / "target"))
    started = time.monotonic()
    process = subprocess.Popen(
        command,
        cwd=ROOT,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        start_new_session=os.name == "posix",
    )
    timed_out = False
    try:
        output, _ = process.communicate(timeout=TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        timed_out = True
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
        output, _ = process.communicate()
    log.write_text(output, encoding="utf-8")
    return {
        "command": command,
        "exit_code": process.returncode,
        "timed_out": timed_out,
        "seconds": round(time.monotonic() - started, 3),
        "log": str(log),
        "output": output,
    }


def verify_control(run: dict, test: str) -> None:
    output = run["output"]
    expected = re.search(rf"^test {re.escape(test)} \.\.\. ok$", output, re.MULTILINE)
    if run["timed_out"] or run["exit_code"] != 0 or not expected:
        raise RuntimeError(f"healthy control did not pass its named test: {test}")


def verify_mutant(run: dict, test: str) -> None:
    output = run["output"]
    named_failure = re.search(
        rf"^test {re.escape(test)} \.\.\. FAILED$", output, re.MULTILINE
    )
    if (
        run["timed_out"]
        or run["exit_code"] == 0
        or not named_failure
        or "test result: FAILED" not in output
        or "assertion" not in output
        or "error: could not compile" in output
    ):
        raise RuntimeError(
            f"mutant was not detected by a running assertion in {test}; inspect {run['log']}"
        )


def compact(run: dict) -> dict:
    return {key: value for key, value in run.items() if key != "output"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "target" / "prove-red",
        help="directory for control/mutant logs and manifest.json",
    )
    args = parser.parse_args()
    directory = args.output.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema_version": 1,
        "candidate": os.environ.get("GITHUB_SHA"),
        "status": "running",
        "mutants": [],
    }
    result = 0
    try:
        for mutation in MUTATIONS:
            path = ROOT / mutation.path
            original_bytes = path.read_bytes()
            original = original_bytes.decode("utf-8")
            pattern = re.compile(mutation.pattern, re.MULTILINE | re.DOTALL)
            count = len(list(pattern.finditer(original)))
            if count != 1:
                raise RuntimeError(
                    f"{mutation.name}: expected one source match, found {count}; refresh the calibration"
                )
            altered = pattern.sub(lambda _: mutation.replacement, original, count=1)
            if altered == original:
                raise RuntimeError(f"{mutation.name}: mutation changed no bytes")
            record = {
                "name": mutation.name,
                "path": mutation.path,
                "test": mutation.test,
                "original_sha256": sha256(original_bytes),
                "mutant_sha256": sha256(altered.encode("utf-8")),
                "status": "running",
            }
            manifest["mutants"].append(record)
            control = run_test(mutation.test, directory / f"{mutation.name}.control.log")
            record["control"] = compact(control)
            verify_control(control, mutation.test)
            if path.read_bytes() != original_bytes:
                raise RuntimeError(f"{mutation.path} changed while its control was running")
            try:
                path.write_text(altered, encoding="utf-8")
                faulty = run_test(mutation.test, directory / f"{mutation.name}.mutant.log")
                record["mutant"] = compact(faulty)
                verify_mutant(faulty, mutation.test)
                record["status"] = "detected"
            finally:
                path.write_bytes(original_bytes)
                record["restored"] = path.read_bytes() == original_bytes
            if not record["restored"]:
                raise RuntimeError(f"could not restore {mutation.path}")
            print(f"DETECTED {mutation.name}: healthy control passed, mutant assertion failed", flush=True)
        manifest["status"] = "passed"
    except Exception as error:
        manifest["status"] = "failed"
        manifest["error"] = str(error)
        print(f"Calibration failed: {error}", file=sys.stderr)
        result = 1
    finally:
        (directory / "manifest.json").write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
        )
    print(json.dumps({"status": manifest["status"], "manifest": str(directory / "manifest.json")}))
    return result


if __name__ == "__main__":
    raise SystemExit(main())
