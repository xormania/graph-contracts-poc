use graph_contracts_poc::evaluation::{Evaluator, NativeEvaluator};
use graph_contracts_poc::graph::{GraphSnapshot, derive_candidate};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};

fn snapshot(revision: &str) -> GraphSnapshot {
    serde_json::from_value(json!({
        "schema_version": 1,
        "id": "observations",
        "revision": revision,
        "nodes": {"service": {"kind": "service", "attributes": {"version": 1}, "evidence": ["source:1"]}},
        "edges": []
    })).unwrap()
}

fn digest(value: &Value) -> String {
    format!("{:x}", Sha256::digest(serde_json::to_vec(value).unwrap()))
}

fn evaluate(contract: &Value, input: &Value) -> Value {
    serde_json::to_value(NativeEvaluator.evaluate(contract, input)).unwrap()
}

#[test]
fn equivalent_candidate_rules_retain_distinct_source_identity() {
    let first = snapshot("revision-1");
    let second = snapshot("revision-2");
    let first_candidate = derive_candidate(&first, "service").unwrap();
    let second_candidate = derive_candidate(&second, "service").unwrap();
    assert_eq!(first_candidate["contract"], second_candidate["contract"]);
    assert_ne!(
        first_candidate["source"]["digest"],
        second_candidate["source"]["digest"]
    );
    assert_eq!(
        first_candidate["source"]["digest"],
        digest(&serde_json::to_value(&first).unwrap())
    );
    assert_eq!(
        first_candidate["source"]["digest_basis"],
        "normalized_graph_snapshot_v1"
    );

    let input = serde_json::to_value(&first).unwrap();
    let first_report = evaluate(&first_candidate, &input);
    let second_report = evaluate(&second_candidate, &input);
    assert_eq!(first_report["status"], "satisfied");
    assert_eq!(second_report["status"], "satisfied");
    assert_eq!(first_report["findings"], second_report["findings"]);
    assert_ne!(
        first_report["contract_digest"],
        second_report["contract_digest"]
    );
    assert_eq!(first_report["contract_digest"], digest(&first_candidate));
    assert_eq!(second_report["contract_digest"], digest(&second_candidate));
    assert_eq!(first_report["contract_context"]["status"], "candidate");
    assert_eq!(first_report["contract_context"]["subject"], "service");
    assert_eq!(
        first_report["contract_context"]["source"],
        first_candidate["source"]
    );
    assert_eq!(
        second_report["contract_context"]["source"],
        second_candidate["source"]
    );
}

#[test]
fn malformed_candidate_source_is_an_error_not_a_bare_contract_fallback() {
    let graph = snapshot("revision-1");
    let input = serde_json::to_value(&graph).unwrap();
    let candidate = derive_candidate(&graph, "service").unwrap();
    for field in ["snapshot", "revision", "digest"] {
        let mut missing = candidate.clone();
        missing["source"].as_object_mut().unwrap().remove(field);
        assert_eq!(evaluate(&missing, &input)["status"], "error", "{field}");
        let mut wrong_type = candidate.clone();
        wrong_type["source"][field] = json!(false);
        assert_eq!(evaluate(&wrong_type, &input)["status"], "error", "{field}");
    }
}
