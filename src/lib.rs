//! Reusable experiments in observed graphs, contract evaluation and declared reuse.
//!
//! Graph observations, intended contracts and implementation evidence retain
//! separate identities. The composition experiment reasons about declared
//! integer-domain contracts; it does not certify implementation behavior.

pub mod composition;
pub mod evaluation;
pub mod graph;
pub mod provider;
