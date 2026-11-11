//! Xorchestrate's one-request JSON CLI provider adapter.
//!
//! Arguments are inline domain documents. Transport context is not interpreted
//! as authority; the calling host owns authorization and orchestration.

use crate::composition::{MatchRequest, analyze};
use crate::evaluation::{CueEvaluator, EvaluationReport, Evaluator, NativeEvaluator, Status, digest};
use crate::graph::{GraphQuery, GraphSnapshot, derive_candidate};
use serde_json::{Value, json};
use std::path::Path;
use std::time::Duration;

fn required<'a>(arguments: &'a Value, name: &str) -> Result<&'a Value, String> {
    arguments.get(name).ok_or_else(|| format!("Missing argument: {name}"))
}

fn text<'a>(arguments: &'a Value, name: &str) -> Result<&'a str, String> {
    required(arguments, name)?.as_str().ok_or_else(|| format!("{name} must be a string"))
}

fn snapshot(arguments: &Value) -> Result<GraphSnapshot, String> {
    let graph: GraphSnapshot = serde_json::from_value(required(arguments, "graph")?.clone())
        .map_err(|error| error.to_string())?;
    graph.validate()?;
    Ok(graph)
}

fn relation(arguments: &Value) -> Result<Option<&str>, String> {
    arguments.get("relation").map(|value| {
        value.as_str().ok_or_else(|| "relation must be a string".to_owned())
    }).transpose()
}

fn check_keys(arguments: &Value, allowed: &[&str]) -> Result<(), String> {
    let object = arguments.as_object().ok_or("arguments must be an object")?;
    if let Some(key) = object.keys().find(|key| !allowed.contains(&key.as_str())) {
        return Err(format!("Unknown argument: {key}"));
    }
    Ok(())
}

pub fn evaluate(backend: &str, contract: &Value, input: &Value, cue: &Path, timeout: Duration) -> EvaluationReport {
    match backend {
        "native" => NativeEvaluator.evaluate(contract, input),
        "cue" => CueEvaluator::new(cue.to_path_buf(), timeout).evaluate(contract, input),
        _ => EvaluationReport::new(backend, contract, input)
            .failure(Status::Unsupported, format!("Unknown evaluator: {backend}")),
    }
}

/// Domain operations are shared by the CLI and the provider protocol.
pub fn execute(name: &str, arguments: &Value, cue: &Path, timeout: Duration) -> Result<Value, String> {
    match name {
        "graph.inspect" => {
            check_keys(arguments, &["graph"])?;
            let graph = snapshot(arguments)?;
            Ok(json!({
                "schema_version": 1, "snapshot": graph.id, "revision": graph.revision,
                "digest": digest(required(arguments, "graph")?),
                "nodes": graph.nodes.len(), "edges": graph.edges.len(),
                "basis": "supplied_observations"
            }))
        }
        "graph.neighbors" => {
            check_keys(arguments, &["graph", "subject", "relation"])?;
            let graph = snapshot(arguments)?;
            let edges = graph.neighbors(text(arguments, "subject")?, relation(arguments)?)?;
            Ok(json!({"schema_version":1,"snapshot":graph.id,"revision":graph.revision,
                "input_digest":digest(required(arguments,"graph")?),"edges":edges}))
        }
        "graph.path" => {
            check_keys(arguments, &["graph", "from", "to", "relation", "max_depth"])?;
            let graph = snapshot(arguments)?;
            let depth = match arguments.get("max_depth") {
                None => 8,
                Some(value) => usize::try_from(value.as_u64().ok_or("max_depth must be a nonnegative integer")?)
                    .map_err(|error| error.to_string())?,
            };
            let path = graph.path(text(arguments, "from")?, text(arguments, "to")?, relation(arguments)?, depth)?;
            Ok(json!({"schema_version":1,"snapshot":graph.id,"revision":graph.revision,
                "input_digest":digest(required(arguments,"graph")?),"path":path,
                "max_depth":depth,"basis":"supplied_observations"}))
        }
        "contract.derive" => {
            check_keys(arguments, &["graph", "subject"])?;
            let graph = snapshot(arguments)?;
            let mut candidate = derive_candidate(&graph, text(arguments, "subject")?)?;
            candidate["source"]["digest"] = json!(digest(required(arguments, "graph")?));
            Ok(candidate)
        }
        "contract.evaluate" => {
            check_keys(arguments, &["backend", "contract", "input"])?;
            serde_json::to_value(evaluate(text(arguments, "backend")?, required(arguments, "contract")?,
                required(arguments, "input")?, cue, timeout)).map_err(|error| error.to_string())
        }
        "contract.compare" => {
            check_keys(arguments, &["native", "cue", "input"])?;
            let input = required(arguments, "input")?;
            let native = evaluate("native", required(arguments, "native")?, input, cue, timeout);
            let cue = evaluate("cue", required(arguments, "cue")?, input, cue, timeout);
            let determinate = |status: &Status| matches!(status, Status::Satisfied | Status::Violated);
            let agreement = if !determinate(&native.status) || !determinate(&cue.status) {
                "not_comparable"
            } else if native.status == cue.status {
                "agree"
            } else {
                "disagree"
            };
            Ok(json!({"schema_version":1,"agreement":agreement,"native":native,"cue":cue,
                "note":"Agreement applies to these inputs and authored contracts; it does not prove equivalent languages."}))
        }
        "reuse.analyze" => {
            check_keys(arguments, &["request"])?;
            let request: MatchRequest = serde_json::from_value(required(arguments, "request")?.clone())
                .map_err(|error| error.to_string())?;
            let report = analyze(&request)?;
            let mut result = serde_json::to_value(report).map_err(|error| error.to_string())?;
            result["input_digest"] = json!(digest(required(arguments, "request")?));
            Ok(result)
        }
        _ => Err(format!("Unknown capability: {name}")),
    }
}

