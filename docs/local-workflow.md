# Local experiment workflow

The Rust library and gcp executable remain the application. The small Python
operator layer prepares local tools, submits bounded experiment requests, and
retains their results. Optional terminal control consumes Xorchestrate's existing
TerminalHost port.

## Prepare once

The local workflow targets Linux/WSL2, including Debian on a Chromebook. Native
experiments need Python 3.11+ and an existing Rust installation that can select
the repository's pinned toolchain. The CUE installer currently verifies the
Linux amd64 v0.17.1 archive. Setup builds the Rust executable, checks the archive
checksum, and writes local tools and setup receipts under .poc.

From this checkout:

```sh
python3 scripts/poc.py setup
python3 scripts/poc.py doctor
python3 scripts/poc.py list
python3 scripts/poc.py run
```

For native-only work, use setup --no-cue and select graph, candidate-drift,
native, or reuse scenarios. An externally installed CUE can be selected with
the global --cue /absolute/path/cue option or CUE_BIN.

Optional terminal/provider integration additionally requires Python 3.12+,
git, and uv. Interactive work requires tmux. Setup reports missing prerequisites
and preserves its command logs; it does not install system packages.

```sh
python3 scripts/poc.py setup --terminal
python3 scripts/poc.py terminal doctor
python3 scripts/poc.py probe-host
```

The optional setup fetches the exact Xorchestrate revision in
[poc/host-lock.json](../poc/host-lock.json), then uses its committed uv.lock.
The source and environment are isolated at .poc/host/source. Repeated setup
reuses a clean matching checkout and refuses to reset modified source or replace
a different local CUE binary. It does not edit the operator's Xorchestrate home.

Current tested source selection:

- Xorchestrate PR #70: bccc822b03e1272dc0675e62cc4b1036605d047a.
- xcontracts: 6abe0e368b46f228edc69354b3bbe24413a00836.
- TerminalHost is provisional in xorchestrate.ports at that revision. It has
  not been presented as a published xcontracts terminal API.
- The Rust package does not depend on the Python host environment.

## Select an experiment

[poc/experiments.json](../poc/experiments.json) is the editable scenario plan.

| Scenario | Cases | Question |
| --- | --- | --- |
| graph | 3 | What relationships are recorded, and what does a bounded path establish? |
| candidate-drift | 3 | Does a candidate retain its original source while observations change? |
| native | 4 | How do native rules treat valid, contradictory, missing, and reordered facts? |
| cue | 4 | How does the authored CUE contract treat those same variations? |
| compare | 3 | Where do the two authored contracts agree or differ? |
| reuse | 3 | What coverage remains, and can a coupling gap prevent completion? |

```sh
python3 scripts/poc.py run --scenario candidate-drift
python3 scripts/poc.py run --scenario native --scenario cue --scenario compare
python3 scripts/poc.py run --scenario reuse
```

Run and repeat rebuild the Rust app with the committed dependency lock by
default. Use --no-build when intentionally using an already built executable.
Use the global --binary /absolute/path/gcp option to select another implementation;
that explicit selection is not rebuilt.

Each run prints a compact per-case summary and its saved directory. Use the
global --json option for machine-readable output:

```sh
python3 scripts/poc.py --json run --scenario native
```

Each case names an existing provider operation, supplies arguments, and states
expected JSON Pointer values. For example:

```json
{
  "id": "native-valid",
  "name": "contract.evaluate",
  "arguments": {
    "backend": "native",
    "contract": {"$file": "examples/native.json"},
    "input": {"$file": "examples/input.json"}
  },
  "expect": {"/status": "satisfied"}
}
```

$file refers to a file inside this project. JSON is loaded as data; a .cue file
becomes a CUE source/definition document. Loaded data is opaque to plan reference
syntax. $result names an earlier case's complete returned value. The candidate
scenario uses that explicit reference to keep the captured candidate fixed while
checking original and changed observations. References are a sequential convenience
for these experiments; the runner does not schedule agents or create host sessions.

A custom plan can be selected with run --plan PATH. Case IDs must be unique
across the plan, and each case needs at least one explicit expectation.
Observation files and intended contracts remain separately editable.

## Read, repeat, and compare

Replace RUN_DIRECTORY with the directory printed by run:

```sh
python3 scripts/poc.py show RUN_DIRECTORY
python3 scripts/poc.py repeat RUN_DIRECTORY
python3 scripts/poc.py compare FIRST_RUN_DIRECTORY SECOND_RUN_DIRECTORY
```

Repeat verifies the saved plan, copied inputs, requests, and results, then creates
a new run using the captured effective requests and expectations. It does not
reread the original input files, reuse an earlier verdict, or overwrite the earlier
run. A candidate supplied to a check therefore remains the captured candidate.
The earlier derive operation can also run again, and its new result is visible
separately; replayed check requests retain their saved contract.

The currently selected implementation is used for repeat. Binary fingerprints
and comparison output show whether the implementation changed. This is useful
when comparing a Rust change against fixed inputs.

A selection whose requests were never captured cannot be replayed exactly.
Select a captured scenario, or start the authored plan as a new run. Failed
executions with captured requests can be repeated. Interrupted and incomplete
evidence remains available.

