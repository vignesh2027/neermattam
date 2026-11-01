//! Neermattam engine: fuses street-level flood depth reports into a live hex
//! map of seven Indian cities (Chennai, Mumbai, Bengaluru, Hyderabad, Kolkata,
//! Delhi and Visakhapatnam by default), rates the hazard to people with the UK Defra and
//! Environment Agency formula, and routes each kind of vehicle around water it
//! cannot safely cross.
//!
//! Module map:
//!
//! - [`fusion`]: per-cell Gaussian state with time decay and inverse-variance
//!   updates on an H3 grid.
//! - [`hazard`]: the FD2320 / FD2321 flood hazard rating and its classes.
//! - [`vehicle`]: per-vehicle depth limits, flow limits and speeds, with sources.
//! - [`graph`]: the compact directed road graph loaded from an OSM PBF extract.
//! - [`routing`]: A* over the road graph, water-aware and baseline.
//! - [`city`]: the covered cities, their metro boxes and road extracts.
//! - [`analytics`]: control-room hotspots, summaries and drain rates.
//! - [`sites`]: critical sites (data centres first), their watch zones and
//!   access routes; [`sites_api`] serves them.
//! - [`ledger`]: the long-lived risk record per area, for siting.
//! - [`api`]: the HTTP service described in `docs/CONTRACTS.md` section 2.
//! - [`persist`]: atomic snapshots of the cell map, with migration.
//! - [`config`]: environment configuration.
//!
//! Every route the engine returns is advisory. A cell with too little
//! evidence is reported as unknown, never as safe.

#![forbid(unsafe_code)]
#![warn(missing_docs)]

pub mod analytics;
pub mod api;
pub mod city;
pub mod config;
pub mod fusion;
pub mod geo;
pub mod graph;
pub mod hazard;
pub mod ledger;
pub mod persist;
pub mod routing;
pub mod sites;
pub mod sites_api;
pub mod vehicle;
