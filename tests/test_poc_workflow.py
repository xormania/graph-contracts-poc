"""Behavior contracts for the local experiment workflow.

These tests were specified independently of scripts/poc.py. They use a real,
trusted subprocess fixture so transport failures cannot be mistaken for domain
outcomes. No model account, CUE installation, or tmux server is needed.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import signal
import stat
import sys
import tempfile
import time
import unittest

PROJECT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("graph_poc_workflow", PROJECT / "scripts" / "poc.py")
assert SPEC is not None and SPEC.loader is not None
WORKFLOW = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = WORKFLOW
SPEC.loader.exec_module(WORKFLOW)

PROVIDER_SOURCE = r'''
import json
import sys
import time
import subprocess
from pathlib import Path

CHILD_SOURCE = """
import json, os, time
from pathlib import Path
import sys
marker, heartbeat = map(Path, sys.argv[1:3])
heartbeat.write_text('started\\\\n', encoding='utf-8')
ticks = Path('/proc/self/stat').read_text().rsplit(') ', 1)[1].split()[19]
temporary = marker.with_suffix('.pending')
temporary.write_text(json.dumps({'pid': os.getpid(), 'start_ticks': ticks}), encoding='utf-8')
temporary.replace(marker)
deadline = time.monotonic() + 5.0
while time.monotonic() < deadline:
    with heartbeat.open('a', encoding='utf-8') as output:
        output.write('tick\\\\n')
    time.sleep(0.025)
