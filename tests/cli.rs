use serde_json::{Value, json};
use std::fs;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::process::{Command, Output, Stdio};

fn binary() -> Command {
    Command::new(env!("CARGO_BIN_EXE_gcp"))
}

fn invoke(command: &mut Command, input: Option<&Value>) -> (Output, Value) {
    command
        .stdin(if input.is_some() { Stdio::piped() } else { Stdio::null() })
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    let mut child = command.spawn().expect("start gcp");
    if let Some(value) = input {
        let mut stdin = child.stdin.take().expect("piped input");
        serde_json::to_writer(&mut stdin, value).unwrap();
        stdin.write_all(b"\n").unwrap();
    }
    let output = child.wait_with_output().expect("finish gcp");
    let value = serde_json::from_slice(&output.stdout).unwrap_or_else(|error| {
        panic!(
            "stdout must contain exactly one JSON value: {error}; stdout={}; stderr={}",
            String::from_utf8_lossy(&output.stdout),
            String::from_utf8_lossy(&output.stderr)
        )
    });
    (output, value)
}

fn native_contract() -> Value {
    json!({
        "schema_version": 1,
        "rules": [{"id": "count", "kind": "integer_range", "pointer": "/count", "min": 1, "max": 10}]
    })
}

fn write_json(directory: &Path, name: &str, value: &Value) -> PathBuf {
    let path = directory.join(name);
    fs::write(&path, serde_json::to_vec(value).unwrap()).unwrap();
    path
}

fn check(contract: &Path, input: &Path) -> Command {
    let mut command = binary();
    command.arg("check").arg("--contract").arg(contract).arg("--input").arg(input);
    command
}

#[test]
fn cli_and_provider_discovery_expose_the_same_capabilities() {
    let (direct_output, direct) = invoke(binary().arg("discover"), None);
    let (provider_output, provider) = invoke(
        binary().arg("provider"),
        Some(&json!({"operation": "discover"})),
    );
    assert!(direct_output.status.success());
    assert!(provider_output.status.success());
    assert_eq!(direct, provider);
    let capabilities = direct["capabilities"].as_array().unwrap();
    for name in ["graph.inspect", "contract.derive", "contract.evaluate", "contract.compare", "reuse.analyze"] {
        let capability = capabilities.iter().find(|item| item["name"] == name).unwrap();
        assert_eq!(capability["input_schema"]["type"], "object");
        assert_eq!(capability["input_schema"]["additionalProperties"], false);
    }
}

#[test]
fn provider_returns_domain_violations_normally_and_does_not_trust_context() {
    let request = json!({
        "name": "contract.evaluate",
        "arguments": {"backend": "native", "contract": native_contract(), "input": {"count": 20}},
        "context": {"force_success": true, "authorized": true}
    });
    let (output, result) = invoke(binary().arg("provider"), Some(&request));
    assert_eq!(output.status.code(), Some(0));
    assert_eq!(result["status"], "violated");
    assert!(result.get("error").is_none());
}

#[test]
fn invalid_provider_requests_have_a_transport_error_and_nonzero_exit() {
    let requests = [
        json!({"name": "unknown", "arguments": {}, "context": {}}),
        json!({
            "name": "contract.evaluate",
            "arguments": {"backend": "native", "contract": native_contract(), "input": {}},
            "context": []
        }),
        json!({
            "name": "contract.evaluate",
            "arguments": {
                "backend": "native",
                "contract": native_contract(),
                "input": {},
                "unexpected": true
            },
            "context": {}
        }),
    ];
    for request in requests {
        let (output, result) = invoke(binary().arg("provider"), Some(&request));
        assert_eq!(output.status.code(), Some(2), "{request}");
        assert_eq!(result["error"]["code"], "invalid_request");
        assert!(result.get("status").is_none());
    }
}

#[test]
fn checking_only_uses_failure_exit_status_when_explicitly_requested() {
    let directory = tempfile::tempdir().unwrap();
    let contract = write_json(directory.path(), "contract.json", &native_contract());
    let input = write_json(directory.path(), "input.json", &json!({"count": 20}));
    let (output, report) = invoke(&mut check(&contract, &input), None);
    assert_eq!(output.status.code(), Some(0));
    assert_eq!(report["status"], "violated");

    let (output, report) = invoke(check(&contract, &input).arg("--require-satisfied"), None);
    assert_eq!(output.status.code(), Some(3));
    assert_eq!(report["status"], "violated");

    fs::write(&input, r#"{"count": 5}"#).unwrap();
    let (output, report) = invoke(check(&contract, &input).arg("--require-satisfied"), None);
    assert_eq!(output.status.code(), Some(0));
    assert_eq!(report["status"], "satisfied");

    fs::write(&input, "{").unwrap();
    let (output, report) = invoke(&mut check(&contract, &input), None);
    assert_eq!(output.status.code(), Some(2));
    assert_eq!(report["error"]["code"], "invalid_request");
}

#[test]
fn missing_cue_makes_comparison_not_comparable() {
    let directory = tempfile::tempdir().unwrap();
    let contract = write_json(directory.path(), "native.json", &native_contract());
    let input = write_json(directory.path(), "input.json", &json!({"count": 5}));
    let cue_contract = directory.path().join("contract.cue");
    fs::write(&cue_contract, "#Contract: { count!: int & >=1 & <=10 }").unwrap();
    let missing_cue = directory.path().join("absent-cue");
    let (output, report) = invoke(
        binary()
            .arg("--cue").arg(missing_cue)
            .arg("compare")
            .arg("--native").arg(contract)
            .arg("--cue-contract").arg(cue_contract)
            .arg("--input").arg(input),
        None,
    );
    assert_eq!(output.status.code(), Some(0));
    assert_eq!(report["agreement"], "not_comparable");
    assert_eq!(report["native"]["status"], "satisfied");
    assert_eq!(report["cue"]["status"], "unsupported");
    assert_eq!(report["native"]["input_digest"], report["cue"]["input_digest"]);
}

#[test]
fn cli_and_provider_preserve_candidate_context_and_full_contract_identity() {
    let directory = tempfile::tempdir().unwrap();
    let candidate = json!({
        "status": "candidate",
        "subject": "service",
        "source": {
            "snapshot": "observations",
            "revision": "revision-1",
            "digest": "0".repeat(64),
            "digest_basis": "normalized_graph_snapshot_v1"
        },
        "contract": native_contract()
    });
    let input = json!({"count": 5});
    let contract_path = write_json(directory.path(), "candidate.json", &candidate);
    let input_path = write_json(directory.path(), "input.json", &input);
    let (cli_output, cli_report) = invoke(&mut check(&contract_path, &input_path), None);
    let request = json!({
        "name": "contract.evaluate",
        "arguments": {"backend": "native", "contract": candidate, "input": input},
        "context": {}
    });
    let (provider_output, provider_report) = invoke(binary().arg("provider"), Some(&request));
    assert_eq!(cli_output.status.code(), Some(0));
    assert_eq!(provider_output.status.code(), Some(0));
    for report in [&cli_report, &provider_report] {
        assert_eq!(report["status"], "satisfied");
        assert_eq!(report["contract_context"]["status"], "candidate");
        assert_eq!(report["contract_context"]["subject"], "service");
        assert_eq!(
            report["contract_context"]["source"],
            request["arguments"]["contract"]["source"]
        );
    }
    for field in ["status", "contract_digest", "input_digest", "contract_context"] {
        assert_eq!(cli_report[field], provider_report[field], "{field}");
    }
}