The report keeps distinct meanings:

- A domain result such as violated, unknown, unsupported, or error remains the
  backend's result.
- A case check says whether the observed JSON matches the experiment's expected
  values. An expected violation can have a passing check and still be violated.
- Execution errors, invalid output, and timeouts cannot pass a case check.
- Overall checks become incomplete if the selected executable changes during the
  run. Before/after binary fingerprints are retained.
- An interrupted run remains incomplete; an absent final record is not completion.

Each private run directory contains:

| Artifact | Contents |
| --- | --- |
| plan.json | Selected authored plan for this attempt |
| effective-plan.json | Captured arguments and expectations used for replay |
| source-inputs.json and inputs/ | Original file identities and copied file bytes |
| requests/ and results/ | Exact dispatched requests and parsed domain results, with digests |
| output/ | Bounded stdout/stderr for each invocation |
| run.json | Progress, execution/check outcomes, identities, timing, and diagnostics |

The runner checkpoints after each case. Provider and version commands have
deadlines, bounded retained output, and cleanup of their own POSIX process group.
A process that deliberately creates another session is outside this narrow
command-runner cleanup guarantee. Xorchestrate owns terminal lifecycle.

Default case timeout is 30 seconds; --timeout changes it to another positive
finite value. Failed/incomplete experimental checks return exit 3; invalid
operator requests and setup errors return exit 2.

## Work in a reusable terminal

```sh
python3 scripts/poc.py terminal start
python3 scripts/poc.py terminal attach
```

This opens an actual interactive Bash terminal in this checkout. The app and
local CUE directories are added to its PATH. Run the same experiment commands,
edit files, or use Cargo there. Under the default tmux host, Ctrl+B then D detaches
the viewer while the terminal continues.

From another terminal:

```sh
python3 scripts/poc.py terminal status
python3 scripts/poc.py terminal output --lines 100
python3 scripts/poc.py terminal attach --print-only
python3 scripts/poc.py terminal stop
```

--name selects another named work terminal. Controls use the saved opaque
receipt and the adapter/options selected at launch. Changes to home settings
do not silently redirect controls to another host. A duplicate running name is
refused; a verified stopped name can be started again.

Xorchestrate chooses the adapter using its home settings. Its default is tmux
with a detached-process fallback. The actual adapter and fallback reason are
reported. A noninteractive fallback cannot provide the interactive work terminal,
so that request is refused explicitly. The detached adapter is exercised in the
integration check for bounded programmatic commands.

Attach obtains a fresh instruction from the port and executes its argv without
a shell. Viewer exit says nothing about experiment acceptance. Stop delegates
cleanup to the host, reports cleanup_verified, and retains experiment files and
terminal logs. An unrecoverable detached receipt remains unknown; it is not
converted into a successful exit. This operator does not create native harness
sessions or duplicate Xorchestrate's agent launchers.

## Verify the boundaries

Ordinary development checks:

```sh
python3 -m unittest discover -s tests -p 'test_poc_workflow.py' -v
python3 scripts/prove_poc_red.py
python3 scripts/poc.py probe-host
.poc/host/source/.venv/bin/python scripts/prove_terminal_red.py
```

The provider probe uses the actual CLIProvider and discovery-bound xcontracts
capability descriptors. It passes absolute app/CUE paths because that provider
starts with an empty environment by default. It retains its report in .poc/probes.

The terminal probe uses both real adapters, a separate launcher process, literal
input, saved-selection restoration, retained logs, and independent cleanup checks.
Its calibration plants a false cleanup claim and requires the independent check
to reject it. These are local protocol/terminal fixtures; no model accounts or
authenticated native-agent claims are involved.

The existing contract.evaluate capability returns a domain report. Xorchestrate's
ToolEvaluator expects an Assessment and adds its own question/context arguments.
An Assessment projection is a separate explicit adapter concern.

## Reused source patterns

| Reference | What this setup carries forward |
| --- | --- |
| [multi-harness-proof operations](https://github.com/xormania/multi-harness-proof/blob/ad294398501ed119a436db54eca784be2fb10e76/OPERATIONS.md) | Stable entry point, retained attempts, fresh repeat from saved inputs |
| [Mago/Serena testing](https://github.com/xormania/mago-serena_v2-poc/blob/b6dbec8378338e39fe51489bc28f9ad578e4f3ab/docs/TESTING.md) | Independent checks, retained failure evidence, clear expected-failure semantics |
| [Agentscient terminal control](https://github.com/xormania/agentscient/blob/a3693e7a707a3b332ce5da410ce7d0cfea78d6a2/docs/tmux-control.md) | Owned terminals, current environment, readiness distinct from terminal existence |
| [Xorchestrate PR #70](https://github.com/xormania/xorchestrate/pull/70) | Actual reusable TerminalHost selection and lifecycle operations |
| [Xorchestrate provider adapter](https://github.com/xormania/xorchestrate/blob/bccc822b03e1272dc0675e62cc4b1036605d047a/xorchestrate/adapters/capabilities.py) | Real capability discovery/invocation boundary |

These are source references for concrete capabilities and prior lessons. Current
explicit direction from xor remains authoritative.