pub fn discover() -> Value {
    let definitions = [
        ("graph.inspect", "Validate and summarize supplied graph observations.", vec!["graph"], vec![]),
        ("graph.neighbors", "Inspect recorded outbound relationships.", vec!["graph", "subject"], vec!["relation"]),
        ("graph.path", "Find a bounded shortest recorded dependency path.", vec!["graph", "from", "to"], vec!["relation", "max_depth"]),
        ("contract.derive", "Derive a candidate expectation from positive graph observations.", vec!["graph", "subject"], vec![]),
        ("contract.evaluate", "Evaluate independent input against a native or CUE contract.", vec!["backend", "contract", "input"], vec![]),
        ("contract.compare", "Compare native and CUE evaluations on the same input.", vec!["native", "cue", "input"], vec![]),
        ("reuse.analyze", "Inspect declared interval coverage, assumptions and coupling gaps.", vec!["request"], vec![]),
    ];
    let capabilities: Vec<Value> = definitions.into_iter().map(|(name, description, required, optional)| {
        let mut properties = serde_json::Map::new();
        for key in required.iter().chain(optional.iter()) {
            let schema = match *key {
                "subject" | "from" | "to" | "relation" | "backend" => json!({"type":"string"}),
                "max_depth" => json!({"type":"integer","minimum":0}),
                "input" => json!({}),
                _ => json!({"type":"object"}),
            };
            properties.insert((*key).to_owned(), schema);
        }
        json!({
            "name":name,"description":description,
            "input_schema":{"type":"object","properties":properties,"required":required,"additionalProperties":false},
            "annotations":{"readOnlyHint":true,"destructiveHint":false},
            "output_schema":{"type":"object"}
        })
    }).collect();
    json!({"capabilities":capabilities})
}

pub fn dispatch(request: &Value, cue: &Path, timeout: Duration) -> Result<Value, String> {
    let object = request.as_object().ok_or("Provider request must be an object")?;
    if object.len() == 1 && request.get("operation").and_then(Value::as_str) == Some("discover") {
        return Ok(discover());
    }
    check_keys(request, &["name", "arguments", "context"])?;
    if request.get("context").is_some_and(|context| !context.is_object()) {
        return Err("context must be an object".to_owned());
    }
    execute(text(request, "name")?, required(request, "arguments")?, cue, timeout)
}
