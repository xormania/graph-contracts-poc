#!/usr/bin/env python3
"""Local graph/contracts experiments; terminal ownership stays in Xorchestrate."""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import copy
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
LIMIT = 4 * 1024 * 1024
SAFE_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\Z")
MISSING = object()


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def encoded(value) -> bytes:
    return (json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False, allow_nan=False) + "\n").encode()


def read_bytes(path: Path) -> bytes:
    with path.open("rb") as stream:
        data = stream.read(LIMIT + 1)
    if len(data) > LIMIT:
        raise ValueError(f"Input exceeds 4 MiB: {path}")
    return data


def invalid_constant(value):
    raise ValueError(f"Nonfinite JSON number: {value}")


def read_json(path: Path):
    return json.loads(read_bytes(path), parse_constant=invalid_constant)


def json_equal(left, right):
    return encoded(left) == encoded(right)


def atomic_json(path: Path, value) -> str:
    data = encoded(value)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return digest(data)


def private_directory(path: Path):
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink() or not path.is_dir():
        raise ValueError(f"Expected a real directory: {path}")


def pointer(value, expression: str):
    if expression == "":
        return value
    if not expression.startswith("/") or re.search(r"~(?![01])", expression):
        raise ValueError(f"Invalid JSON pointer: {expression}")
    current = value
    for part in expression[1:].split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict):
            if part not in current:
                return MISSING
            current = current[part]
        elif isinstance(current, list) and re.fullmatch(r"0|[1-9][0-9]*", part):
            index = int(part)
            if index >= len(current):
                return MISSING
            current = current[index]
        else:
            return MISSING
    return current


def validate_plan(plan):
    if not isinstance(plan, dict) or plan.get("schema_version") != 1:
        raise ValueError("Plan schema_version must be 1")
    scenarios = plan.get("scenarios")
    if not isinstance(scenarios, list) or not scenarios:
        raise ValueError("Plan requires nonempty scenarios")
    seen_scenarios, seen_cases = set(), set()
    for scenario in scenarios:
        name = scenario.get("id") if isinstance(scenario, dict) else None
        if not isinstance(name, str) or not SAFE_ID.fullmatch(name) or name in seen_scenarios:
            raise ValueError("Scenario IDs must be unique safe names")
        seen_scenarios.add(name)
        cases = scenario.get("cases")
        if not isinstance(cases, list) or not cases:
            raise ValueError(f"Scenario {name} requires cases")
        for case in cases:
            identifier = case.get("id") if isinstance(case, dict) else None
            if (not isinstance(identifier, str) or not SAFE_ID.fullmatch(identifier)
                    or identifier in seen_cases):
                raise ValueError("Case IDs must be unique safe names across the plan")
            seen_cases.add(identifier)
            if not isinstance(case.get("name"), str) or not case["name"]:
                raise ValueError(f"Case {identifier} requires an operation name")
            if not isinstance(case.get("arguments"), dict):
                raise ValueError(f"Case {identifier} arguments must be an object")
            expect = case.get("expect")
            if not isinstance(expect, dict) or not expect:
                raise ValueError(f"Case {identifier} requires nonempty expectations")
            for expression in expect:
                pointer({}, expression)
    return plan


def selected_plan(plan, selected):
    plan = copy.deepcopy(validate_plan(plan))
    if selected:
        unknown = set(selected) - {item["id"] for item in plan["scenarios"]}
        if unknown:
            raise ValueError(f"Unknown scenarios: {', '.join(sorted(unknown))}")
        plan["scenarios"] = [item for item in plan["scenarios"] if item["id"] in selected]
    return plan


def safe_artifact(directory: Path, relative: str) -> Path:
    if not isinstance(relative, str) or not relative or "\\" in relative:
        raise ValueError("Artifact path must be a normalized relative path")
    raw = Path(relative)
    if raw.is_absolute() or any(part in ("", ".", "..") for part in relative.split("/")):
        raise ValueError("Artifact path must be a normalized relative path")
    path = directory / raw
    cursor = directory
    for part in raw.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise ValueError("Artifact path cannot follow a symlink")
    if not path.resolve().is_relative_to(directory.resolve()):
        raise ValueError("Artifact path escapes its run")
    return path


