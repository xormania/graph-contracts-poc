//! Experimental matching of declared contracts over inclusive integer intervals.
//!
//! Coverage combines alternative primitive declarations over the required input
//! domain. It does not establish runtime dispatch, sequential composition,
//! implementation correctness, or general subtraction of behavioral contracts.
//! Assumptions and coupling obligations are satisfied only by explicit, exact
//! entries in established_assumptions. Evidence strings are preserved references;
//! this module does not verify their contents.

use std::collections::BTreeSet;

use serde::{Deserialize, Serialize};

pub const SCHEMA_VERSION: u32 = 1;

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Interval {
    pub min: i64,
    pub max: i64,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct InterfaceContract {
    pub input: Interval,
    pub output: Interval,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Primitive {
    pub id: String,
    pub contract: InterfaceContract,
    #[serde(default)]
    pub assumptions: Vec<String>,
    #[serde(default)]
    pub evidence: Vec<String>,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Coupling {
    pub id: String,
    pub from: String,
    pub to: String,
    #[serde(default)]
    pub obligations: Vec<String>,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct MatchRequest {
    pub schema_version: u32,
    pub required: InterfaceContract,
    pub primitives: Vec<Primitive>,
    #[serde(default)]
    pub established_assumptions: Vec<String>,
    #[serde(default)]
    pub couplings: Vec<Coupling>,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case", deny_unknown_fields)]
pub enum MatchStatus {
    Complete,
    Partial,
    Incompatible,
    Conditional,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PrimitiveMatch {
    /// The unchanged declaration, including its evidence references.
    pub primitive: Primitive,
    pub status: MatchStatus,
    /// Established eligible coverage only; conditional matches contribute none.
    pub covered: Option<Interval>,
    pub missing_assumptions: Vec<String>,
    pub reasons: Vec<String>,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CouplingFinding {
    pub id: String,
    pub missing_obligations: Vec<String>,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CompositionReport {
    pub schema_version: u32,
    /// Always "declared_contracts"; it does not denote implementation proof.
    pub basis: String,
    pub required: InterfaceContract,
    pub matches: Vec<PrimitiveMatch>,
    pub covered: Vec<Interval>,
    pub residual: Vec<Interval>,
    pub coupling_findings: Vec<CouplingFinding>,
    /// Complete only within this declared interval-and-obligation experiment.
    pub complete: bool,
}

/// Compare primitive declarations with a required contract.
///
/// A primitive may accept a wider input domain, but must guarantee an output
/// domain contained in the required output domain. Its input intersection counts
/// toward coverage only when every declared assumption is explicitly established.
/// Every listed coupling is checked independently of primitive compatibility.
///
/// Returns an error for unsupported schema versions, invalid bounds, duplicate or
/// empty identities, duplicate or empty assumption/obligation entries, or coupling
/// endpoints that do not identify a primitive.
pub fn analyze(request: &MatchRequest) -> Result<CompositionReport, String> {
    if request.schema_version != SCHEMA_VERSION {
        return Err(format!(
            "unsupported schema_version {}; expected {}",
            request.schema_version, SCHEMA_VERSION
        ));
    }

    validate_interval(&request.required.input, "required input")?;
    validate_interval(&request.required.output, "required output")?;

    let established = unique_entries(
        &request.established_assumptions,
        "established_assumptions",
    )?;
    let mut ids = BTreeSet::new();

    for primitive in &request.primitives {
        if primitive.id.trim().is_empty() {
            return Err("primitive id must not be empty".to_owned());
        }
        if !ids.insert(primitive.id.as_str()) {
            return Err(format!("duplicate id '{}'", primitive.id));
        }
        validate_interval(
            &primitive.contract.input,
            &format!("primitive '{}' input", primitive.id),
        )?;
        validate_interval(
            &primitive.contract.output,
            &format!("primitive '{}' output", primitive.id),
        )?;
        unique_entries(
            &primitive.assumptions,
            &format!("primitive '{}' assumptions", primitive.id),
        )?;
    }

    let primitive_ids = ids.clone();
    for coupling in &request.couplings {
        if coupling.id.trim().is_empty() {
            return Err("coupling id must not be empty".to_owned());
        }
        if !ids.insert(coupling.id.as_str()) {
            return Err(format!("duplicate id '{}'", coupling.id));
        }
        if !primitive_ids.contains(coupling.from.as_str()) {
            return Err(format!(
                "coupling '{}' has unknown from primitive '{}'",
                coupling.id, coupling.from
            ));
        }
        if !primitive_ids.contains(coupling.to.as_str()) {
            return Err(format!(
                "coupling '{}' has unknown to primitive '{}'",
                coupling.id, coupling.to
            ));
        }
        unique_entries(
            &coupling.obligations,
            &format!("coupling '{}' obligations", coupling.id),
        )?;
    }

    let matches: Vec<PrimitiveMatch> = request
        .primitives
        .iter()
        .map(|primitive| {
            let overlap = intersection(&request.required.input, &primitive.contract.input);
            let output_fits = primitive.contract.output.min >= request.required.output.min
                && primitive.contract.output.max <= request.required.output.max;
            let missing_assumptions: Vec<String> = primitive
                .assumptions
                .iter()
                .filter(|assumption| !established.contains(assumption.as_str()))
                .cloned()
                .collect();
            let mut reasons = Vec::new();

            if overlap.is_none() {
                reasons.push(
                    "Declared input does not overlap the required input interval.".to_owned(),
                );
            }
            if !output_fits {
                reasons.push(
                    "Declared output is not contained in the required output interval."
                        .to_owned(),
                );
            }
            if !missing_assumptions.is_empty() {
                reasons.push(
                    "Some declared assumptions are not explicitly established; no coverage is credited."
                        .to_owned(),
                );
            }

            let status = if overlap.is_none() || !output_fits {
                MatchStatus::Incompatible
            } else if !missing_assumptions.is_empty() {
                MatchStatus::Conditional
            } else if overlap.as_ref() == Some(&request.required.input) {
                reasons.push(
                    "Declared input covers the required interval and its output guarantee fits."
                        .to_owned(),
                );
                MatchStatus::Complete
            } else {
                reasons.push(
                    "Only the input intersection is covered; the declared output guarantee fits."
                        .to_owned(),
                );
                MatchStatus::Partial
            };

            let covered = if output_fits && missing_assumptions.is_empty() {
                overlap
            } else {
                None
            };

            PrimitiveMatch {
                primitive: primitive.clone(),
                status,
                covered,
                missing_assumptions,
                reasons,
            }
        })
        .collect();

    let covered = normalize(
        matches
            .iter()
            .filter_map(|finding| finding.covered.clone())
            .collect(),
    );
    let residual = remaining(&request.required.input, &covered);
    let coupling_findings: Vec<CouplingFinding> = request
        .couplings
        .iter()
        .map(|coupling| CouplingFinding {
            id: coupling.id.clone(),
            missing_obligations: coupling
                .obligations
                .iter()
                .filter(|obligation| !established.contains(obligation.as_str()))
                .cloned()
                .collect(),
        })
        .collect();
    let complete = residual.is_empty()
        && coupling_findings
            .iter()
            .all(|finding| finding.missing_obligations.is_empty());

    Ok(CompositionReport {
        schema_version: SCHEMA_VERSION,
        basis: "declared_contracts".to_owned(),
        required: request.required.clone(),
        matches,
        covered,
        residual,
        coupling_findings,
        complete,
    })
}

fn validate_interval(interval: &Interval, label: &str) -> Result<(), String> {
    if interval.min > interval.max {
        return Err(format!(
            "{label} has invalid bounds: {} > {}",
            interval.min, interval.max
        ));
    }
    Ok(())
}

fn unique_entries<'a>(
    values: &'a [String],
    label: &str,
) -> Result<BTreeSet<&'a str>, String> {
    let mut seen = BTreeSet::new();
    for value in values {
        if value.trim().is_empty() {
            return Err(format!("{label} must not contain empty entries"));
        }
        if !seen.insert(value.as_str()) {
            return Err(format!("{label} contains duplicate entry '{value}'"));
        }
    }
    Ok(seen)
}

fn intersection(left: &Interval, right: &Interval) -> Option<Interval> {
    let min = left.min.max(right.min);
    let max = left.max.min(right.max);
    (min <= max).then_some(Interval { min, max })
}

fn normalize(mut intervals: Vec<Interval>) -> Vec<Interval> {
    intervals.sort_by_key(|interval| (interval.min, interval.max));
    let mut normalized: Vec<Interval> = Vec::new();
    for interval in intervals {
        if let Some(previous) = normalized.last_mut()
            && i128::from(interval.min) <= i128::from(previous.max) + 1
        {
            previous.max = previous.max.max(interval.max);
            continue;
        }
        normalized.push(interval);
    }
    normalized
}

fn remaining(required: &Interval, covered: &[Interval]) -> Vec<Interval> {
    // i128 leaves room for the inclusive successor of i64::MAX. Every emitted
    // endpoint remains within the validated required interval.
    let mut cursor = i128::from(required.min);
    let end = i128::from(required.max);
    let mut residual = Vec::new();

    for interval in covered {
        let start = i128::from(interval.min);
        if start > cursor {
            residual.push(Interval {
                min: cursor as i64,
                max: (start - 1) as i64,
            });
        }
        cursor = cursor.max(i128::from(interval.max) + 1);
        if cursor > end {
            break;
        }
    }
    if cursor <= end {
        residual.push(Interval {
            min: cursor as i64,
            max: required.max,
        });
    }
    residual
}
