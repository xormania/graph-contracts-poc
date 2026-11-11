"""Real consumer checks for both pinned Xorchestrate terminal adapters.

Run with the optional host environment's Python. This opens only temporary,
run-owned terminals. A fixture success is not an authenticated agent acceptance.
"""
from __future__ import annotations

import json
import os
import signal
from pathlib import Path
import subprocess
import sys
import tempfile
import time

import poc_terminal as terminal

SOURCE = """import select, signal, sys, time
signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
print('READY-' + sys.argv[1], flush=True)
end = time.monotonic() + 45
while time.monotonic() < end:
    readable, _, _ = select.select([sys.stdin], [], [], 0.1)
    if readable:
        line = sys.stdin.readline()
        if line:
            print('INPUT=' + line.rstrip(), flush=True)
        else:
            time.sleep(0.05)
"""
STARTER = """import json, pathlib, sys
sys.path.insert(0, sys.argv[1])
import poc_terminal
print(json.dumps(poc_terminal.start(pathlib.Path(sys.argv[2]),
                                  argv=json.loads(sys.argv[3]), name=sys.argv[4])))
"""


def require(condition, detail):
    if not condition:
        raise AssertionError(detail)


def wait_for(predicate, *, timeout=12):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(0.05)
    raise AssertionError("Condition did not become true before its deadline: " + str(last))


def settings(home, adapter, *, owned=False):
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = home / "settings.json"
    path.write_text(json.dumps({
        "schema": 1,
        "terminal_host": {"adapter": adapter, "options": {"owned": owned, "grace": 0.4}},
    }), encoding="utf-8")
    path.chmod(0o600)


def start_from_another_process(root, name):
    result = subprocess.run(
        [sys.executable, "-c", STARTER, str(Path(__file__).resolve().parent),
         str(root), json.dumps([sys.executable, "-u", "-c", SOURCE, name]), name],
        capture_output=True, text=True, timeout=15, check=True,
    )
    answer = json.loads(result.stdout)
    require(answer["status"] == "started", answer)
    return answer


def receipt(root, name):
    return json.loads((root / ".poc" / "terminal" / (name + ".json")).read_text(encoding="utf-8"))


def independently_gone(record):
    """Ask the actual port about the original receipt, ignoring consumer state."""
    from xorchestrate.instance.services import construct
    host = construct("terminal_host", record["selection"], "terminal consumer verification")
    try:
        try:
            observed = host.adopt(record["receipt"])
        except Exception as error:
            require(getattr(error, "code", None) == "terminal_not_recoverable",
                    {"unexpected_refusal": getattr(error, "code", str(error))})
        else:
            require(not observed.running, "consumer claimed cleanup but the original terminal is live")
    finally:
        host.close()



def cleanup_saved(record):
    """Cleanup uses only the captured original receipt and the real host port."""
    from xorchestrate.instance.services import construct
    host = construct("terminal_host", record["selection"], "terminal probe cleanup")
    try:
        try:
            host.adopt(record["receipt"])
        except Exception as error:
            if getattr(error, "code", None) != "terminal_not_recoverable":
                raise
        else:
            closure = host.close_terminal(record["name"], grace=0.4)
            require(closure.cleanup_verified, "The probe could not verify fixture cleanup.")
    finally:
        host.close()
    # Fixture-owned empty directories only; the port owns its socket and processes.
    location = Path(record["selection"]["options"]["location"])
    for directory in (location / "terminals", location):
        try:
            directory.rmdir()
        except OSError:
            pass


