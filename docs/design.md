# Design and current boundaries

The PoC separates recorded relationships, intended agreements, and evaluation.
It provides reusable Rust operations with a thin CLI and CLI-provider adapter.
Xorchestrate owns orchestration; no second session or workflow engine is introduced.

## Modules and ports

| Module | Responsibility |
| --- | --- |
| [graph](../src/graph.rs) | Snapshot values, structural validation, deterministic queries, candidate derivation |
| [evaluation](../src/evaluation.rs) | Native and CUE contract evaluation through `Evaluator` |
| [composition](../src/composition.rs) | Declared interval coverage, assumptions, coupling gaps, residual intervals |
| [provider](../src/provider.rs) | Discovery and JSON argument/result mapping to domain operations |
| [main](../src/main.rs) | CLI argument parsing, bounded file/stdin input, result presentation |

`GraphQuery` supplies outbound neighbors and bounded shortest-path witnesses.
`SnapshotStore` loads a validated snapshot; memory and JSON-file implementations exist.
`Evaluator` receives a backend-native contract and independent JSON input.
These seams do not require a graph database, CUE runtime, or host orchestration inside the domain.

## Use the library directly

```rust
use graph_contracts_poc::graph::{
    GraphQuery, JsonFileSnapshotStore, SnapshotStore,
};

fn main() -> Result<(), String> {
    let graph = JsonFileSnapshotStore {
        path: "examples/graph.json".into(),
    }
    .load()?;
    let witness = graph.path("caller", "store", None, 2)?;
    println!("{witness:?}");
    Ok(())
}
```

For evaluation, import `evaluation::{Evaluator, NativeEvaluator}` and call
`NativeEvaluator.evaluate(&contract, &input)`, or construct
`CueEvaluator::new(binary_path, timeout)`.
The caller chooses storage, evaluator, and host integration independently.

## Identities and evidence

A graph snapshot has a schema version, identity, revision, nodes, and directed edges.
Kinds and relation names are supplied labels. Validation checks structural consistency;
a caller-supplied revision does not verify a live checkout.
Queries are cycle-safe, outbound, deterministic, and bounded by edge depth.

A derived candidate retains its originating observation identity and positive expectations.
An intended contract is authored or adopted separately. Checking fresh observations must
not silently regenerate that intended baseline.
Evidence strings remain opaque references; their existence does not establish their contents.

Evaluation reports identify the backend, available backend version, input, and contract.
Graph operations digest the normalized snapshot, labeled `normalized_graph_snapshot_v1`.
Generic evaluation digests the original supplied JSON; native candidate evaluation digests
the full wrapper and retains candidate source identity in `contract_context`.
Digests use this version's serialized `serde_json::Value`; this is not an RFC 8785
canonicalization guarantee or a claim that differently represented observations are equivalent.

## Evaluator choices

Native rules inspect JSON Pointers. Missing observations are unknown; observed contradictions
are violations. An existing null value differs from a missing field.
Malformed contracts are errors, including empty rule sets and invalid bounds.

CUE contracts retain their source and selected definition. The adapter preflights the
definition and validates independent input with CUE. Required fields can make missing data
a violation. It retains the raw-input identity rather than exporting unified defaults as facts.
An absent executable is unsupported; backend errors never cause fallback.

The shared report is an interchange boundary, not a universal contract language.
Comparison evaluates two separately authored contracts on one input.
Its agreement field describes those results only.

## Earlier compositional ideas considered

| Prior direction | Present experiment and remaining question |
| --- | --- |
| Reuse existing capabilities before realizing the remainder | Declared alternatives cover integer domains; discovery and realization are open extensions |
| Interactions carry obligations of their own | Explicit coupling gaps can prevent a complete result despite full interval coverage |
| Completed compositions can become reusable capabilities | Recursive promotion and multiple granularities remain experiments to design |
| Refinement and equivalence matter | Directional input/output coverage is explored; general behavioral refinement is unimplemented |
| Compare marginal decomposition utility with marginal cost | Authoring and verification effort can be recorded; no economic optimizer is implemented |
| Contract boundaries may illuminate an entropy-based account | The hypothesis is retained; no entropy metric or physical equivalence is asserted |

The interval model explores one tractable part of those ideas. Its data fields do not
establish a permanent ontology or prescribe a universal decomposition granularity.
Further source, state, temporal, resource, generation, or evaluation approaches remain options.

## Host boundary

The provider accepts one request and returns one result. Arguments contain inline domain
documents; transport context does not grant authority.
Xorchestrate's existing CLI provider can own discovery snapshots, authorization, and invocation.
A full MCP transport and a projection into an xcontracts `Assessment` are separate adapters.
The Rust `Evaluator` trait is the local evaluation seam; it is not the Python host interface.

The code is an experiment to modify. New cases should expose the distinction they investigate
and preserve the inputs needed to reproduce an observed result.

## Local experiment tooling

scripts/poc.py is a development operator for bounded sequential provider requests,
input/result capture, and fresh replay. It does not change the Rust library's ports.
scripts/poc_terminal.py consumes Xorchestrate's selected TerminalHost; its receipts
remain opaque and host lifecycle stays in that implementation. The optional Python
environment is installed from the explicit source pair in poc/host-lock.json.

[The local workflow guide](local-workflow.md) records the prior PoC patterns,
the current terminal-port boundary, and the differences between experiment checks,
domain outcomes, terminal observations, and verified cleanup.
