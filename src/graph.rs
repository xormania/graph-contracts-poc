//! Recorded graph facts, deterministic queries, and observational contract candidates.
//!
//! Node kinds and edge relations are extractor-supplied labels. An edge records a
//! relationship; it does not imply a runtime call, authorization, or behavioral
//! guarantee. Evidence strings are opaque references, not proofs. A snapshot can
//! be incomplete, and its revision is supplied provenance rather than a live
//! freshness check.

use std::collections::{BTreeMap, BTreeSet, VecDeque};
use std::fs;
use std::path::PathBuf;

use serde::{Deserialize, Serialize};
use serde_json::Value;
use sha2::{Digest, Sha256};

/// A versioned set of recorded nodes and directed relationships.
#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct GraphSnapshot {
    pub schema_version: u32,
    pub id: String,
    pub revision: String,
    pub nodes: BTreeMap<String, Node>,
    pub edges: Vec<Edge>,
}

/// An extractor-supplied description of one graph subject.
#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Node {
    pub kind: String,
    #[serde(default)]
    pub attributes: Value,
    #[serde(default)]
    pub evidence: Vec<String>,
}

/// An outbound relationship with a snapshot-local identity.
///
/// Parallel edges are permitted when their identities differ. Relation labels
/// and evidence references carry no additional semantics in this module.
#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Edge {
    pub id: String,
    pub from: String,
    pub to: String,
    pub relation: String,
    #[serde(default)]
    pub evidence: Vec<String>,
}

impl GraphSnapshot {
    /// Hash the normalized typed snapshot, including defaulted fields.
    pub fn digest(&self) -> String {
        let value = serde_json::to_value(self).expect("graph snapshot");
        format!("{:x}", Sha256::digest(serde_json::to_vec(&value).expect("graph JSON")))
    }

    /// Check the supported schema and the snapshot's structural consistency.
    ///
    /// This does not establish completeness, freshness, or behavioral truth.
    pub fn validate(&self) -> Result<(), String> {
        if self.schema_version != 1 {
            return Err(format!(
                "unsupported graph schema_version: expected 1, got {}",
                self.schema_version
            ));
        }
        if self.id.is_empty() {
            return Err("graph id must not be empty".to_owned());
        }
        if self.revision.is_empty() {
            return Err("graph revision must not be empty".to_owned());
        }
        for (id, node) in &self.nodes {
            if id.is_empty() {
                return Err("node id must not be empty".to_owned());
            }
            if node.kind.is_empty() {
                return Err(format!("node {id:?} kind must not be empty"));
            }
        }
        let mut edge_ids = BTreeSet::new();
        for edge in &self.edges {
            if edge.id.is_empty() {
                return Err("edge id must not be empty".to_owned());
            }
            if !edge_ids.insert(edge.id.as_str()) {
                return Err(format!("duplicate edge id {:?}", edge.id));
            }
            if edge.relation.is_empty() {
                return Err(format!("edge {:?} relation must not be empty", edge.id));
            }
            if !self.nodes.contains_key(&edge.from) {
                return Err(format!(
                    "edge {:?} has unknown source node {:?}",
                    edge.id, edge.from
                ));
            }
            if !self.nodes.contains_key(&edge.to) {
                return Err(format!(
                    "edge {:?} has unknown target node {:?}",
                    edge.id, edge.to
                ));
            }
        }
        Ok(())
    }

    fn require_node(&self, id: &str) -> Result<(), String> {
        if self.nodes.contains_key(id) {
            Ok(())
        } else {
            Err(format!("unknown graph node {id:?}"))
        }
    }

    fn outgoing(&self, node: &str, relation: Option<&str>) -> Vec<&Edge> {
        let mut edges: Vec<_> = self
            .edges
            .iter()
            .filter(|edge| edge.from == node && relation.is_none_or(|name| edge.relation == name))
            .collect();
        sort_edges(&mut edges);
        edges
    }
}

/// Queries over recorded, outbound relationships.
///
/// A missing witness describes this snapshot and the supplied depth bound. It
/// does not establish that a relationship is absent from the real system.
pub trait GraphQuery {
    /// Return matching outgoing edges, ordered by relation, target, and edge id.
    fn neighbors(&self, node: &str, relation: Option<&str>) -> Result<Vec<Edge>, String>;

    /// Return a shortest outbound witness containing at most max_depth edges.
    ///
    /// Relation filtering applies to every edge. Equal endpoints have an empty
    /// witness, including at depth zero. Ties use the neighbors ordering.
    fn path(
        &self,
        from: &str,
        to: &str,
        relation: Option<&str>,
        max_depth: usize,
    ) -> Result<Option<Vec<Edge>>, String>;
}

impl GraphQuery for GraphSnapshot {
    fn neighbors(&self, node: &str, relation: Option<&str>) -> Result<Vec<Edge>, String> {
        self.validate()?;
        self.require_node(node)?;
        Ok(self.outgoing(node, relation).into_iter().cloned().collect())
    }