def verify_run(directory: Path):
    report = read_json(directory / "run.json")
    for name, field in [("effective-plan.json", "effective_plan_digest"),
                        ("source-inputs.json", "source_inputs_digest")]:
        expected = report.get(field)
        if not expected or digest(read_bytes(directory / name)) != expected:
            raise ValueError(f"Saved {name} identity mismatch")
    sources = read_json(directory / "source-inputs.json")
    for item in sources:
        if digest(read_bytes(safe_artifact(directory, item["snapshot"]))) != item["digest"]:
            raise ValueError("Saved input identity mismatch")
    for case in report["cases"]:
        for path_key, hash_key in [("request_path", "request_digest"),
                                   ("result_path", "result_digest")]:
            if case.get(path_key):
                content = read_bytes(safe_artifact(directory, case[path_key]))
                if digest(content) != case.get(hash_key):
                    raise ValueError(f"Saved {path_key} identity mismatch")
    return report, sources


def resolve_arguments(value, root: Path, run: Path, sources: list, results):
    if isinstance(value, dict) and "$result" in value:
        if set(value) != {"$result"} or not isinstance(value["$result"], str) or value["$result"] not in results:
            raise ValueError("$result must name a previously completed case")
        return copy.deepcopy(results[value["$result"]])
    if isinstance(value, dict) and "$file" in value:
        if set(value) != {"$file"} or not isinstance(value["$file"], str):
            raise ValueError("$file must be the only property and contain a relative path")
        candidate = (root / value["$file"]).resolve()
        if not candidate.is_relative_to(root.resolve()):
            raise ValueError("Input file must be inside the selected project")
        data = read_bytes(candidate)
        suffix = ".cue" if candidate.suffix == ".cue" else ".json"
        relative = f"inputs/{digest(data)}{suffix}"
        destination = run / relative
        if not destination.exists():
            fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
        record = {"path": value["$file"], "digest": digest(data), "snapshot": relative}
        if record not in sources:
            sources.append(record)
        if suffix == ".cue":
            return {"source": data.decode("utf-8"), "definition": "#Contract"}
        return json.loads(data, parse_constant=invalid_constant)
    if isinstance(value, dict):
        return {key: resolve_arguments(item, root, run, sources, results) for key, item in value.items()}
    if isinstance(value, list):
        return [resolve_arguments(item, root, run, sources, results) for item in value]
    return value


def executable(value: str | Path) -> Path | None:
    value = str(value)
    found = shutil.which(value) if not os.path.dirname(value) else value
    return Path(found).resolve() if found and Path(found).is_file() else None


def fingerprint(value: str | Path):
    path = executable(value)
    if path is None:
        return {"requested": str(value), "available": False}
    record = {"requested": str(value), "path": str(path), "available": True}
    with path.open("rb") as stream:
        record["sha256"] = hashlib.file_digest(stream, "sha256").hexdigest()
    return record


def identity(value: str | Path, version_argument: str):
    record = fingerprint(value)
    if not record["available"]:
        return record
    try:
        with tempfile.TemporaryDirectory(prefix="gcp-version-") as temporary:
            directory = Path(temporary)
            out, err = directory / "stdout", directory / "stderr"
            observed = capture_command([record["path"], version_argument], None,
                                       out, err, 5, directory)
            with out.open("rb") as stream:
                version = stream.read(4096).decode("utf-8", errors="replace")
            record["version"] = version.splitlines()[:1]
            record["version_exit_code"] = observed["exit_code"]
            if observed["problem"]:
                record["version_problem"] = observed["problem"]
            if observed["interrupted"]:
                raise KeyboardInterrupt
    except OSError as error:
        record["version_error"] = str(error)
    return record


def source_identity(root: Path):
    # Binary identity is authoritative for what ran. Source identity is additional context.
    paths = [root / "Cargo.toml", root / "Cargo.lock", root / "rust-toolchain.toml"]
    if (root / "src").is_dir():
        paths.extend(sorted((root / "src").rglob("*.rs")))
    if (root / "scripts").is_dir():
        paths.extend(sorted((root / "scripts").glob("*.py")))
    paths.append(root / "poc/host-lock.json")
    hashes = {str(path.relative_to(root)): digest(path.read_bytes())
              for path in paths if path.is_file()}
    return {"files": hashes, "digest": digest(encoded(hashes))}


def terminate(process: subprocess.Popen):
    # Only this invocation's newly created process group is signalled.
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGTERM)
        elif process.poll() is None:
            process.terminate()
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        pass
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        elif process.poll() is None:
            process.kill()
    except ProcessLookupError:
        pass
    process.wait(timeout=2)