def check_adapter(base, adapter):
    root = base / adapter
    root.mkdir(mode=0o700)
    home = root / "home"
    settings(home, adapter)
    os.environ["XORCHESTRATE_HOME"] = str(home)
    ready = terminal.doctor(root)
    require(ready["status"] == "ready", ready)
    require(ready["host"]["adapter"] == adapter and ready["host"]["fallback_reason"] is None, ready)
    names = ["work", "neighbor"]
    saved = {}
    try:
        for name in names:
            start_from_another_process(root, name)
            saved[name] = receipt(root, name)
            wait_for(lambda: "READY-" + name in terminal.output(root, name=name).get("output", ""))
        require(terminal.status(root, name="work")["status"] == "running",
                "Terminal did not survive its launcher.")
        duplicate = terminal.start(root, argv=[sys.executable, "-c", "raise SystemExit(91)"], name="work")
        require(duplicate.get("code") == "terminal_exists", duplicate)
        require(receipt(root, "work")["receipt"] == saved["work"]["receipt"], "Duplicate start replaced its receipt.")

        # Controls must retain the adapter selected at start even after a home change.
        other = "detached-process" if adapter == "tmux" else "tmux"
        settings(home, other)
        observed = terminal.status(root, name="work")
        require(observed["status"] == "running" and observed["host"]["adapter"] == adapter, observed)

        attached = terminal.attach(root, name="work", execute=False)
        typed = terminal.input_text(root, "literal;value\n", name="work")
        if adapter == "tmux":
            require(attached["status"] == "instruction" and attached["argv"], attached)
            require(typed["status"] == "input_sent", typed)
            wait_for(lambda: "INPUT=literal;value" in terminal.output(root, name="work").get("output", ""))
        else:
            require(attached["status"] == "unsupported", attached)
            require(typed["status"] == "unsupported", typed)

        stopped = terminal.stop(root, name="work")
        require(stopped["status"] == "stopped" and stopped["cleanup_verified"] is True, stopped)
        independently_gone(saved["work"])
        require(terminal.stop(root, name="work").get("already_stopped") is True, "Stop was not idempotent.")
        neighbor = terminal.status(root, name="neighbor")
        require(neighbor["status"] == "running", {"foreign_terminal": neighbor})
        retained = terminal.output(root, name="work")
        require(retained["basis"] == "retained_log" and "READY-work" in retained["output"], retained)
        require(Path(saved["work"]["log"]).is_file(), "Stopping removed the retained log.")

        stopped = terminal.stop(root, name="neighbor")
        require(stopped["cleanup_verified"] is True, stopped)
        independently_gone(saved["neighbor"])

        # Copied receipts must not let another project control these terminals.
        foreign = root / "foreign-project"
        foreign.mkdir(mode=0o700)
        foreign_state = foreign / ".poc" / "terminal"
        foreign_state.mkdir(mode=0o700, parents=True)
        copied = foreign_state / "work.json"
        copied.write_text(json.dumps(saved["work"]), encoding="utf-8")
        copied.chmod(0o600)
        refused = terminal.stop(foreign, name="work")
        require(refused.get("code") == "invalid_terminal_receipt", refused)

        settings(home, adapter, owned=True)
        unsupported = terminal.start(root, argv=[sys.executable, "-c", "raise SystemExit(92)"], name="owned")
        require(unsupported["status"] == "unsupported", unsupported)
        return {
            "adapter": adapter, "status": "passed",
            "checks": ["separate_launcher_survival", "duplicate_refusal", "frozen_selection",
                       "attach_capability", "literal_input_capability", "verified_close",
                       "independent_original_receipt_check", "neighbor_preserved",
                       "retained_log", "idempotent_stop", "foreign_receipt_refusal", "owned_mode_refusal"],
        }
    finally:
        failures = []
        for record in saved.values():
            try:
                cleanup_saved(record)
            except Exception as error:
                failures.append(str(error))
        require(not failures, {"fixture_cleanup_failures": failures})


def main():
    def interrupted(*_):
        raise InterruptedError("Terminal consumer probe interrupted")
    signal.signal(signal.SIGTERM, interrupted)
    previous_home = os.environ.get("XORCHESTRATE_HOME")
    try:
        with tempfile.TemporaryDirectory(prefix="gcp-terminal-probe-") as directory:
            base = Path(directory)
            reports = [check_adapter(base, adapter) for adapter in ("tmux", "detached-process")]
            print(json.dumps({"status": "passed", "terminal_consumers": reports}))
    finally:
        if previous_home is None:
            os.environ.pop("XORCHESTRATE_HOME", None)
        else:
            os.environ["XORCHESTRATE_HOME"] = previous_home


if __name__ == "__main__":
    main()
