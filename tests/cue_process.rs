//! Process-adapter regressions use a trusted Python fixture, not a CUE substitute.
//! Real CUE language conformance is exercised separately in tests/cue.rs.
#![cfg(unix)]

use graph_contracts_poc::evaluation::{CueEvaluator, Evaluator};
use serde_json::{Value, json};
use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};
use tempfile::TempDir;

fn fixture(mode: &str, under_working_directory: bool) -> (TempDir, PathBuf, PathBuf) {
    let directory = if under_working_directory {
        tempfile::Builder::new()
            .prefix("gcp-cue-process-")
            .tempdir_in(std::env::current_dir().unwrap())
            .unwrap()
    } else {
        tempfile::tempdir().unwrap()
    };
    let executable = directory.path().join("fake-cue.py");
    let script = r#"#!/usr/bin/env python3
import json
import os
import pathlib
import sys
import time

mode = __MODE__
operation = sys.argv[1] if len(sys.argv) > 1 else ""
state = pathlib.Path(__file__).with_suffix(".state.json")
state.write_text(json.dumps({"pid": os.getpid(), "cwd": os.getcwd(), "operation": operation}))
if operation == "version":
    print("cue version fixture")
    sys.exit(0)
if operation == "eval":
    sys.exit(0)
if operation != "vet":
    sys.exit(2)
if mode == "unicode":
    sys.stdout.buffer.write(("a" + "\u00e9" * 33000).encode("utf-8"))
    sys.exit(1)
if mode == "exit-two":
    print("fixture execution failure", file=sys.stderr)
    sys.exit(2)
if mode == "oversize":
    sys.stdout.buffer.write(b"x" * 1048577)
    sys.exit(0)
if mode == "timeout":
    time.sleep(0.5)
    sys.exit(0)
sys.exit(0)
"#;
    fs::write(&executable, script.replace("__MODE__", &serde_json::to_string(mode).unwrap())).unwrap();
    fs::set_permissions(&executable, fs::Permissions::from_mode(0o700)).unwrap();
    let state = executable.with_extension("state.json");
    (directory, executable, state)
}

fn evaluate(binary: PathBuf, timeout: Duration) -> Value {
    let evaluator = CueEvaluator::new(binary, timeout);
    let contract = json!({"source": "#Contract: { count!: int }", "definition": "#Contract"});
    serde_json::to_value(evaluator.evaluate(&contract, &json!({"count": 1}))).unwrap()
}

fn diagnostic(report: &Value) -> String {
    report["findings"]
        .as_array()
        .unwrap()
        .iter()
        .filter_map(|finding| finding["reason"].as_str())
        .collect::<Vec<_>>()
        .join("\n")
}

#[test]
fn explicit_relative_cue_path_survives_the_temporary_working_directory() {
    let (_directory, executable, _state) = fixture("success", true);
    let working_directory = std::env::current_dir().unwrap();
    let relative = executable.strip_prefix(&working_directory).unwrap().to_path_buf();
    assert!(relative.is_relative());
    let report = evaluate(relative, Duration::from_secs(3));
    assert_eq!(report["status"], "satisfied", "{report}");
    assert_eq!(report["backend_version"], "cue version fixture");
}

#[test]
fn cue_diagnostics_keep_utf8_and_distinguish_execution_exit_codes() {
    for (mode, expected) in [("unicode", "violated"), ("exit-two", "error")] {
        let (_directory, executable, _state) = fixture(mode, false);
        let report = evaluate(executable, Duration::from_secs(3));
        assert_eq!(report["status"], expected, "{report}");
        let text = diagnostic(&report);
        assert!(!text.trim().is_empty());
        if mode == "unicode" {
            assert!(text.contains('\u{e9}'), "Unicode diagnostic must remain readable");
        }
    }
}

#[test]
fn output_limit_also_applies_to_a_child_that_has_already_exited() {
    let (_directory, executable, _state) = fixture("oversize", false);
    let report = evaluate(executable, Duration::from_secs(3));
    assert_eq!(report["status"], "error", "{report}");
    assert!(diagnostic(&report).contains("output"), "{report}");
}

#[test]
fn timed_out_cue_is_reaped_and_its_evaluation_directory_is_removed() {
    let (_directory, executable, state) = fixture("timeout", false);
    let started = Instant::now();
    let report = evaluate(executable, Duration::from_millis(100));
    assert_eq!(report["status"], "error", "{report}");
    assert!(diagnostic(&report).contains("deadline"), "{report}");
    assert!(started.elapsed() < Duration::from_secs(3));

    let trace: Value = serde_json::from_slice(&fs::read(state).unwrap()).unwrap();
    assert_eq!(trace["operation"], "vet", "fixture must reach its slow operation");
    assert!(!Path::new(trace["cwd"].as_str().unwrap()).exists());
    #[cfg(target_os = "linux")]
    {
        let pid = trace["pid"].as_u64().unwrap();
        assert!(!Path::new(&format!("/proc/{pid}")).exists(), "timed-out child must be reaped");
    }
}