def capture_command(argv, input_path, stdout_path, stderr_path, timeout, cwd):
    started = time.monotonic()
    process = None
    problem, interrupted = None, False
    with ExitStack() as stack:
        incoming = stack.enter_context(input_path.open("rb")) if input_path else subprocess.DEVNULL
        out = stack.enter_context(stdout_path.open("xb"))
        err = stack.enter_context(stderr_path.open("xb"))
        os.chmod(stdout_path, 0o600)
        os.chmod(stderr_path, 0o600)
        try:
            process = subprocess.Popen(argv, stdin=incoming, stdout=out, stderr=err,
                                       start_new_session=os.name == "posix", cwd=cwd)
            while True:
                if stdout_path.stat().st_size > LIMIT or stderr_path.stat().st_size > LIMIT:
                    problem = "command_output_limit"
                    break
                if process.poll() is not None:
                    break
                remaining = timeout - (time.monotonic() - started)
                if remaining <= 0:
                    problem = "command_timeout"
                    break
                try:
                    process.wait(timeout=min(0.025, remaining))
                except subprocess.TimeoutExpired:
                    pass
        except KeyboardInterrupt:
            interrupted, problem = True, "interrupted"
        except OSError as error:
            problem = f"command_unavailable: {error}"
        finally:
            # The session may still contain children after its leader has exited.
            if process is not None:
                terminate(process)
    for path in (stdout_path, stderr_path):
        if path.stat().st_size > LIMIT:
            problem = "command_output_limit"
            with path.open("r+b") as stream:
                stream.truncate(LIMIT)
    return {"exit_code": process.returncode if process else None,
            "duration_ms": round((time.monotonic() - started) * 1000),
            "problem": problem, "interrupted": interrupted}


def invoke(binary: Path, cue: str, request: bytes, run: Path, case_id: str, timeout: float):
    stdout_path, stderr_path = run / f"output/{case_id}.stdout", run / f"output/{case_id}.stderr"
    # Use the retained request file so a child that never reads stdin cannot
    # block the controller before its deadline starts.
    observed = capture_command([str(binary), "--cue", cue, "provider"],
                               run / f"requests/{case_id}.json",
                               stdout_path, stderr_path, timeout, run)
    return {**observed, "stdout_path": str(stdout_path.relative_to(run)),
            "stderr_path": str(stderr_path.relative_to(run))}


