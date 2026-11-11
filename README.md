# Graph/contracts PoC

A small Rust library and CLI for experimenting with graph observations,
independently authored contracts, alternative evaluators, and declared reuse.
Change the examples, inspect the results, and use that feedback to refine the concept.
Xorchestrate remains the host for orchestration.

## Start here

The repository pins Rust **1.99.1** in [rust-toolchain.toml](rust-toolchain.toml).
Native experiments need only that toolchain. CUE experiments additionally use
**CUE v0.17.1**; put that executable on PATH or pass its absolute path with `--cue`.

```sh
cargo build --locked
cargo run --locked -- inspect --graph examples/graph.json
cargo run --locked -- neighbors --graph examples/graph.json --subject parser
cargo run --locked -- path --graph examples/graph.json --from caller --to store --max-depth 2
cargo run --locked -- check --backend native --contract examples/native.json --input examples/input.json
cargo run --locked -- match --request examples/reuse.json
```

The supplied reuse example covers inputs `[5,10]` and leaves `[1,4]` unresolved.
These are declared integer intervals; the command does not execute their implementations.

To exercise CUE and compare the two authored contracts:

```sh
cargo run --locked -- --cue /absolute/path/to/cue check --backend cue --contract examples/contract.cue --input examples/input.json
cargo run --locked -- --cue /absolute/path/to/cue compare --native examples/native.json --cue-contract examples/contract.cue --input examples/input.json
```

A missing CUE executable produces `unsupported`. It never selects another backend.
The two example contracts deliberately retain their own semantics: native `contains`
finds a binding anywhere; the current CUE example requires it first. See
[experiments](docs/experiments.md) for useful disagreements and missing-fact cases.

## Commands and results

| Command | What to vary |
| --- | --- |
| `inspect` | Snapshot identity, nodes, edges, and structural validity |
| `neighbors` / `path` | Subject, direction, relation filter, and depth |
| `derive` | Subject whose positive observations become a contract candidate |
| `check` | Backend, native contract document, independent input |
| `compare` | Two authored contracts evaluated on the same input |
| `match` | Declared capabilities, input/output intervals, assumptions, couplings |
| `discover` / `provider` | CLI-provider discovery and one-request invocation |

Run `cargo run --locked -- --help` or append a command followed by `--help`.
Global `--timeout-ms` defaults to 5000 for each CUE process invocation.

Evaluation reports distinguish `satisfied`, `violated`, `unknown`, `unsupported`,
and `error`, with input/contract digests and backend identity.
A missing native pointer is `unknown`; a missing field required by a CUE contract
can be `violated`. Comparison reports `not_comparable` if either result is indeterminate.

Reports normally return exit 0 so experiments can inspect every outcome.
Add `--require-satisfied` to `check` to return exit 3 for every other status.
Invalid operation requests or file reads return exit 2.

## Change the setup

Start with [docs/experiments.md](docs/experiments.md).
The examples are small editable inputs, rather than a generated baseline that changes
with the implementation. Keep an intended contract fixed while changing its observations.

The crate `graph_contracts_poc` can be used without the CLI.
[docs/design.md](docs/design.md) describes `GraphQuery`, `SnapshotStore`, `Evaluator`,
and the deliberately bounded composition experiment.
Current graph input is supplied JSON; CodeGraph/Mago extraction adapters are further experiments.

## Use through Xorchestrate

Build `target/release/gcp` with `cargo build --release --locked`.
Add this provider fragment to an existing Xorchestrate project configuration,
replacing both executable paths. The CUE path is needed only for CUE requests.

```json
{
  "providers": {
    "graph_contracts": {
      "adapter": "cli",
      "options": {
        "command": ["/absolute/path/gcp", "--cue", "/absolute/path/cue", "provider"],
        "discovery_command": ["/absolute/path/gcp", "--cue", "/absolute/path/cue", "provider"],
        "timeout": 30
      }
    }
  }
}
```

`provider` reads one JSON value from stdin and writes one JSON result.
Discovery accepts `{"operation":"discover"}`; invocation accepts
`{"name":"capability.name","arguments":{...},"context":{...}}`.
The domain documents are inline arguments. `gcp discover` prints the capability list directly.

This follows Xorchestrate's
[CLIProvider boundary](https://github.com/xormania/xorchestrate/blob/master/xorchestrate/adapters/capabilities.py).
The host owns discovery snapshots, authorization, sessions, and scheduling.
The executable supplies CLI-provider operations; it does not implement an MCP handshake.
Mapping domain reports into an xcontracts `Assessment` is a separate adapter concern.

## Check changes

```sh
cargo test --locked
CUE_BIN=/absolute/path/to/cue cargo test --locked --test cue -- --ignored
python3 scripts/prove_red.py
```

Ordinary tests include the absent-CUE case. Unix fake-process tests and the fault runner
require Python 3. Real CUE tests are selected with the second command.
The fault runner checks that deliberate defects are detected;
consult its output and CI artifacts for the observed results of a revision.

Copyright 2026 xormania.
