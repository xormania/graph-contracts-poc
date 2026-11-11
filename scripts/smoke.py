#!/usr/bin/env python3
"""Exercise the documented examples through the built executable."""
import json
import os
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
BINARY = ROOT / "target" / "debug" / ("gcp.exe" if os.name == "nt" else "gcp")
CUE = os.environ.get("CUE_BIN", "cue")


def invoke(*arguments):
    result = subprocess.run(
        [str(BINARY), "--cue", CUE, *map(str, arguments)],
        cwd=ROOT, check=True, capture_output=True, text=True, timeout=30,
    )
    return json.loads(result.stdout)


def main():
    observed = invoke("inspect", "--graph", "examples/graph.json")
    assert (observed["nodes"], observed["edges"]) == (3, 2)
    witness = invoke("path", "--graph", "examples/graph.json",
                     "--from", "caller", "--to", "store", "--max-depth", "2")
    assert len(witness["path"]) == 2
    candidate = invoke("derive", "--graph", "examples/graph.json", "--subject", "parser")
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "candidate.json"
        path.write_text(json.dumps(candidate), encoding="utf-8")
        report = invoke("check", "--contract", path, "--input", "examples/graph.json")
        assert report["status"] == "satisfied"
        assert report["contract_context"]["status"] == "candidate"
    for name, expected in [("input.json", "satisfied"), ("input-violated.json", "violated")]:
        report = invoke("compare", "--native", "examples/native.json",
                        "--cue-contract", "examples/contract.cue", "--input", f"examples/{name}")
        assert report["agreement"] == "agree", report
        assert report["native"]["status"] == report["cue"]["status"] == expected, report
    partial = invoke("match", "--request", "examples/reuse.json")
    assert partial["residual"] == [{"min": 1, "max": 4}]
    assert invoke("match", "--request", "examples/reuse-complete.json")["complete"]
    gap = invoke("match", "--request", "examples/coupling-gap.json")
    assert not gap["complete"] and not gap["residual"]
    assert any(item["missing_obligations"] for item in gap["coupling_findings"])
    print(json.dumps({"status": "passed", "examples": [
        "graph query", "candidate provenance", "native/CUE valid and violated",
        "partial reuse", "complete declared reuse", "coupling gap",
    ]}))


if __name__ == "__main__":
    main()
