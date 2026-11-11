use graph_contracts_poc::composition::{self, MatchRequest};
use graph_contracts_poc::evaluation::{Evaluator, NativeEvaluator};
use graph_contracts_poc::graph::{self, GraphQuery, GraphSnapshot};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};

fn interval(min: i64, max: i64) -> Value {
    json!({"min": min, "max": max})
}

fn interface(input: (i64, i64), output: (i64, i64)) -> Value {
    json!({
        "input": interval(input.0, input.1),
        "output": interval(output.0, output.1)
    })
}

fn primitive(id: &str, input: (i64, i64), output: (i64, i64)) -> Value {
    json!({
        "id": id,
        "contract": interface(input, output),
        "assumptions": [],
        "evidence": [format!("fixture:{id}")]
    })
}

fn request(primitives: Vec<Value>) -> Value {
    json!({
        "schema_version": 1,
        "required": interface((1, 10), (1, 10)),
        "primitives": primitives,
        "established_assumptions": [],
        "couplings": []
    })
}

fn analyze(value: Value) -> Value {
    let request: MatchRequest = serde_json::from_value(value).expect("valid test request");
    serde_json::to_value(composition::analyze(&request).expect("valid composition"))
        .expect("serializable report")
}

fn snapshot_value() -> Value {
    json!({
        "schema_version": 1,
        "id": "fixture",
        "revision": "rev-1",
        "nodes": {
            "root": {"kind": "consumer", "attributes": {"version": 1}, "evidence": ["source:root"]},
            "a": {"kind": "adapter", "attributes": {}, "evidence": []},
            "b": {"kind": "adapter", "attributes": {}, "evidence": []},
            "c": {"kind": "provider", "attributes": {}, "evidence": []}
        },
        "edges": [
            {"id": "z", "from": "root", "to": "b", "relation": "uses", "evidence": ["source:z"]},
            {"id": "ba", "from": "b", "to": "a", "relation": "uses", "evidence": []},
            {"id": "ab", "from": "a", "to": "b", "relation": "uses", "evidence": []},
            {"id": "a", "from": "root", "to": "a", "relation": "uses", "evidence": ["source:a"]},
            {"id": "observe", "from": "root", "to": "b", "relation": "observes", "evidence": []},
            {"id": "bc", "from": "b", "to": "c", "relation": "uses", "evidence": []}
        ]
    })
}

fn snapshot(value: Value) -> GraphSnapshot {
    serde_json::from_value(value).expect("valid graph shape")
}

fn native(contract: &Value, input: &Value) -> Value {
    serde_json::to_value(NativeEvaluator.evaluate(contract, input))
        .expect("serializable evaluation")
}

fn rules(rules: Vec<Value>) -> Value {
    json!({"schema_version": 1, "rules": rules})
}

fn range_rule() -> Value {
    json!({"id": "count-range", "pointer": "/count", "kind": "integer_range", "min": 1, "max": 10})
}

fn digest(value: &Value) -> String {
    format!("{:x}", Sha256::digest(serde_json::to_vec(value).unwrap()))
}

#[test]
fn directional_matching_rejects_overlapping_output_guarantees() {
    let cases = [
        ((1, 10), "complete", true),
        ((2, 8), "complete", true),
        ((5, 15), "incompatible", false),
        ((0, 5), "incompatible", false),
        ((11, 20), "incompatible", false),
    ];
    for (output, status, complete) in cases {
        let report = analyze(request(vec![primitive("p", (1, 10), output)]));
        assert_eq!(report["matches"][0]["status"], status, "{output:?}");
        assert_eq!(report["complete"], complete, "{output:?}");
        if !complete {
            assert_eq!(report["residual"], json!([interval(1, 10)]));
        }
    }
}

#[test]
fn partial_reuse_retains_exact_remaining_input_cases() {
    let cases = [
        ((5, 15), json!([interval(1, 4)]), "partial"),
        ((-5, 5), json!([interval(6, 10)]), "partial"),
        ((3, 7), json!([interval(1, 2), interval(8, 10)]), "partial"),
        ((11, 15), json!([interval(1, 10)]), "incompatible"),
    ];
    for (input, residual, status) in cases {
        let report = analyze(request(vec![primitive("p", input, (2, 8))]));
        assert_eq!(report["matches"][0]["status"], status, "{input:?}");
        assert_eq!(report["residual"], residual, "{input:?}");
        assert_eq!(report["complete"], false);
        assert_eq!(report["basis"], "declared_contracts");
    }
}

