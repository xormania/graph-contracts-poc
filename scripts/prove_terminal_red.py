"""Calibrate the PoC terminal consumer's cleanup assertion with one semantic fault.

The same real two-adapter probe must pass healthy and reject a false cleanup
receipt. Compilation/import errors, timeouts and unrelated assertions are not
detection evidence. Source bytes are restored even when calibration fails.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "scripts" / "poc_terminal.py"
PROBE = ROOT / "scripts" / "probe_terminal.py"
OUTPUT = ROOT / "target" / "prove-terminal-red"
DEADLINE = 90
EXPECTED = "AssertionError: consumer claimed cleanup but the original terminal is live"
OLD = '            closure = host.close_terminal(name)\n            record["last_observation"]'
NEW = ('            closure = SimpleNamespace(terminal=host.adopt(record["receipt"]), cleanup_verified=True)\n'
       '            record["last_observation"]')


def digest(content):
    return hashlib.sha256(content).hexdigest()


def run_probe(label):
    environment = dict(os.environ)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONIOENCODING"] = "utf-8"
    command = [sys.executable, str(PROBE)]
    process = subprocess.Popen(
        command, cwd=ROOT, env=environment, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
        start_new_session=True,
    )
    timed_out = False
    try:
        output, _ = process.communicate(timeout=DEADLINE)
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            output, _ = process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            output, _ = process.communicate(timeout=5)
    path = OUTPUT / (label + ".log")
    path.write_text(output, encoding="utf-8")
    return {
        "command": command, "exit_code": process.returncode,
        "timed_out": timed_out, "log": str(path.relative_to(ROOT)),
    }, output


def main():
    OUTPUT.mkdir(parents=True, exist_ok=True)
    original = TARGET.read_bytes()
    source = original.decode("utf-8")
    manifest = {
        "schema_version": 1,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "source_sha256": digest(original),
        "probe_sha256": digest(PROBE.read_bytes()),
        "fault": "claim_cleanup_without_stopping_terminal",
        "status": "failed",
        "results": {},
    }
    try:
        if source.count(OLD) != 1:
            raise AssertionError("The terminal consumer fault anchor must match exactly once.")
        healthy, output = run_probe("healthy")
        manifest["results"]["healthy"] = healthy
        if healthy["timed_out"] or healthy["exit_code"] != 0:
            raise AssertionError("The healthy terminal probe did not pass; inspect healthy.log.")
        report = json.loads(output.strip().splitlines()[-1])
        adapters = {item["adapter"] for item in report.get("terminal_consumers", [])
                    if item.get("status") == "passed"}
        if report.get("status") != "passed" or adapters != {"tmux", "detached-process"}:
            raise AssertionError("The healthy probe must exercise both real terminal adapters.")
        mutated = source.replace(OLD, NEW, 1).encode("utf-8")
        TARGET.write_bytes(mutated)
        manifest["mutated_source_sha256"] = digest(mutated)
        faulty, output = run_probe("faulty")
        manifest["results"]["faulty"] = faulty
        final_line = output.strip().splitlines()[-1] if output.strip() else ""
        if (faulty["timed_out"] or faulty["exit_code"] == 0 or final_line != EXPECTED
                or any(marker in output for marker in ("SyntaxError:", "ImportError:", "ModuleNotFoundError:"))):
            raise AssertionError("The false-cleanup fault did not fail the intended assertion; inspect faulty.log.")
        manifest["status"] = "passed"
        manifest["detected_assertion"] = EXPECTED
        print("DETECTED claim_cleanup_without_stopping_terminal: healthy control passed, independent receipt assertion failed")
    except BaseException as error:
        manifest["error"] = {"type": type(error).__name__, "message": str(error)}
        raise
    finally:
        TARGET.write_bytes(original)
        manifest["restored"] = TARGET.read_bytes() == original
        manifest["finished_at"] = datetime.now(timezone.utc).isoformat()
        (OUTPUT / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": manifest["status"], "manifest": str(OUTPUT / "manifest.json")}))


if __name__ == "__main__":
    main()
