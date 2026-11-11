# Experiments

Use these as editable questions, not a fixed acceptance model for the broader idea.
Keep the changed input, contract, command, versions, and result together so a later
comparison describes the same experiment.

## 1. Separate observation, candidate, and intent

Inspect the existing recorded relationships, then derive a candidate for `parser`:

```sh
cargo run --locked -- derive --graph examples/graph.json --subject parser > candidate.json
cargo run --locked -- check --backend native --contract candidate.json --input examples/graph.json
```

The CLI accepts the candidate envelope as a native contract input.
Its `status: candidate` and source snapshot/revision/digest identify where it came from.
Native reports retain this envelope's identity in `contract_context` and digest the full wrapper.
Running a check does not approve its expectations.

Copy `examples/graph.json` to `graph-changed.json` and change
`nodes.parser.attributes.max_bytes` from 1024 to 2048. Keep `candidate.json` fixed:

```sh
cargo run --locked -- check --backend native --contract candidate.json --input graph-changed.json
```

Expected: the observed-attributes equality rule is violated.
Now decide whether 1024 was a requirement or just an observation.
For an intended range, author a separate native contract with this rule:

```json
{
  "schema_version": 1,
  "rules": [
    {
      "id": "allowed-parser-size",
      "pointer": "/nodes/parser/attributes/max_bytes",
      "kind": "integer_range",
      "min": 1,
      "max": 4096
    }
  ]
}
```

The changed value satisfies that range. This distinguishes capturing current structure
from choosing a future guarantee. Derivation also produces positive edge-existence rules;
it does not forbid extra edges or prove that all dependencies were observed.

Try `path` with `--max-depth 1` and then `--max-depth 2`.
The `caller` to `store` witness needs two edges.
Adding `--relation calls` removes the `uses` edge from the search.
A missing bounded witness describes this snapshot, not every possible runtime path.

## 2. Compare evaluator semantics

Start with the README's `compare` command.
For the supplied input, both authored contracts describe a valid example.
Edit a copy of `examples/input.json`, then pass its path with `--input`.

| Change | Native expectation | CUE expectation |
| --- | --- | --- |
| Set `limits.max_bytes` to 0 | `violated` | `violated` |
| Remove `limits.max_bytes` | `unknown` | `violated` because the field is required |
| Put another well-formed dependency before the store binding | `satisfied` | `violated` because the example requires the first binding |
| Remove the store binding entirely | `violated` | `violated` |
| Use an unavailable CUE executable | Native unchanged | `unsupported`; comparison is `not_comparable` |

The dependency-order difference is in these authored examples, not a claim that CUE
cannot express membership. Modify the CUE contract to search the list if that is the
agreement you want, then keep reordered and extra-binding examples as comparisons.

Native rules use JSON Pointers and `exists`, `equals`, `integer_range`, or `contains`.
A present `null` satisfies `exists`; an absent pointer is unknown.
`contains` searches arrays and matches expected object properties recursively.

A `.cue` file selects `#Contract`.
The programmatic CUE contract shape is
`{"source":"...","definition":"#Contract"}`.
CUE receives the original JSON separately from its source; unified defaults do not
replace the supplied observations. Syntax or definition errors are `error`.

Compare the diagnoses as well as the headline result:
Does the evaluator point to the useful clause? Can you express the intended agreement
without encoding incidental structure? What does each backend consider missing evidence?
Agreement on a finite example set does not establish equivalent languages or contracts.

## 3. Reuse, remaining obligations, and interactions

```sh
cargo run --locked -- match --request examples/reuse.json
cargo run --locked -- match --request examples/reuse-complete.json
cargo run --locked -- match --request examples/coupling-gap.json
```

| Request | Expected result within this model |
| --- | --- |
| `reuse.json` | Coverage `[5,10]`; residual `[1,4]`; incomplete |
| `reuse-complete.json` | Coverage `[1,10]`; no residual; complete |
| `coupling-gap.json` | Full interval coverage, but `shared_memory_within_budget` remains unresolved |

Try these independent edits:

1. Widen a primitive's accepted input. A wider input domain can serve a narrower request
   (input contravariance); a partial overlap contributes only that intersection.
2. Widen its promised output beyond `[0,100]`. It becomes incompatible: a usable output
   guarantee must stay inside the required output domain.
3. Add an assumption to `lower_range`. Until its exact name appears in
   `established_assumptions`, that primitive is conditional and contributes no coverage.
4. Add `shared_memory_within_budget` to the coupling example's established assumptions.
   The declared gap closes. This records a premise; it does not measure actual memory use.

Coverage combines alternative declarations over inclusive integer intervals.
The result does not synthesize a dispatcher, execute the primitives, or establish sequential
composition. Residual intervals are exact for this bounded calculation, not a general
quotient of behavioral contracts.

## 4. Change the implementation seam

| Edit | Question it helps answer |
| --- | --- |
| `examples/graph.json`, then a `SnapshotStore` adapter | Which source facts and identities are useful? |
| `GraphQuery` implementation | Which relationships and bounded queries help exploration? |
| `Evaluator` implementation | Which contract language and diagnostics fit the agreement? |
| `src/composition.rs` and its examples | Which additional assumptions or couplings change reuse? |
| `src/provider.rs` | Which compact operations help an existing host consume the results? |

Prefer one deliberate semantic change per comparison. Record the improvement and added
authoring, extraction, or verification effort. The current examples leave room for other
languages, source adapters, richer contracts, and different granularities of reuse.
