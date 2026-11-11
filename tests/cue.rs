use graph_contracts_poc::evaluation::{CueEvaluator, Evaluator};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::path::PathBuf;
use std::time::Duration;

fn report(evaluator: &CueEvaluator, contract: &Value, input: &Value) -> Value {
    serde_json::to_value(evaluator.evaluate(contract, input)).expect("serializable report")
}

fn pinned_cue() -> CueEvaluator {
    let binary = std::env::var_os("CUE_BIN")
        .expect("set CUE_BIN to the pinned CUE executable when running ignored tests");
    CueEvaluator::new(PathBuf::from(binary), Duration::from_secs(10))
}

#[test]
fn absent_cue_is_unsupported_instead_of_a_success_or_contract_violation() {
    let directory = tempfile::tempdir().unwrap();
    let evaluator = CueEvaluator::new(
        directory.path().join("cue-does-not-exist"),
        Duration::from_secs(1),
    );
    let contract = json!({"source": "#Contract: { count!: int }", "definition": "#Contract"});
    assert_eq!(
        report(&evaluator, &contract, &json!({"count": 1}))["status"],
        "unsupported"
    );
}

#[test]
#[ignore = "requires pinned CUE_BIN; explicitly run in the CUE CI job"]
fn actual_cue_distinguishes_contract_failures_from_invalid_definitions() {
    let evaluator = pinned_cue();
    let contract = json!({
        "source": "#Contract: { count!: int & >=1 & <=10 }",
        "definition": "#Contract"
    });
    for (input, expected) in [
        (json!({"count": 1}), "satisfied"),
        (json!({"count": 10}), "satisfied"),
        (json!({"count": 0}), "violated"),
        (json!({}), "violated"),
    ] {
        assert_eq!(
            report(&evaluator, &contract, &input)["status"],
            expected,
            "{input}"
        );
    }
    let invalid = json!({"source": "#Contract: { count!: ", "definition": "#Contract"});
    assert_eq!(report(&evaluator, &invalid, &json!({}))["status"], "error");
    let missing_definition =
        json!({"source": "#Other: { count!: int }", "definition": "#Contract"});
    assert_eq!(
        report(&evaluator, &missing_definition, &json!({}))["status"],
        "error"
    );
}

#[test]
#[ignore = "requires pinned CUE_BIN; explicitly run in the CUE CI job"]
fn actual_cue_does_not_replace_observations_with_unified_defaults() {
    let evaluator = pinned_cue();
    let contract = json!({
        "source": "#Contract: { count: *5 | int }",
        "definition": "#Contract"
    });
    let input = json!({});
    let original = input.clone();
    let evaluated = report(&evaluator, &contract, &input);
    assert!(
        evaluated["status"] == "satisfied" || evaluated["status"] == "violated",
        "{evaluated}"
    );
    assert_eq!(input, original);
    let raw_digest = format!(
        "{:x}",
        Sha256::digest(serde_json::to_vec(&original).unwrap())
    );
    assert_eq!(evaluated["input_digest"], raw_digest);
    assert_ne!(
        evaluated["input_digest"],
        format!(
            "{:x}",
            Sha256::digest(serde_json::to_vec(&json!({"count": 5})).unwrap())
        )
    );
}