#[test]
fn combining_coverage_preserves_evidence_and_handles_integer_endpoints() {
    let report = analyze(request(vec![
        primitive("left", (1, 4), (2, 8)),
        primitive("right", (5, 15), (2, 8)),
    ]));
    assert_eq!(report["complete"], true);
    assert_eq!(report["residual"], json!([]));
    assert_eq!(report["covered"], json!([interval(1, 10)]));
    assert_eq!(report["matches"][0]["primitive"]["evidence"], json!(["fixture:left"]));

    for (right_start, expected) in [(0, json!([])), (1, json!([interval(0, 0)]))] {
        let mut input = request(vec![
            primitive("negative", (i64::MIN, -1), (0, 0)),
            primitive("positive", (right_start, i64::MAX), (0, 0)),
        ]);
        input["required"] = interface((i64::MIN, i64::MAX), (0, 0));
        let report = analyze(input);
        assert_eq!(report["residual"], expected);
        assert_eq!(report["complete"], right_start == 0);
    }
}

#[test]
fn unestablished_assumptions_do_not_count_as_coverage() {
    let mut p = primitive("p", (1, 10), (2, 8));
    p["assumptions"] = json!(["source-current"]);
    let mut input = request(vec![p]);
    input["established_assumptions"] = json!(["source-current=false"]);
    let report = analyze(input.clone());
    assert_eq!(report["matches"][0]["status"], "conditional");
    assert_eq!(report["matches"][0]["missing_assumptions"], json!(["source-current"]));
    assert_eq!(report["matches"][0]["covered"], Value::Null);
    assert_eq!(report["residual"], json!([interval(1, 10)]));
    assert_eq!(report["complete"], false);

    input["established_assumptions"] = json!(["source-current"]);
    let report = analyze(input);
    assert_eq!(report["complete"], true);
    assert_eq!(report["residual"], json!([]));
}

#[test]
fn valid_parts_do_not_discharge_missing_coupling_obligations() {
    let mut input = request(vec![
        primitive("left", (1, 4), (2, 8)),
        primitive("right", (5, 10), (2, 8)),
    ]);
    input["couplings"] = json!([{
        "id": "combined-budget",
        "from": "left",
        "to": "right",
        "obligations": ["shared-budget"]
    }]);
    let report = analyze(input.clone());
    assert_eq!(report["residual"], json!([]));
    assert_eq!(report["complete"], false);
    assert_eq!(
        report["coupling_findings"][0]["missing_obligations"],
        json!(["shared-budget"])
    );

    input["established_assumptions"] = json!(["shared-budget"]);
    assert_eq!(analyze(input)["complete"], true);
}

#[test]
fn malformed_compositions_are_errors_instead_of_partial_successes() {
    let base = request(vec![primitive("p", (1, 10), (2, 8))]);
    let mut reversed = base.clone();
    reversed["required"]["input"] = interval(10, 1);
    let mut duplicate = base.clone();
    duplicate["primitives"] = json!([
        primitive("p", (1, 5), (2, 8)),
        primitive("p", (6, 10), (2, 8))
    ]);
    let mut missing_endpoint = base.clone();
    missing_endpoint["couplings"] =
        json!([{"id": "missing", "from": "p", "to": "absent", "obligations": []}]);
    let mut wrong_version = base;
    wrong_version["schema_version"] = json!(2);

    for malformed in [reversed, duplicate, missing_endpoint, wrong_version] {
        let parsed: MatchRequest = serde_json::from_value(malformed).unwrap();
        assert!(composition::analyze(&parsed).is_err());
    }
}

#[test]
fn graph_queries_are_deterministic_bounded_and_return_witnesses() {
    let graph = snapshot(snapshot_value());
    graph.validate().unwrap();
    let neighbors = graph.neighbors("root", Some("uses")).unwrap();
    assert_eq!(
        neighbors.iter().map(|edge| edge.to.as_str()).collect::<Vec<_>>(),
        vec!["a", "b"]
    );
    let path = graph.path("root", "c", Some("uses"), 2).unwrap().unwrap();
    assert_eq!(
        path.iter().map(|edge| edge.id.as_str()).collect::<Vec<_>>(),
        vec!["z", "bc"]
    );
    assert_eq!(path[0].evidence, vec!["source:z"]);
    assert!(graph.path("root", "c", Some("uses"), 1).unwrap().is_none());
    assert!(graph.path("c", "root", Some("uses"), 100).unwrap().is_none());
    assert_eq!(graph.path("root", "root", None, 0).unwrap(), Some(vec![]));
    assert!(graph.path("absent", "root", None, 10).is_err());
    assert!(graph.neighbors("absent", None).is_err());

    let mut reversed = graph.clone();
    reversed.edges.reverse();
    assert_eq!(
        reversed.neighbors("root", Some("uses")).unwrap(),
        neighbors
    );
}