"""

if "--version" in sys.argv or sys.argv[1:] == ["version"]:
    print("fixture-provider 1.0")
    raise SystemExit(0)
if sys.argv[-1:] != ["provider"] or "--cue" not in sys.argv:
    print("expected explicit provider invocation", file=sys.stderr)
    raise SystemExit(9)
request = json.load(sys.stdin)
if request.get("context") != {}:
    raise SystemExit(8)
arguments = request["arguments"]
mode = arguments.get("fixture_mode", "satisfied")
if mode == "malformed":
    print("{unfinished")
    raise SystemExit(0)
if mode == "timeout":
    time.sleep(1.0)
if mode == "derive":
    result = {
        "status": "candidate",
        "expected": arguments["input"]["limit"],
        "source_revision": arguments["input"]["revision"],
    }
elif mode == "candidate_check":
    result = {
        "status": "satisfied" if arguments["contract"]["expected"] == arguments["input"]["limit"] else "violated",
        "candidate": arguments["contract"],
    }
elif mode == "echo":
    result = {"status": "satisfied", "echo": arguments["payload"]}
elif mode == "spawn_child":
    marker = Path(arguments["child_marker"])
    heartbeat = Path(arguments["child_heartbeat"])
    child = subprocess.Popen(
        [sys.executable, "-u", "-c", CHILD_SOURCE, str(marker), str(heartbeat)],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 1.0
    while not marker.is_file() and time.monotonic() < deadline:
        time.sleep(0.005)
    result = {"status": "satisfied", "child_pid": child.pid, "child_started": marker.is_file()}
elif mode == "checkpoint":
    reports = [json.loads(path.read_text(encoding="utf-8"))
               for path in Path(arguments["runs_root"]).glob("*/run.json")]
    saved = any(any(case.get("id") == "before" and case.get("execution") == "completed"
                    and case.get("check") == "passed" for case in report.get("cases", []))
                for report in reports)
    result = {"status": "satisfied" if saved else "violated"}
else:
    result = {"status": "violated" if mode == "violated" else "satisfied"}
if mode == "self_modify":
    executable = Path(sys.argv[0])
    executable.write_text(executable.read_text(encoding="utf-8") + "\n# changed during execution\n",
                          encoding="utf-8")
print(json.dumps(result))
if mode == "nonzero":
    print("fixture execution failed after producing output", file=sys.stderr)
    raise SystemExit(7)
'''


@unittest.skipUnless(os.name == "posix", "The trusted executable fixture uses a POSIX shebang")
class WorkflowBehavior(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="graph workflow ")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "project with spaces"
        self.root.mkdir()
        (self.root / "inputs").mkdir()
        (self.root / "tools").mkdir()
        self.binary = self.root / "tools" / "fixture-provider"
        self.binary.write_text(f"#!{sys.executable}\n" + PROVIDER_SOURCE, encoding="utf-8")
        self.binary.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
        self.output_root = self.root / "runs"
        self.sequence = 0

    def write_json(self, relative, value):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
        return path

    def plan(self, cases, *, scenario="behavior"):
        self.sequence += 1
        return self.write_json(
            f"plans/plan-{self.sequence}.json",
            {"schema_version": 1, "scenarios": [{"id": scenario, "cases": cases}]},
        )

    def case(self, identifier, mode, expected="satisfied"):
        return {
            "id": identifier,
            "name": "contract.evaluate",
            "arguments": {"fixture_mode": mode},
            "expect": {"/status": expected},
        }

    def run_plan(self, path, *, selected=None, repeat=None, timeout=2.0):
        return Path(WORKFLOW.run_plan(
            root=self.root,
            plan_path=path,
            selected=selected,
            binary=self.binary,
            cue=str(self.binary),
            output_root=self.output_root,
            timeout=timeout,
            repeat=repeat,
        ))

    def report(self, run):
        return json.loads((run / "run.json").read_text(encoding="utf-8"))

    def artifact(self, run, value):
        self.assertIsInstance(value, str)
        path = Path(value)
        path = path if path.is_absolute() else run / path
        self.assertTrue(path.resolve().is_relative_to(run.resolve()), str(path))
        return path

    def document(self, run, value):
        return json.loads(self.artifact(run, value).read_text(encoding="utf-8"))

    def frozen_files(self, directory):
        return {
            str(path.relative_to(directory)): path.read_bytes()
            for path in directory.rglob("*") if path.is_file()
        }

    @unittest.skipUnless(Path("/proc/self/stat").is_file(), "Child identity check requires Linux /proc")
    def test_finished_provider_does_not_leave_its_child_running(self):
        marker = self.root / "child.json"
        heartbeat = self.root / "child-heartbeat.txt"
        path = self.plan([{
            "id": "owned-child",
            "name": "contract.evaluate",
            "arguments": {
                "fixture_mode": "spawn_child",
                "child_marker": str(marker),
                "child_heartbeat": str(heartbeat),
            },
            "expect": {"/status": "satisfied", "/child_started": True},
        }])
        try:
            run = self.run_plan(path)
            summary = self.report(run)
            result = self.document(run, summary["cases"][0]["result_path"])
            self.assertTrue(result.get("child_started"), "Fixture child did not establish its identity")
            self.assertEqual(summary["cases"][0]["check"], "passed")
            before = heartbeat.read_bytes()
            time.sleep(0.15)
            self.assertEqual(heartbeat.read_bytes(), before,
                             "A provider descendant continued writing after the run returned")
        finally:
            # The failing historical implementation must not leave the fixture
            # child behind. The marker was written by this fresh child; fence
            # its PID by Linux start identity before signalling it.
            if marker.is_file():
                record = json.loads(marker.read_text(encoding="utf-8"))
                process = Path("/proc") / str(record["pid"])
                try:
                    current = (process / "stat").read_text().rsplit(") ", 1)[1].split()[19]
                    if current == record["start_ticks"] and process.stat().st_uid == os.getuid():
                        os.kill(record["pid"], signal.SIGKILL)
                except (FileNotFoundError, ProcessLookupError):
                    pass

    def test_repeat_rejects_noncanonical_snapshot_paths_before_writing(self):
        self.write_json("inputs/value.json", {"limit": 7})
        case = {
            "id": "snapshot-path",
            "name": "contract.evaluate",
            "arguments": {"fixture_mode": "echo", "payload": {"$file": "inputs/value.json"}},
            "expect": {"/echo": {"limit": 7}},
        }
        path = self.plan([case])
        for shape in ["absolute", "parent-traversal"]:
            with self.subTest(shape=shape):
                run = self.run_plan(path)
                source_path = run / "source-inputs.json"
                sources = json.loads(source_path.read_text(encoding="utf-8"))
                relative = sources[0]["snapshot"]
                original_input = run / relative
                sources[0]["snapshot"] = (
                    str(original_input.resolve()) if shape == "absolute"
                    else f"../{run.name}/{relative}"
                )
                source_path.write_text(json.dumps(sources, indent=2) + "\n", encoding="utf-8")
                summary = self.report(run)
                summary["source_inputs_digest"] = hashlib.sha256(source_path.read_bytes()).hexdigest()
                (run / "run.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
                before = self.frozen_files(run)
                original_mtime = original_input.stat().st_mtime_ns
                rejected = False
                try:
                    self.run_plan(path, repeat=run)
                except (ValueError, RuntimeError):
                    rejected = True
                self.assertTrue(rejected, "Saved snapshot paths must be normalized relative paths")
                self.assertEqual(self.frozen_files(run), before)
                self.assertEqual(original_input.stat().st_mtime_ns, original_mtime,
                                 "Repeat rewrote an input in the original run")

    def test_oversized_version_probe_is_reported_and_bounded(self):
        original = self.binary.read_text(encoding="utf-8")
        replacement = 'sys.stdout.buffer.write(b"x" * (5 * 1024 * 1024))'
        self.assertEqual(original.count('print("fixture-provider 1.0")'), 1)
        self.binary.write_text(
            original.replace('print("fixture-provider 1.0")', replacement),
            encoding="utf-8",
        )
        started = time.monotonic()
        run = self.run_plan(self.plan([self.case("version-bound", "satisfied")]))
        summary = self.report(run)
        self.assertEqual(summary.get("binary", {}).get("version_problem"), "command_output_limit")
        self.assertLess(len(json.dumps(summary.get("binary", {}))), 10000,
                        "Version diagnostics must remain bounded")
        self.assertLess(time.monotonic() - started, 8.0)

    def test_changed_executable_keeps_the_run_unqualified(self):
        path = self.plan([self.case("changed-implementation", "self_modify")])
        run = self.run_plan(path)
        summary = self.report(run)
        self.assertEqual(summary["cases"][0]["check"], "passed",
                         "The domain fixture still returned its expected outcome")
        self.assertTrue(summary.get("implementation_changed_during_run", False),
                        "Executable changes must be visible in the run identity")
        after = summary.get("binary_after")
        self.assertIsInstance(after, dict, "Retain the executable identity observed after execution")
        self.assertNotEqual(summary["binary"].get("sha256"), after.get("sha256"))
        self.assertEqual(summary["checks"], "incomplete",
                         "Passing outcomes cannot qualify a run whose implementation changed")

    def test_outcome_matrix_preserves_domain_and_execution_distinctions(self):
        # A violation can be the expected experimental result. It remains a
        # violation in retained domain data, while the experimental check passes.
        matrix = [
            ("valid", "satisfied", "satisfied", "completed", "passed"),
            ("expected-violation", "violated", "violated", "completed", "passed"),
            ("unexpected", "violated", "satisfied", "completed", "failed"),
            ("malformed", "malformed", "satisfied", None, None),
            ("nonzero", "nonzero", "satisfied", None, None),
            ("deadline", "timeout", "satisfied", None, None),
        ]
        for identifier, mode, expected, execution, check in matrix:
            with self.subTest(case=identifier):
                started = time.monotonic()
                run = self.run_plan(
                    self.plan([self.case(identifier, mode, expected)]),
                    timeout=0.05 if mode == "timeout" else 2.0,
                )
                summary = self.report(run)
                self.assertEqual(summary["schema_version"], 1)
                self.assertEqual(len(summary["cases"]), 1)
                record = summary["cases"][0]
                self.assertEqual(record["id"], identifier)
                self.assertTrue(self.artifact(run, record["request_path"]).is_file())
                if execution is not None:
                    self.assertEqual(record["execution"], execution)
                    self.assertEqual(record["check"], check)
                    self.assertEqual(summary["execution"], "completed")
                    self.assertEqual(summary["checks"], check)
                    result = self.document(run, record["result_path"])
                    self.assertEqual(result["status"], mode)
                else:
                    self.assertIn(record["execution"], {"error", "interrupted"})
                    self.assertIn(record["check"], {"failed", "incomplete"})
                    self.assertNotEqual(summary["checks"], "passed")
                if mode == "timeout":
                    self.assertLess(time.monotonic() - started, 2.5)

    def test_repeat_uses_saved_inputs_in_a_fresh_run(self):
        source = self.write_json("inputs/value.json", {"limit": 7})
        case = {
            "id": "echo",
            "name": "contract.evaluate",
            "arguments": {"fixture_mode": "echo", "payload": {"$file": "inputs/value.json"}},
            "expect": {"/echo": {"limit": 7}},
        }
        path = self.plan([case])
        first = self.run_plan(path)
        before = self.frozen_files(first)
        source.write_text('{"limit": 99}\n', encoding="utf-8")
        path.unlink()
        second = self.run_plan(path, repeat=first)
        self.assertNotEqual(first, second)
        old, new = self.report(first), self.report(second)
        self.assertNotEqual(old["id"], new["id"])
        self.assertEqual(new["checks"], "passed")
        self.assertEqual(
            self.document(first, old["cases"][0]["request_path"]),
            self.document(second, new["cases"][0]["request_path"]),
        )
        self.assertEqual(self.frozen_files(first), before)
        self.assertTrue((second / "effective-plan.json").is_file())
        copied = list((second / "inputs").rglob("*"))
        self.assertTrue(any(p.is_file() for p in copied), "A repeat must retain verified input blobs")

    def test_result_reference_keeps_candidate_fixed_against_changed_observation(self):
        self.write_json("inputs/original.json", {"revision": "before", "limit": 7})
        self.write_json("inputs/changed.json", {"revision": "after", "limit": 9})
        cases = [
            {
                "id": "derive",
                "name": "contract.derive",
                "arguments": {"fixture_mode": "derive", "input": {"$file": "inputs/original.json"}},
                "expect": {"/status": "candidate"},
            },
            {
                "id": "original-check",
                "name": "contract.evaluate",
                "arguments": {
                    "fixture_mode": "candidate_check",
                    "contract": {"$result": "derive"},
                    "input": {"$file": "inputs/original.json"},
                },
                "expect": {"/status": "satisfied"},
            },
            {
                "id": "changed-check",
                "name": "contract.evaluate",
                "arguments": {
                    "fixture_mode": "candidate_check",
                    "contract": {"$result": "derive"},
                    "input": {"$file": "inputs/changed.json"},
                },
                "expect": {"/status": "violated"},
            },
        ]
        run = self.run_plan(self.plan(cases))
        summary = self.report(run)
        self.assertEqual(summary["checks"], "passed")
        by_id = {case["id"]: case for case in summary["cases"]}
        candidate = self.document(run, by_id["derive"]["result_path"])
        request = self.document(run, by_id["changed-check"]["request_path"])
        self.assertEqual(request["arguments"]["contract"], candidate)
        self.assertEqual(candidate["source_revision"], "before")
        self.assertEqual(request["arguments"]["input"]["revision"], "after")
        result = self.document(run, by_id["changed-check"]["result_path"])
        self.assertEqual(result["status"], "violated")

    def test_cue_file_reference_retains_source_and_definition(self):
        source = "#Contract: { value!: int & >=1 }\n"
        (self.root / "inputs" / "contract.cue").write_text(source, encoding="utf-8")
        expanded = {"source": source, "definition": "#Contract"}
        case = {
            "id": "cue-source",
            "name": "contract.evaluate",
            "arguments": {"fixture_mode": "echo", "payload": {"$file": "inputs/contract.cue"}},
            "expect": {"/echo": expanded},
        }
        run = self.run_plan(self.plan([case]))
        summary = self.report(run)
        self.assertEqual(summary["checks"], "passed")
        result = self.document(run, summary["cases"][0]["result_path"])
        self.assertEqual(result["echo"], expanded)

    def test_file_data_is_opaque_to_reference_resolution_and_repeat(self):
        payload = {
            "$file": "ordinary-domain-value",
            "nested": {"$result": "ordinary-domain-value"},
        }
        source = self.write_json("inputs/literal-markers.json", payload)
        path = self.plan([{
            "id": "literal-markers",
            "name": "contract.evaluate",
            "arguments": {"fixture_mode": "echo", "payload": {"$file": "inputs/literal-markers.json"}},
            "expect": {"/echo": payload},
        }])
        first = self.run_plan(path)
        self.assertEqual(self.report(first)["checks"], "passed")
        source.unlink()
        second = self.run_plan(path, repeat=first)
        summary = self.report(second)
        self.assertEqual(summary["checks"], "passed")
        result = self.document(second, summary["cases"][0]["result_path"])
        self.assertEqual(result["echo"], payload)

    def test_nonfinite_deadlines_are_rejected(self):
        path = self.plan([self.case("deadline", "satisfied")])
        for timeout in [float("nan"), float("inf"), -float("inf"), 0.0]:
            with self.subTest(timeout=timeout):
                with self.assertRaises(ValueError):
                    self.run_plan(path, timeout=timeout)

    def test_selection_runs_only_the_requested_scenario(self):
        path = self.write_json("plans/selection.json", {
            "schema_version": 1,
            "scenarios": [
                {"id": "first", "cases": [self.case("unselected", "violated")]},
                {"id": "second", "cases": [self.case("selected", "satisfied")]},
            ],
        })
        run = self.run_plan(path, selected=["second"])
        summary = self.report(run)
        self.assertEqual(summary["checks"], "passed")
        self.assertEqual([case["id"] for case in summary["cases"]], ["selected"])

    def test_invalid_manifests_are_rejected(self):
        no_expect = self.case("missing-expect", "satisfied")
        no_expect.pop("expect")
        duplicate = self.case("duplicate", "satisfied")
        for name, cases in [
            ("missing-expect", [no_expect]),
            ("empty-expect", [{**self.case("empty", "satisfied"), "expect": {}}]),
            ("duplicate-case", [duplicate, dict(duplicate)]),
        ]:
            with self.subTest(case=name):
                with self.assertRaises((ValueError, RuntimeError)):
                    self.run_plan(self.plan(cases))

    def test_escaped_input_is_rejected_without_provider_invocation(self):
        outside = self.root.parent / "outside.json"
        outside.write_text('{"outside":true}', encoding="utf-8")
        escaped = {
            "id": "escaped",
            "name": "contract.evaluate",
            "arguments": {"payload": {"$file": "../outside.json"}},
            "expect": {"/status": "satisfied"},
        }
        run = self.run_plan(self.plan([escaped]))
        summary = self.report(run)
        record = summary["cases"][0]
        self.assertEqual(record["execution"], "error")
        self.assertNotEqual(summary["checks"], "passed")
        self.assertFalse(record.get("request_path"))
        self.assertFalse(list((run / "output").glob("*.stdout")))

    def test_json_scalar_types_are_not_coerced(self):
        path = self.plan([{
            "id": "boolean-is-not-number",
            "name": "contract.evaluate",
            "arguments": {"fixture_mode": "echo", "payload": 1},
            "expect": {"/echo": True},
        }])
        run = self.run_plan(path)
        summary = self.report(run)
        self.assertEqual(summary["cases"][0]["execution"], "completed")
        self.assertEqual(summary["cases"][0]["check"], "failed")
        self.assertEqual(summary["checks"], "failed")

    def test_completed_case_is_saved_before_the_next_case_runs(self):
        cases = [
            self.case("before", "satisfied"),
            {
                "id": "checkpoint",
                "name": "contract.evaluate",
                "arguments": {"fixture_mode": "checkpoint", "runs_root": str(self.output_root)},
                "expect": {"/status": "satisfied"},
            },
            self.case("final-failure", "malformed"),
        ]
        run = self.run_plan(self.plan(cases))
        summary = self.report(run)
        by_id = {case["id"]: case for case in summary["cases"]}
        self.assertEqual(by_id["before"]["check"], "passed")
        self.assertEqual(by_id["checkpoint"]["check"], "passed")
        self.assertIn(by_id["final-failure"]["execution"], {"error", "interrupted"})
        self.assertNotEqual(summary["checks"], "passed")

    def test_repeat_rejects_tampered_saved_result(self):
        path = self.plan([self.case("identity", "satisfied")])
        run = self.run_plan(path)
        summary = self.report(run)
        result = self.artifact(run, summary["cases"][0]["result_path"])
        result.write_text('{"status":"violated"}\n', encoding="utf-8")
        with self.assertRaises((ValueError, RuntimeError)):
            self.run_plan(path, repeat=run)

    def test_repeat_rejects_tampered_saved_request(self):
        path = self.plan([self.case("identity", "satisfied")])
        run = self.run_plan(path)
        summary = self.report(run)
        request = self.artifact(run, summary["cases"][0]["request_path"])
        request.write_text('{"name":"contract.evaluate","arguments":{"changed":true},"context":{}}\n',
                           encoding="utf-8")
        with self.assertRaises((ValueError, RuntimeError)):
            self.run_plan(path, repeat=run)

    def test_repeat_rejects_tampered_copied_input(self):
        self.write_json("inputs/value.json", {"limit": 7})
        path = self.plan([{
            "id": "identity",
            "name": "contract.evaluate",
            "arguments": {"fixture_mode": "echo", "payload": {"$file": "inputs/value.json"}},
            "expect": {"/echo": {"limit": 7}},
        }])
        run = self.run_plan(path)
        inputs = [p for p in (run / "inputs").rglob("*") if p.is_file()]
        self.assertTrue(inputs, "The source input must be retained")
        inputs[0].write_bytes(b"tampered\n")
        with self.assertRaises((ValueError, RuntimeError)):
            self.run_plan(path, repeat=run)


if __name__ == "__main__":
    unittest.main()
