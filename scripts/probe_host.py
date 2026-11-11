#!/usr/bin/env python3
"""Exercise the real Xorchestrate CLIProvider against the built Rust app."""

from __future__ import annotations

import argparse
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone
import importlib.metadata
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from poc_setup import digest, host_python


def probe(root: Path, cue: Path | None = None, binary: Path | None = None) -> dict:
    root = root.resolve()
    cue = (cue or root / ".poc" / "bin" / "cue").absolute()
    binary = (binary or root / "target" / "debug" / ("gcp.exe" if os.name == "nt" else "gcp")).absolute()
    summary = {"schema_version": 1, "boundary": "real_xorchestrate_cli_provider",
               "status": "failed", "started_at": datetime.now(timezone.utc).isoformat(), "checks": []}
    providers = []

    def check(name, condition, observed):
        summary["checks"].append({"name": name, "status": "passed" if condition else "failed",
                                  "observed": observed})
        if not condition:
            raise AssertionError(f"Host integration check failed: {name}")

    try:
        import xorchestrate
        import xcontracts
        from xcontracts.model import OrchestrationError
        from xcontracts.ports import CapabilityProvider
        from xcontracts.testing import factory

        source = root / ".poc" / "host" / "source"
        lock = json.loads((root / "poc" / "host-lock.json").read_text(encoding="utf-8"))
        if source.resolve() not in Path(xorchestrate.__file__).resolve().parents:
            raise RuntimeError(f"Use the prepared host interpreter: {host_python(root)}")
        revision = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=source, check=True, capture_output=True,
            text=True, timeout=10,
        ).stdout.strip()
        if revision != lock["revision"]:
            raise RuntimeError("Installed host source does not match poc/host-lock.json.")
        origin_text = importlib.metadata.distribution("xcontracts").read_text("direct_url.json")
        origin = json.loads(origin_text) if origin_text else {}
        installed_revision = origin.get("vcs_info", {}).get("commit_id")
        check("xcontracts_source_pair", installed_revision == lock["xcontracts_revision"],
              {"expected_revision": lock["xcontracts_revision"], "installation_origin": origin})
        summary["host"] = {
            "revision": revision, "uv_lock_sha256": digest(source / "uv.lock"),
            "xorchestrate": importlib.metadata.version("xorchestrate"),
            "xcontracts": importlib.metadata.version("xcontracts"),
            "xcontracts_revision": installed_revision,
            "python": sys.version.split()[0],
            "module": str(Path(xorchestrate.__file__).resolve()),
            "xcontracts_module_sha256": digest(Path(xcontracts.__file__)),
        }
        summary["gcp"] = {"path": str(binary), "sha256": digest(binary)}
        summary["cue"] = {"path": str(cue), "available": cue.is_file()}
        if cue.is_file():
            summary["cue"]["sha256"] = digest(cue)

        def provider_for(cue_binary):
            command = [str(binary), "--cue", str(cue_binary), "provider"]
            instance = factory.construct("providers", {
                "adapter": "xorchestrate.adapters.capabilities:CLIProvider",
                "options": {"command": command, "discovery_command": command,
                            "cwd": str(root), "timeout": 30},
            })
            providers.append(instance)
            return instance

        provider = provider_for(cue)
        check("public_port_structure", isinstance(provider, CapabilityProvider), type(provider).__name__)
        capabilities = {item.name: item for item in provider.discover()}
        required = {"graph.inspect", "graph.neighbors", "graph.path", "contract.derive",
                    "contract.evaluate", "contract.compare", "reuse.analyze"}
        check("discovery", required <= capabilities.keys(), sorted(capabilities))

        def invoke(name, arguments, instance=provider, catalog=capabilities):
            return instance.invoke(name, arguments, {"capability": asdict(catalog[name])})

        def fixture(name):
            return json.loads((root / "examples" / name).read_text(encoding="utf-8"))

        graph = fixture("graph.json")
        report = invoke("graph.inspect", {"graph": graph})
        check("graph_observations", (report["nodes"], report["edges"]) == (3, 2), report)
        report = invoke("graph.path", {"graph": graph, "from": "caller", "to": "store", "max_depth": 2})
        check("bounded_path", len(report["path"]) == 2, report)
        candidate = invoke("contract.derive", {"graph": graph, "subject": "parser"})
        report = invoke("contract.evaluate", {"backend": "native", "contract": candidate, "input": graph})
        check("candidate_context", report["status"] == "satisfied"
              and report["contract_context"]["status"] == "candidate"
              and report["contract_context"]["source"] == candidate["source"], report)

        native, valid, violated = fixture("native.json"), fixture("input.json"), fixture("input-violated.json")
        missing = deepcopy(valid)
        del missing["limits"]["max_bytes"]
        matrix = [
            ("native_satisfied", native, valid, "satisfied"),
            ("native_violated", native, violated, "violated"),
            ("native_unknown", native, missing, "unknown"),
            ("native_error", {"schema_version": 1, "rules": []}, valid, "error"),
        ]
        for name, contract, observation, expected in matrix:
            report = invoke("contract.evaluate", {"backend": "native", "contract": contract, "input": observation})
            check(name, report["status"] == expected, report)

        cue_contract = {"source": (root / "examples" / "contract.cue").read_text(encoding="utf-8"),
                        "definition": "#Contract"}
        if cue.is_file():
            for name, observation, expected in (
                ("cue_satisfied", valid, "satisfied"), ("cue_violated", violated, "violated"),
            ):
                report = invoke("contract.evaluate", {"backend": "cue", "contract": cue_contract, "input": observation})
                check(name, report["status"] == expected, report)
            report = invoke("contract.compare", {"native": native, "cue": cue_contract, "input": valid})
            check("backend_comparison", report["agreement"] == "agree", report)
        else:
            summary["checks"].append({"name": "real_cue", "status": "not_run",
                                      "reason": "CUE executable is unavailable; native and missing-backend checks still run."})

        with tempfile.TemporaryDirectory(prefix="gcp-missing-cue-") as temporary:
            absent = provider_for(Path(temporary) / "not-installed")
            absent_catalog = {item.name: item for item in absent.discover()}
            report = invoke("contract.evaluate", {"backend": "cue", "contract": cue_contract, "input": valid},
                            instance=absent, catalog=absent_catalog)
            check("cue_unsupported", report["status"] == "unsupported", report)

        for name, expected in (("reuse.json", False), ("reuse-complete.json", True), ("coupling-gap.json", False)):
            report = invoke("reuse.analyze", {"request": fixture(name)})
            check(name, report["complete"] is expected, report)

        def expect_error(name, action, expected):
            try:
                action()
            except OrchestrationError as error:
                check(name, error.code == expected and error.details.get("outcome") == "not_started",
                      {"code": error.code, "details": error.details})
            else:
                check(name, False, {"error": "Expected the host to reject the request."})

        expect_error("invalid_arguments_before_launch", lambda: invoke(
            "graph.path", {"graph": graph, "from": "caller", "to": "store", "max_depth": "two"}),
            "invalid_arguments")
        changed = asdict(capabilities["graph.inspect"])
        changed["description"] += " changed"
        expect_error("descriptor_mismatch", lambda: provider.invoke(
            "graph.inspect", {"graph": graph}, {"capability": changed}), "capability_changed")
        provider.close()
        expect_error("closed_provider", lambda: invoke("graph.inspect", {"graph": graph}), "provider_unavailable")
        summary["status"] = "passed"
    except ImportError as error:
        summary["error"] = f"{error}. Run setup --terminal, then use {host_python(root)} to run this probe."
    except Exception as error:
        summary["error"] = f"{type(error).__name__}: {error}"
    finally:
        for provider in providers:
            try:
                provider.close()
            except Exception as error:
                summary["status"] = "failed"
                summary["checks"].append({"name": "provider_cleanup", "status": "failed",
                                          "observed": f"{type(error).__name__}: {error}"})
        summary["finished_at"] = datetime.now(timezone.utc).isoformat()
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--cue", type=Path)
    parser.add_argument("--binary", type=Path)
    parser.add_argument("--output", type=Path, help="Write an exclusive JSON receipt; an existing file is refused.")
    arguments = parser.parse_args()
    if arguments.output and arguments.output.exists():
        parser.error(f"Output already exists: {arguments.output}")
    result = probe(arguments.root, cue=arguments.cue, binary=arguments.binary)
    if arguments.output:
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(arguments.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(result, stream, indent=2, sort_keys=True)
            stream.write("\n")
    print(json.dumps(result, sort_keys=True))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