def run_plan(root: Path, plan_path: Path, selected: list[str] | None, binary: Path,
             cue: str, output_root: Path, timeout: float = 30.0,
             repeat: Path | None = None) -> Path:
    root, output_root = Path(root).resolve(), Path(output_root).resolve()
    binary = Path(binary).resolve()
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("Timeout must be positive and finite")
    parent, old_sources = None, []
    if repeat is not None:
        repeat = Path(repeat).resolve()
        parent, old_sources = verify_run(repeat)
        plan = selected_plan(read_json(repeat / "effective-plan.json"), selected)
        captured = {item["id"]: item for item in parent["cases"]}
        for scenario in plan["scenarios"]:
            for case in scenario["cases"]:
                previous = captured.get(case["id"], {})
                if not previous.get("request_path"):
                    raise ValueError("This selection has uncaptured inputs; repeat a captured selection or start the plan afresh")
                request = read_json(safe_artifact(repeat, previous["request_path"]))
                if request.get("name") != case["name"] or not json_equal(request.get("arguments"), case["arguments"]):
                    raise ValueError("Saved effective plan does not match its request")
    else:
        plan = selected_plan(read_json(Path(plan_path)), selected)
    private_directory(output_root)
    identifier = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:12]
    run = output_root / identifier
    run.mkdir(mode=0o700)
    for name in ("inputs", "requests", "results", "output"):
        (run / name).mkdir(mode=0o700)
    atomic_json(run / "plan.json", plan)
    report = {"schema_version": 1, "id": identifier, "execution": "running",
              "checks": "incomplete", "started_at": datetime.now(timezone.utc).isoformat(),
              "root": str(root), "repeat_of": str(repeat) if repeat else None,
              "binary": identity(binary, "--version"), "cue": identity(cue, "version"),
              "source": source_identity(root), "cases": []}
    sources, results = [], {}
    cue_used = False
    if repeat:
        for item in old_sources:
            destination = safe_artifact(run, item["snapshot"])
            destination.write_bytes(read_bytes(safe_artifact(repeat, item["snapshot"])))
            os.chmod(destination, 0o600)
            sources.append(copy.deepcopy(item))
    report["source_inputs_digest"] = atomic_json(run / "source-inputs.json", sources)
    report["effective_plan_digest"] = atomic_json(run / "effective-plan.json", plan)
    atomic_json(run / "run.json", report)
    aborted = False
    for scenario in plan["scenarios"]:
        for case in scenario["cases"]:
            record = {"id": case["id"], "scenario": scenario["id"], "name": case["name"],
                      "execution": "running", "check": "incomplete"}
            report["cases"].append(record)
            atomic_json(run / "run.json", report)
            try:
                if repeat:
                    arguments = copy.deepcopy(case["arguments"])
                else:
                    arguments = resolve_arguments(case["arguments"], root, run, sources, results)
                case["arguments"] = arguments
                cue_used = cue_used or case["name"] == "contract.compare" or (
                    case["name"] == "contract.evaluate" and arguments.get("backend") == "cue")
                report["source_inputs_digest"] = atomic_json(run / "source-inputs.json", sources)
                report["effective_plan_digest"] = atomic_json(run / "effective-plan.json", plan)
                request = {"name": case["name"], "arguments": arguments, "context": {}}
                record["request_path"] = f"requests/{case['id']}.json"
                record["request_digest"] = atomic_json(run / record["request_path"], request)
                atomic_json(run / "run.json", report)
                observed = invoke(binary, cue, encoded(request), run, case["id"], timeout)
                record.update({key: value for key, value in observed.items() if key != "interrupted"})
                if observed["interrupted"]:
                    record.update(execution="interrupted", check="incomplete")
                    aborted = True
                elif observed["problem"] or observed["exit_code"] != 0:
                    record.update(execution="error", check="failed")
                else:
                    try:
                        result = read_json(run / observed["stdout_path"])
                    except (ValueError, UnicodeError) as error:
                        record.update(execution="error", check="failed",
                                      problem=f"invalid_json_output: {error}")
                    else:
                        record["result_path"] = f"results/{case['id']}.json"
                        record["result_digest"] = atomic_json(run / record["result_path"], result)
                        results[case["id"]] = result
                        failures = []
                        for expression, expected in case["expect"].items():
                            actual = pointer(result, expression)
                            if actual is MISSING or not json_equal(actual, expected):
                                failures.append({"pointer": expression, "expected": expected,
                                                 "actual": None if actual is MISSING else actual,
                                                 "missing": actual is MISSING})
                        record.update(execution="completed", check="failed" if failures else "passed",
                                      expectation_failures=failures)
                        if isinstance(result, dict) and "status" in result:
                            record["domain_status"] = result["status"]
            except KeyboardInterrupt:
                record.update(execution="interrupted", check="incomplete", problem="interrupted")
                aborted = True
            except (ValueError, OSError, KeyError, TypeError) as error:
                record.update(execution="error", check="failed", problem=str(error))
            atomic_json(run / "run.json", report)
            if aborted:
                break
        if aborted:
            break
    report["execution"] = "interrupted" if aborted else "completed"
    report["checks"] = ("incomplete" if aborted else
                        "passed" if all(c["check"] == "passed" for c in report["cases"]) else "failed")
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    report["binary_after"] = fingerprint(binary)
    report["cue_after"] = fingerprint(cue)
    report["implementation_changed_during_run"] = (
        report["binary"].get("sha256") != report["binary_after"].get("sha256")
        or (cue_used and report["cue"].get("sha256") != report["cue_after"].get("sha256")))
    if report["implementation_changed_during_run"]:
        report["checks"] = "incomplete"
    report["source_after"] = source_identity(root)
    report["source_changed_during_run"] = report["source"] != report["source_after"]
    atomic_json(run / "run.json", report)
    return run


def doctor(root: Path, binary: Path, cue: str):
    binary_info, cue_info = identity(binary, "--version"), identity(cue, "version")
    native_ready = binary_info.get("available", False) and binary_info.get("version_exit_code") == 0 and not binary_info.get("version_problem")
    cue_ready = cue_info.get("available", False) and cue_info.get("version_exit_code") == 0 and not cue_info.get("version_problem")
    return {"schema_version": 1, "root": str(root),
            "status": "ready" if native_ready else "needs_setup",
            "native_ready": native_ready, "cue_ready": cue_ready,
            "binary": binary_info, "cue": cue_info,
            "cargo": shutil.which("cargo"), "python": sys.version.split()[0],
            "host_environment": (root / ".poc/host/source/.venv/bin/python").is_file(),
            "plan": str(root / "poc/experiments.json"),
            "next": [] if native_ready else ["python3 scripts/poc.py setup"]}