#[test]
fn malformed_graphs_do_not_enter_queries_as_valid_snapshots() {
    let base = snapshot_value();
    let mut bad_version = base.clone();
    bad_version["schema_version"] = json!(2);
    let mut missing_revision = base.clone();
    missing_revision["revision"] = json!("");
    let mut unknown_endpoint = base.clone();
    unknown_endpoint["edges"][0]["to"] = json!("missing");
    let mut duplicate_edge = base.clone();
    duplicate_edge["edges"][1]["id"] = json!("z");
    let mut empty_kind = base;
    empty_kind["nodes"]["a"]["kind"] = json!("");

    for malformed in [
        bad_version,
        missing_revision,
        unknown_endpoint,
        duplicate_edge,
        empty_kind,
    ] {
        assert!(snapshot(malformed).validate().is_err());
    }
}

#[test]
fn native_evaluation_distinguishes_missing_fact_and_violation() {
    let contract = rules(vec![range_rule()]);
    let cases = [
        (json!({"count": 1}), "satisfied"),
        (json!({"count": 10}), "satisfied"),
        (json!({"count": 0}), "violated"),
        (json!({"count": 11}), "violated"),
        (json!({"count": true}), "violated"),
        (json!({}), "unknown"),
    ];
    for (input, expected) in cases {
        assert_eq!(native(&contract, &input)["status"], expected, "{input}");
    }
    let equality = rules(vec![json!({
        "id": "boolean",
        "pointer": "/value",
        "kind": "equals",
        "expected": true
    })]);
    assert_eq!(native(&equality, &json!({"value": 1}))["status"], "violated");

    let exists = rules(vec![json!({"id": "present", "pointer": "/value", "kind": "exists"})]);
    assert_eq!(native(&exists, &json!({"value": null}))["status"], "satisfied");
}

#[test]
fn native_contains_preserves_relationship_fields_and_rejects_malformed_contracts() {
    let contract = rules(vec![json!({
        "id": "uses",
        "pointer": "/edges",
        "kind": "contains",
        "expected": {"from": "a", "to": "b", "relation": "uses"}
    })]);
    let observed = json!({"edges": [{
        "id": "e1", "from": "a", "to": "b", "relation": "uses", "evidence": ["src:1"]
    }]});
    assert_eq!(native(&contract, &observed)["status"], "satisfied");
    let reverse = json!({"edges": [{"from": "b", "to": "a", "relation": "uses"}]});
    assert_eq!(native(&contract, &reverse)["status"], "violated");

    let malformed = [
        json!({"schema_version": 1, "rules": "not-a-list"}),
        rules(vec![]),
        rules(vec![json!({"id": "bad", "pointer": "not/a/pointer", "kind": "exists"})]),
    ];
    for contract in malformed {
        assert_eq!(native(&contract, &json!({}))["status"], "error");
    }
}

#[test]
fn report_identity_is_bound_to_original_inputs_and_contracts() {
    let contract = rules(vec![range_rule()]);
    let input = json!({"count": 5});
    let report = native(&contract, &input);
    assert_eq!(report["input_digest"], digest(&input));
    assert_eq!(report["contract_digest"], digest(&contract));
    let changed = native(&contract, &json!({"count": 6}));
    assert_ne!(report["input_digest"], changed["input_digest"]);
    assert_eq!(report["contract_digest"], changed["contract_digest"]);

    let mut changed_contract = contract.clone();
    changed_contract["rules"][0]["max"] = json!(9);
    let changed = native(&changed_contract, &input);
    assert_eq!(report["input_digest"], changed["input_digest"]);
    assert_ne!(report["contract_digest"], changed["contract_digest"]);
}

#[test]
fn candidate_derivation_preserves_old_expectation_after_graph_change() {
    let graph = snapshot(snapshot_value());
    let candidate = graph::derive_candidate(&graph, "root").unwrap();
    assert_eq!(candidate["status"], "candidate");
    assert_eq!(candidate["source"]["snapshot"], "fixture");
    assert_eq!(candidate["source"]["revision"], "rev-1");
    let original = serde_json::to_value(&graph).unwrap();
    assert_eq!(native(&candidate["contract"], &original)["status"], "satisfied");

    let mut changed = original.clone();
    changed["nodes"]["root"]["kind"] = json!("different-kind");
    assert_eq!(native(&candidate["contract"], &changed)["status"], "violated");

    let mut missing_edge = graph.clone();
    missing_edge.edges.retain(|edge| edge.id != "a");
    assert_eq!(
        native(&candidate["contract"], &serde_json::to_value(missing_edge).unwrap())["status"],
        "violated"
    );
    assert!(graph::derive_candidate(&graph, "missing").is_err());

    let special = snapshot(json!({
        "schema_version": 1,
        "id": "escaped",
        "revision": "1",
        "nodes": {"svc/~": {"kind": "service", "attributes": {"a/b~": 8}, "evidence": []}},
        "edges": []
    }));
    let candidate = graph::derive_candidate(&special, "svc/~").unwrap();
    assert_eq!(
        native(&candidate["contract"], &serde_json::to_value(special).unwrap())["status"],
        "satisfied"
    );
}