    fn path(
        &self,
        from: &str,
        to: &str,
        relation: Option<&str>,
        max_depth: usize,
    ) -> Result<Option<Vec<Edge>>, String> {
        self.validate()?;
        self.require_node(from)?;
        self.require_node(to)?;
        if from == to {
            return Ok(Some(Vec::new()));
        }
        if max_depth == 0 {
            return Ok(None);
        }

        let mut adjacency: BTreeMap<&str, Vec<&Edge>> = BTreeMap::new();
        for edge in &self.edges {
            if relation.is_none_or(|name| edge.relation == name) {
                adjacency.entry(edge.from.as_str()).or_default().push(edge);
            }
        }
        for edges in adjacency.values_mut() {
            sort_edges(edges);
        }

        let mut queue = VecDeque::from([(from, 0_usize)]);
        let mut visited = BTreeSet::from([from]);
        let mut predecessors: BTreeMap<&str, &Edge> = BTreeMap::new();

        while let Some((node, depth)) = queue.pop_front() {
            if depth >= max_depth {
                continue;
            }
            let Some(edges) = adjacency.get(node) else {
                continue;
            };
            for edge in edges {
                let next = edge.to.as_str();
                if !visited.insert(next) {
                    continue;
                }
                predecessors.insert(next, *edge);
                if next == to {
                    let mut witness = Vec::new();
                    let mut cursor = to;
                    while cursor != from {
                        let Some(incoming) = predecessors.get(cursor) else {
                            return Err("path witness could not be reconstructed".to_owned());
                        };
                        witness.push((**incoming).clone());
                        cursor = incoming.from.as_str();
                    }
                    witness.reverse();
                    return Ok(Some(witness));
                }
                queue.push_back((next, depth + 1));
            }
        }

        Ok(None)
    }
}

fn sort_edges(edges: &mut [&Edge]) {
    edges.sort_by(|left, right| {
        (&left.relation, &left.to, &left.id).cmp(&(&right.relation, &right.to, &right.id))
    });
}

/// Load one structurally validated graph snapshot.
pub trait SnapshotStore {
    fn load(&self) -> Result<GraphSnapshot, String>;
}

/// A caller-owned snapshot, validated and cloned on each load.
#[derive(Clone, Debug, PartialEq)]
pub struct MemorySnapshotStore(pub GraphSnapshot);

impl SnapshotStore for MemorySnapshotStore {
    fn load(&self) -> Result<GraphSnapshot, String> {
        self.0.validate()?;
        Ok(self.0.clone())
    }
}

/// Read a snapshot from a UTF-8 JSON file without modifying it.
#[derive(Clone, Debug, PartialEq)]
pub struct JsonFileSnapshotStore {
    pub path: PathBuf,
}

impl SnapshotStore for JsonFileSnapshotStore {
    fn load(&self) -> Result<GraphSnapshot, String> {
        let content = fs::read_to_string(&self.path).map_err(|error| {
            format!(
                "could not read graph snapshot {}: {error}",
                self.path.display()
            )
        })?;
        let snapshot: GraphSnapshot = serde_json::from_str(&content).map_err(|error| {
            format!(
                "could not parse graph snapshot {}: {error}",
                self.path.display()
            )
        })?;
        snapshot.validate()?;
        Ok(snapshot)
    }
}

/// Derive an explicitly unaccepted native-evaluator contract candidate.
///
/// Rules describe positive observations: the subject's kind, its complete
/// attributes value when non-null, and its outgoing relationship signatures.
/// They do not assert graph completeness, forbid additional edges, establish
/// edge multiplicity, or promote evidence references into behavioral proof.
/// Source identity is retained so an accepted contract can later be compared
/// against another snapshot without silently regenerating its expectations.
pub fn derive_candidate(graph: &GraphSnapshot, subject: &str) -> Result<Value, String> {
    graph.validate()?;
    graph.require_node(subject)?;
    let node = &graph.nodes[subject];
    let node_pointer = format!("/nodes/{}", escape_pointer_segment(subject));
    let mut rules = vec![serde_json::json!({
        "id": "node-kind",
        "pointer": format!("{node_pointer}/kind"),
        "kind": "equals",
        "expected": &node.kind,
    })];

    if !node.attributes.is_null() {
        rules.push(serde_json::json!({
            "id": "node-attributes",
            "pointer": format!("{node_pointer}/attributes"),
            "kind": "equals",
            "expected": &node.attributes,
        }));
    }

    for edge in graph.outgoing(subject, None) {
        rules.push(serde_json::json!({
            "id": format!("edge:{}", edge.id),
            "pointer": "/edges",
            "kind": "contains",
            "expected": {
                "from": &edge.from,
                "to": &edge.to,
                "relation": &edge.relation,
            },
        }));
    }

    Ok(serde_json::json!({
        "status": "candidate",
        "subject": subject,
        "source": {
            "snapshot": &graph.id,
            "revision": &graph.revision,
            "digest": graph.digest(),
            "digest_basis": "normalized_graph_snapshot_v1",
        },
        "contract": {
            "schema_version": 1,
            "rules": rules,
        },
    }))
}

fn escape_pointer_segment(segment: &str) -> String {
    segment.replace('~', "~0").replace('/', "~1")
}
