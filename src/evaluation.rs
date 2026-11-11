//! Evaluator port and two deliberately separate contract languages.
//!
//! Native rules inspect concrete JSON observations. CUE contracts retain their
//! own source and semantics. Neither adapter replaces observations with values
//! obtained by unifying them with a contract.

use serde::{Deserialize, Serialize};
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::collections::BTreeSet;
use std::fs;
use std::io::Read;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::thread;
use std::time::{Duration, Instant};
use tempfile::NamedTempFile;

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum Status {
    Satisfied,
    Violated,
    Unknown,
    Unsupported,
    Error,
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
pub struct Finding {
    pub id: String,
    pub pointer: Option<String>,
    pub status: Status,
    pub reason: String,
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
pub struct EvaluationReport {
    pub schema_version: u32,
    pub backend: String,
    pub backend_version: Option<String>,
    pub status: Status,
    pub findings: Vec<Finding>,
    pub input_digest: String,
    pub contract_digest: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub contract_context: Option<Value>,
}

/// A backend receives its native contract document and independent input facts.
pub trait Evaluator {
    fn evaluate(&self, contract: &Value, input: &Value) -> EvaluationReport;
}

/// Stable for this version's serde_json representation; not an RFC 8785 claim.
pub fn digest(value: &Value) -> String {
    format!(
        "{:x}",
        Sha256::digest(serde_json::to_vec(value).expect("JSON value"))
    )
}

impl EvaluationReport {
    pub fn new(backend: &str, contract: &Value, input: &Value) -> Self {
        Self {
            schema_version: 1,
            backend: backend.to_owned(),
            backend_version: None,
            status: Status::Unknown,
            findings: Vec::new(),
            input_digest: digest(input),
            contract_digest: digest(contract),
            contract_context: None,
        }
    }

    pub fn failure(mut self, status: Status, reason: impl Into<String>) -> Self {
        self.status = status.clone();
        self.findings.push(Finding {
            id: "evaluation".to_owned(),
            pointer: None,
            status,
            reason: reason.into(),
        });
        self
    }
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Candidate {
    status: String,
    subject: String,
    source: CandidateSource,
    contract: Value,
}

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct CandidateSource {
    snapshot: String,
    revision: String,
    digest: String,
    digest_basis: String,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct NativeContract {
    schema_version: u32,
    rules: Vec<Rule>,
}

#[derive(Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
enum Rule {
    Exists {
        id: String,
        pointer: String,
    },
    Equals {
        id: String,
        pointer: String,
        expected: Value,
    },
    IntegerRange {
        id: String,
        pointer: String,
        min: Option<i64>,
        max: Option<i64>,
    },
    Contains {
        id: String,
        pointer: String,
        expected: Value,
    },
}

impl Rule {
    fn identity(&self) -> (&str, &str) {
        match self {
            Self::Exists { id, pointer }
            | Self::Equals { id, pointer, .. }
            | Self::IntegerRange { id, pointer, .. }
            | Self::Contains { id, pointer, .. } => (id, pointer),
        }
    }
}

fn valid_pointer(pointer: &str) -> bool {
    if pointer.is_empty() {
        return true;
    }
    if !pointer.starts_with('/') {
        return false;
    }
    let mut chars = pointer.chars();
    while let Some(character) = chars.next() {
        if character == '~' && !matches!(chars.next(), Some('0' | '1')) {
            return false;
        }
    }
    true
}

fn contains_value(actual: &Value, expected: &Value) -> bool {
    if let Value::Object(properties) = expected {
        let Some(object) = actual.as_object() else {
            return false;
        };
        properties.iter().all(|(key, value)| {
            object
                .get(key)
                .is_some_and(|actual| contains_value(actual, value))
        })
    } else {
        actual == expected
    }
}

pub struct NativeEvaluator;

impl Evaluator for NativeEvaluator {
    fn evaluate(&self, contract: &Value, input: &Value) -> EvaluationReport {
        let mut report = EvaluationReport::new("native", contract, input);
        report.backend_version = Some(env!("CARGO_PKG_VERSION").to_owned());
        let candidate = if contract.get("status").and_then(Value::as_str) == Some("candidate") {
            match serde_json::from_value::<Candidate>(contract.clone()) {
                Ok(candidate) => {
                    if [
                        &candidate.subject,
                        &candidate.source.snapshot,
                        &candidate.source.revision,
                        &candidate.source.digest,
                        &candidate.source.digest_basis,
                    ]
                    .iter()
                    .any(|value| value.trim().is_empty())
                    {
                        return report
                            .failure(Status::Error, "Candidate identity fields must be nonempty");
                    }
                    report.contract_context = Some(serde_json::json!({
                        "status": candidate.status,
                        "subject": candidate.subject,
                        "source": candidate.source
                    }));
                    Some(candidate.contract)
                }
                Err(error) => {
                    return report.failure(Status::Error, format!("Invalid candidate: {error}"));
                }
            }
        } else {
            None
        };
        let document = candidate.as_ref().unwrap_or(contract);
        let specification: NativeContract = match serde_json::from_value(document.clone()) {
            Ok(value) => value,
            Err(error) => {
                return report.failure(Status::Error, format!("Invalid native contract: {error}"));
            }
        };
        if specification.schema_version != 1 || specification.rules.is_empty() {
            return report.failure(
                Status::Error,
                "Expected schema_version 1 and at least one rule",
            );
        }
        let mut ids = BTreeSet::new();
        for rule in &specification.rules {
            let (id, pointer) = rule.identity();
            if id.trim().is_empty() || !ids.insert(id) || !valid_pointer(pointer) {
                return report.failure(
                    Status::Error,
                    "Rule IDs must be nonempty and unique; pointers must be valid JSON Pointers",
                );
            }
            if let Rule::IntegerRange { min, max, .. } = rule
                && ((min.is_none() && max.is_none())
                    || min.zip(*max).is_some_and(|(low, high)| low > high))
            {
                return report
                    .failure(Status::Error, "Integer bounds must be nonempty and ordered");
            }
        }
        for rule in specification.rules {
            let (id, pointer) = rule.identity();
            let (status, reason) = if let Some(value) = input.pointer(pointer) {
                let holds = match &rule {
                    Rule::Exists { .. } => true,
                    Rule::Equals { expected, .. } => value == expected,
                    Rule::IntegerRange { min, max, .. } => value.as_i64().is_some_and(|number| {
                        min.is_none_or(|low| number >= low) && max.is_none_or(|high| number <= high)
                    }),
                    Rule::Contains { expected, .. } => value.as_array().is_some_and(|items| {
                        items.iter().any(|item| contains_value(item, expected))
                    }),
                };
                if holds {
                    (Status::Satisfied, "Observed value satisfies this rule")
                } else {
                    (Status::Violated, "Observed value contradicts this rule")
                }
            } else {
                (Status::Unknown, "No observation exists at this pointer")
            };
            report.findings.push(Finding {
                id: id.to_owned(),
                pointer: Some(pointer.to_owned()),
                status,
                reason: reason.to_owned(),
            });
        }
        report.status = if report
            .findings
            .iter()
            .any(|finding| finding.status == Status::Violated)
        {
            Status::Violated
        } else if report
            .findings
            .iter()
            .any(|finding| finding.status == Status::Unknown)
        {
            Status::Unknown
        } else {
            Status::Satisfied
        };
        report
    }
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct CueContract {
    source: String,
    #[serde(default = "default_definition")]
    definition: String,
}

fn default_definition() -> String {
    "#Contract".to_owned()
}

pub struct CueEvaluator {
    binary: PathBuf,
    timeout: Duration,
}

struct ProcessOutput {
    success: bool,
    code: Option<i32>,
    text: String,
}

impl CueEvaluator {
    pub fn new(binary: PathBuf, timeout: Duration) -> Self {
        let binary = if binary.is_relative() && binary.components().count() > 1 {
            std::env::current_dir()
                .map(|directory| directory.join(&binary))
                .unwrap_or(binary)
        } else {
            binary
        };
        Self { binary, timeout }
    }

    fn run(&self, directory: &Path, arguments: &[&str]) -> Result<ProcessOutput, (Status, String)> {
        let mut output =
            NamedTempFile::new_in(directory).map_err(|error| (Status::Error, error.to_string()))?;
        let output_handle = output
            .as_file()
            .try_clone()
            .map_err(|error| (Status::Error, error.to_string()))?;
        let error_handle = output
            .as_file()
            .try_clone()
            .map_err(|error| (Status::Error, error.to_string()))?;
        let mut child = Command::new(&self.binary)
            .current_dir(directory)
            .args(arguments)
            .stdin(Stdio::null())
            .stdout(Stdio::from(output_handle))
            .stderr(Stdio::from(error_handle))
            .spawn()
            .map_err(|error| {
                let status = if error.kind() == std::io::ErrorKind::NotFound {
                    Status::Unsupported
                } else {
                    Status::Error
                };
                (status, format!("Cannot run CUE: {error}"))
            })?;
        let start = Instant::now();
        let status = loop {
            let too_large = output
                .as_file()
                .metadata()
                .map(|metadata| metadata.len() > 1_048_576)
                .unwrap_or(true);
            if too_large || start.elapsed() >= self.timeout {
                let _ = child.kill();
                let _ = child.wait();
                return Err((
                    Status::Error,
                    if too_large {
                        "CUE output exceeded 1 MiB".to_owned()
                    } else {
                        "CUE command exceeded its deadline".to_owned()
                    },
                ));
            }
            match child.try_wait() {
                Ok(Some(status)) => break status,
                Ok(None) => thread::sleep(Duration::from_millis(10)),
                Err(error) => {
                    let _ = child.kill();
                    let _ = child.wait();
                    return Err((Status::Error, error.to_string()));
                }
            }
        };
        let final_size = output
            .as_file()
            .metadata()
            .map_err(|error| (Status::Error, error.to_string()))?
            .len();
        if final_size > 1_048_576 {
            return Err((Status::Error, "CUE output exceeded 1 MiB".to_owned()));
        }
        use std::io::{Seek, SeekFrom};
        output
            .as_file_mut()
            .seek(SeekFrom::Start(0))
            .map_err(|error| (Status::Error, error.to_string()))?;
        let mut bytes = Vec::new();
        output
            .take(65_537)
            .read_to_end(&mut bytes)
            .map_err(|error| (Status::Error, error.to_string()))?;
        let truncated = bytes.len() > 65_536;
        bytes.truncate(65_536);
        let mut text = String::from_utf8_lossy(&bytes).into_owned();
        if truncated {
            text.push_str("\n[diagnostic truncated]");
        }
        Ok(ProcessOutput {
            success: status.success(),
            code: status.code(),
            text,
        })
    }
}

impl Evaluator for CueEvaluator {
    fn evaluate(&self, contract: &Value, input: &Value) -> EvaluationReport {
        let mut report = EvaluationReport::new("cue", contract, input);
        let specification: CueContract = match serde_json::from_value(contract.clone()) {
            Ok(value) => value,
            Err(error) => {
                return report.failure(Status::Error, format!("Invalid CUE document: {error}"));
            }
        };
        if specification.source.trim().is_empty() || specification.definition.trim().is_empty() {
            return report.failure(Status::Error, "CUE source and definition must be nonempty");
        }
        let directory = match tempfile::tempdir() {
            Ok(directory) => directory,
            Err(error) => return report.failure(Status::Error, error.to_string()),
        };
        for (name, bytes) in [
            ("schema.cue", specification.source.into_bytes()),
            ("input.json", serde_json::to_vec(input).expect("JSON value")),
        ] {
            if let Err(error) = fs::write(directory.path().join(name), bytes) {
                return report.failure(Status::Error, error.to_string());
            }
        }
        let version = match self.run(directory.path(), &["version"]) {
            Ok(output) if output.success => output,
            Ok(output) => return report.failure(Status::Error, output.text),
            Err((status, message)) => return report.failure(status, message),
        };
        report.backend_version = version.text.lines().next().map(str::to_owned);
        let preflight = match self.run(
            directory.path(),
            &["eval", "-e", &specification.definition, "schema.cue"],
        ) {
            Ok(output) => output,
            Err((status, message)) => return report.failure(status, message),
        };
        if !preflight.success {
            return report.failure(
                Status::Error,
                format!("CUE contract failed preflight: {}", preflight.text),
            );
        }
        match self.run(
            directory.path(),
            &[
                "vet",
                "-c",
                "-d",
                &specification.definition,
                "schema.cue",
                "input.json",
            ],
        ) {
            Ok(output) if output.success => {
                report.status = Status::Satisfied;
                report
            }
            Ok(output) => {
                let status = if output.code == Some(1) {
                    Status::Violated
                } else {
                    Status::Error
                };
                report.failure(status, output.text)
            }
            Err((status, message)) => report.failure(status, message),
        }
    }
}