def compare_runs(before: Path, after: Path):
    left, _ = verify_run(before)
    right, _ = verify_run(after)
    first = {item["id"]: item for item in left["cases"]}
    second = {item["id"]: item for item in right["cases"]}
    rows = []
    for identifier in sorted(set(first) | set(second)):
        a, b = first.get(identifier, {}), second.get(identifier, {})
        rows.append({"id": identifier, "same_request": bool(a and b) and
                     a.get("request_digest") == b.get("request_digest"),
                     "result_changed": a.get("result_digest") != b.get("result_digest"),
                     "before": {k: a.get(k) for k in ("execution", "check", "domain_status")},
                     "after": {k: b.get(k) for k in ("execution", "check", "domain_status")}})
    return {"same_requests": all(row["same_request"] for row in rows),
            "binary_changed": left["binary"].get("sha256") != right["binary"].get("sha256"),
            "before": str(before), "after": str(after), "cases": rows}


def default_cue(root: Path):
    configured = os.environ.get("CUE_BIN")
    local = root / ".poc/bin/cue"
    return str(executable(configured) or configured) if configured else str(local if local.exists() else executable("cue") or "cue")


def present(result, command, machine=False):
    if machine:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    elif command == "list":
        for scenario in result:
            needs = ", ".join(scenario["requires"]) or "native"
            print(f"{scenario['id']} ({len(scenario['cases'])} cases; {needs})")
            print("  " + scenario["description"])
    elif command in ("run", "repeat", "show"):
        print(f"Run {result['id']}: {result['execution']}; checks {result['checks']}")
        for case in result["cases"]:
            observed = case.get("domain_status", case["execution"])
            print(f"  {case['check'].upper():10} {case['id']}: {observed}")
            if case.get("problem"):
                print("    " + case["problem"])
        print("Saved: " + result["directory"])
        if result.get("implementation_changed_during_run"):
            print("The selected executable changed during this run; qualification is incomplete.")
    elif command == "doctor":
        print("Rust app: " + ("ready" if result["native_ready"] else "needs setup"))
        print("CUE: " + ("ready" if result["cue_ready"] else "unavailable; optional for native scenarios"))
        print("Host environment: " + ("prepared; use terminal doctor to check it"
                                     if result["host_environment"] else "optional setup --terminal"))
        for step in result["next"]:
            print("Next: " + step)
    elif command == "setup":
        print("Setup: " + result["status"])
        print("Rust app: " + result["gcp"]["path"])
        if "cue" in result:
            print("CUE: " + result["cue"]["path"])
        if "host" in result:
            print("Xorchestrate: " + result["host"]["revision"])
        print("Next: python3 scripts/poc.py list")
    elif command == "probe-host":
        print(f"Host provider: {result['status']} ({len(result['checks'])} checks)")
        print("Saved: " + result["receipt_path"])
        if result.get("error"):
            print(result["error"])
    elif command == "compare":
        print("Same captured requests: " + str(result["same_requests"]).lower())
        print("Implementation binary changed: " + str(result["binary_changed"]).lower())
        for case in result["cases"]:
            print(f"  {case['id']}: request {'same' if case['same_request'] else 'different'}; "
                  f"result {'changed' if case['result_changed'] else 'unchanged'}")
    else:
        print("Terminal: " + result.get("status", "unknown"))
        host = result.get("host", {})
        if host:
            print("Host: " + host.get("adapter", "unknown"))
            if host.get("fallback_reason"):
                print("Fallback: " + str(host["fallback_reason"]))
        if result.get("message"):
            print(result["message"])
        if "output" in result:
            print(result["output"])
        if result.get("log"):
            print("Log: " + result["log"])
        if "cleanup_verified" in result:
            print("Cleanup verified: " + str(result["cleanup_verified"]).lower())


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="Emit structured output")
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--binary", type=Path)
    parser.add_argument("--cue")
    sub = parser.add_subparsers(dest="command", required=True)
    setup_parser = sub.add_parser("setup", help="Build Rust and prepare selected optional tools")
    setup_parser.add_argument("--terminal", action="store_true")
    setup_parser.add_argument("--no-cue", action="store_true")
    sub.add_parser("doctor", help="Report selected binaries and optional host readiness")
    sub.add_parser("list", help="List editable experiment scenarios")
    for command in ("run", "repeat"):
        current = sub.add_parser(command)
        if command == "repeat":
            current.add_argument("previous", type=Path)
        current.add_argument("--scenario", action="append")
        current.add_argument("--plan", type=Path)
        current.add_argument("--output", type=Path)
        current.add_argument("--timeout", type=float, default=30)
        current.add_argument("--no-build", action="store_true")
    current = sub.add_parser("show")
    current.add_argument("run", type=Path)
    current = sub.add_parser("compare")
    current.add_argument("before", type=Path)
    current.add_argument("after", type=Path)
    terminal = sub.add_parser("terminal", help="Use the pinned Xorchestrate TerminalHost")
    terminal.add_argument("action", choices=("doctor", "start", "status", "output", "attach", "stop"))
    terminal.add_argument("--name", default="work")
    terminal.add_argument("--lines", type=int, default=100)
    terminal.add_argument("--print-only", action="store_true")
    sub.add_parser("probe-host", help="Exercise the real Xorchestrate CLIProvider")
    args = parser.parse_args(argv)
    root = args.root.resolve()
    binary = (args.binary or root / "target/debug/gcp").resolve()
    cue = str(executable(args.cue) or args.cue) if args.cue else default_cue(root)
    if args.command == "setup":
        from poc_setup import setup
        result = setup(root, with_terminal=args.terminal, cue=not args.no_cue)
    elif args.command == "doctor":
        result = doctor(root, binary, cue)
    elif args.command == "list":
        plan = validate_plan(read_json(root / "poc/experiments.json"))
        result = [{"id": s["id"], "description": s.get("description", ""),
                   "requires": s.get("requires", []), "cases": [c["id"] for c in s["cases"]]}
                  for s in plan["scenarios"]]
    elif args.command in ("run", "repeat"):
        if not args.no_build and args.binary is None:
            subprocess.run(["cargo", "build", "--locked", "--target-dir", str(root / "target")],
                           cwd=root, check=True, timeout=600)
        run = run_plan(root, args.plan or root / "poc/experiments.json", args.scenario,
                       binary, cue, args.output or root / ".poc/runs", args.timeout,
                       args.previous if args.command == "repeat" else None)
        result = read_json(run / "run.json")
        result["directory"] = str(run)
    elif args.command == "show":
        result, _ = verify_run(args.run.resolve())
        result["directory"] = str(args.run.resolve())
    elif args.command == "compare":
        result = compare_runs(args.before.resolve(), args.after.resolve())
    else:
        from poc_setup import host_python
        interpreter = host_python(root)
        if not interpreter.is_file():
            raise ValueError("Optional host environment is missing; run setup --terminal")
        if Path(sys.prefix).resolve() != interpreter.parent.parent.resolve():
            os.execv(str(interpreter), [str(interpreter), str(Path(__file__).resolve()), *sys.argv[1:]])
        if args.command == "probe-host":
            directory = root / ".poc/probes"
            private_directory(directory)
            receipt = directory / (uuid.uuid4().hex + ".json")
            completed = subprocess.run(
                [str(interpreter), str(root / "scripts/probe_host.py"),
                 "--root", str(root), "--binary", str(binary), "--cue", cue,
                 "--output", str(receipt)], capture_output=True, text=True, timeout=120)
            if not receipt.exists():
                raise RuntimeError("Host probe did not retain its report: " + completed.stderr[-4096:])
            result = read_json(receipt)
            result["receipt_path"] = str(receipt)
            present(result, args.command, args.json)
            return 0 if result["status"] == "passed" and completed.returncode == 0 else 1
        import poc_terminal
        if args.action == "doctor":
            result = poc_terminal.doctor(root)
        elif args.action == "start":
            result = poc_terminal.start(root, name=args.name)
        elif args.action == "status":
            result = poc_terminal.status(root, name=args.name)
        elif args.action == "output":
            result = poc_terminal.output(root, name=args.name, lines=args.lines)
        elif args.action == "attach":
            result = poc_terminal.attach(root, name=args.name, execute=not args.print_only)
        else:
            result = poc_terminal.stop(root, name=args.name)
    present(result, args.command, args.json)
    if args.command == "doctor" and not result["native_ready"]:
        return 2
    if args.command == "terminal" and isinstance(result, dict):
        if result.get("status") in ("error", "unsupported", "unavailable", "unknown"):
            return 2
        if args.action == "stop" and not result.get("cleanup_verified", False):
            return 2
    if isinstance(result, dict) and result.get("checks") in ("failed", "incomplete"):
        return 3
    return 0


if __name__ == "__main__":
    def cancelled(_number, _frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, cancelled)
    try:
        raise SystemExit(main())
    except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as error:
        print(json.dumps({"error": str(error)}), file=sys.stderr)
        raise SystemExit(2)
