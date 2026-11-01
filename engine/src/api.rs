//! The HTTP API, as specified in `docs/CONTRACTS.md` section 2.
//!
//! - `POST /v1/readings`: fuse one depth reading from inside any covered city.
//! - `GET /v1/cells?bbox=minLon,minLat,maxLon,maxLat&city=...`: GeoJSON hex
//!   cells; at least one of `bbox` and `city`.
//! - `GET /v1/route?from=lat,lon&to=lat,lon&vehicle=...`: water-aware route on
//!   the graph of the city holding both ends, as a GeoJSON LineString Feature,
//!   with the baseline route in a `baseline` member alongside.
//! - `GET /v1/stream?city=...`: server-sent events, one `cell` event per
//!   changed cell.
//! - `GET /v1/cities`: the covered cities with their boxes and graphs.
//! - `GET /v1/hotspots?city=...&limit=...`: cells ranked by an explained score.
//! - `GET /v1/summary?city=...`: counts for a control room.
//! - `GET /v1/recession?cell=...`: a cell's depth history and drain rate.
//! - `GET /v1/export?city=...&format=geojson|csv`: the city's cells.
//! - `GET|POST /v1/sites`, `GET|DELETE /v1/sites/{id}`, `GET /v1/siting`:
//!   critical sites and the risk ledger, in [`crate::sites_api`].
//! - `GET /healthz`, `GET /readyz`.
//!
//! Every map endpoint (readings, cells, route, stream, hotspots, summary,
//! recession, export) takes `layer=live` (the default) or `layer=replay`.
//! The replay layer is a second, separate map for a labelled simulated
//! scenario: it is never saved, never feeds the risk ledger or the critical
//! sites, and nothing posted to it can reach the live map.
//!
//! Every error is JSON, `{"error": "..."}`, with status 400 for invalid
//! input, 404 for an unknown path, an unknown cell or when no road connects
//! the points, 413 for an oversized body and 503 while a road graph is
//! loading or has failed.

use crate::analytics;
use crate::city::{City, CityLookup};
use crate::config::Config;
use crate::fusion::{CellView, Grid, Observation};
use crate::graph::{GraphStats, RoadGraph};
use crate::hazard::{assess, HazardClass};
use crate::persist;
use crate::routing::{blocking_estimate, plan_route, RouteError, RouteKind, RouteSummary};
use crate::vehicle::{CrossingLimit, Vehicle};
use axum::extract::rejection::{JsonRejection, QueryRejection};
use axum::extract::{DefaultBodyLimit, Query, State};
use axum::http::{header, HeaderValue, StatusCode};
use axum::response::sse::{Event, KeepAlive, Sse};
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post};
use axum::{Json, Router};
use chrono::{DateTime, Utc};
use futures_util::{Stream, StreamExt};
use parking_lot::{Mutex, RwLock};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::collections::{HashMap, VecDeque};
use std::convert::Infallible;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, OnceLock};
use tokio::sync::{broadcast, watch};
use tokio_stream::wrappers::errors::BroadcastStreamRecvError;
use tokio_stream::wrappers::BroadcastStream;
use tower_http::timeout::TimeoutLayer;
use tower_http::trace::{DefaultMakeSpan, DefaultOnResponse, TraceLayer};
use tracing::{info, warn, Level};

/// Capacity of the change broadcast. Slow SSE clients that fall further behind
/// get a `lagged` comment and should refetch `/v1/cells`.
const EVENT_BUFFER: usize = 4096;

/// How far in the future `observed_at` may be, for client clock drift.
/// Later times are refused, and times up to this far ahead are clamped to
/// the server clock. A reading dated hours ahead would hold its cell at that
/// time, and every real reading would then arrive "late", be decayed across
/// the gap and lose almost all its weight: one future-dated dry report could
/// hide deep water for a day.
pub const MAX_FUTURE_SKEW_SECS: i64 = 300;

/// Which map a request reads or writes.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum Layer {
    /// The live map of real reports: saved, fed to the risk ledger and the
    /// critical sites. The default.
    Live,
    /// A separate map for a labelled simulated replay. Kept in memory only,
    /// never mixed with the live map.
    Replay,
}

/// The optional `layer` query parameter.
pub(crate) fn layer_param(q: &HashMap<String, String>) -> Result<Layer, ApiError> {
    match q.get("layer").map(|s| s.trim()) {
        None | Some("" | "live") => Ok(Layer::Live),
        Some("replay") => Ok(Layer::Replay),
        Some(other) => Err(ApiError::bad_request(format!(
            "layer must be live or replay, got '{other}'"
        ))),
    }
}

/// A bounded map of recently fused reading ids to the cell each fell in, so
/// a retried POST is not fused twice and is answered with the original cell.
#[derive(Debug)]
struct RecentIds {
    cells: HashMap<Arc<str>, h3o::CellIndex>,
    order: VecDeque<Arc<str>>,
    cap: usize,
}

impl RecentIds {
    fn new(cap: usize) -> Self {
        RecentIds {
            cells: HashMap::new(),
            order: VecDeque::new(),
            cap: cap.max(1),
        }
    }

    /// The cell an id was fused into, if it is remembered.
    fn get(&self, id: &str) -> Option<h3o::CellIndex> {
        self.cells.get(id).copied()
    }

    /// Remember `id`, forgetting the oldest id when full.
    fn insert(&mut self, id: &str, cell: h3o::CellIndex) {
        if self.cells.contains_key(id) {
            return;
        }
        if self.order.len() >= self.cap {
            if let Some(old) = self.order.pop_front() {
                self.cells.remove(&old);
            }
        }
        let id: Arc<str> = Arc::from(id);
        self.cells.insert(Arc::clone(&id), cell);
        self.order.push_back(id);
    }
}

/// One changed cell, as broadcast to `/v1/stream` subscribers.
#[derive(Debug)]
struct CellEvent {
    /// The cell, so a city filter can use [`City::holds_cell`].
    cell: h3o::CellIndex,
    /// The GeoJSON Feature, serialised.
    data: String,
}

/// A city's road graph slot: empty while loading, then the graph or an error.
#[derive(Debug, Default)]
struct GraphSlot {
    graph: OnceLock<Arc<RoadGraph>>,
    error: Mutex<Option<String>>,
}

/// Where a city's road graph stands.
#[derive(Clone, Debug)]
pub enum GraphStatus {
    /// Still loading.
    Loading,
    /// Loaded.
    Ready(GraphStats),
    /// Failed to load, with the reason.
    Failed(String),
}

/// Shared server state.
pub struct AppState {
    /// Configuration.
    pub config: Config,
    /// The live cell map, shared by every city (the H3 grid is global).
    pub grid: RwLock<Grid>,
    /// The replay map: a labelled simulated scenario, kept apart from the
    /// live map in memory only (see [`Layer`]).
    pub replay: RwLock<Grid>,
    replay_events: broadcast::Sender<Arc<CellEvent>>,
    replay_ids: Mutex<RecentIds>,
    /// One slot per configured city, in `config.cities` order.
    graphs: Vec<GraphSlot>,
    events: broadcast::Sender<Arc<CellEvent>>,
    dirty: AtomicBool,
    /// The history changed since the history file was last written.
    history_dirty: AtomicBool,
    recent_ids: Mutex<RecentIds>,
    shutdown_tx: watch::Sender<bool>,
    /// Critical sites and the risk ledger.
    pub sites: crate::sites::SitesState,
}

impl AppState {
    /// Fresh state with an empty map and no graph yet.
    #[must_use]
    pub fn new(config: Config) -> Arc<Self> {
        let grid = Grid::new(config.fusion, config.resolution);
        let (events, _) = broadcast::channel(EVENT_BUFFER);
        let (replay_events, _) = broadcast::channel(EVENT_BUFFER);
        let replay = Grid::new(config.fusion, config.resolution);
        let replay_ids = RecentIds::new(config.dedupe_capacity);
        let (shutdown_tx, _) = watch::channel(false);
        let sites = crate::sites::SitesState::new(&config);
        if let Some(e) = &sites.registry.load_error {
            warn!(error = %e, "OpenStreetMap sites not loaded; only operator sites are available");
        }
        Arc::new(AppState {
            sites,
            recent_ids: Mutex::new(RecentIds::new(config.dedupe_capacity)),
            graphs: config.cities.iter().map(|_| GraphSlot::default()).collect(),
            config,
            replay: RwLock::new(replay),
            replay_events,
            replay_ids: Mutex::new(replay_ids),
            grid: RwLock::new(grid),
            events,
            dirty: AtomicBool::new(false),
            history_dirty: AtomicBool::new(false),
            shutdown_tx,
        })
    }

    /// The map a layer names.
    #[must_use]
    pub fn grid_of(&self, layer: Layer) -> &RwLock<Grid> {
        match layer {
            Layer::Live => &self.grid,
            Layer::Replay => &self.replay,
        }
    }

    fn events_of(&self, layer: Layer) -> &broadcast::Sender<Arc<CellEvent>> {
        match layer {
            Layer::Live => &self.events,
            Layer::Replay => &self.replay_events,
        }
    }

    fn ids_of(&self, layer: Layer) -> &Mutex<RecentIds> {
        match layer {
            Layer::Live => &self.recent_ids,
            Layer::Replay => &self.replay_ids,
        }
    }

    fn slot(&self, city: &str) -> Option<&GraphSlot> {
        self.config
            .cities
            .iter()
            .position(|c| c.id == city)
            .map(|i| &self.graphs[i])
    }

    /// Install a city's loaded road graph. Returns false for an unknown city;
    /// later calls for the same city are ignored.
    pub fn set_graph(&self, city: &str, g: RoadGraph) -> bool {
        self.slot(city)
            .map(|s| {
                let _ = s.graph.set(Arc::new(g));
            })
            .is_some()
    }

    /// Record why a city's graph failed to load (reported by `/readyz`).
    pub fn set_graph_error(&self, city: &str, e: String) {
        if let Some(s) = self.slot(city) {
            *s.error.lock() = Some(e);
        }
    }

    /// A city's road graph, once loaded.
    #[must_use]
    pub fn graph(&self, city: &str) -> Option<Arc<RoadGraph>> {
        self.slot(city).and_then(|s| s.graph.get().cloned())
    }

    /// Where a city's road graph stands. Unknown cities report a failure.
    #[must_use]
    pub fn graph_status(&self, city: &str) -> GraphStatus {
        match self.slot(city) {
            None => GraphStatus::Failed(format!("'{city}' is not a configured city")),
            Some(s) => match (s.graph.get(), s.error.lock().clone()) {
                (Some(g), _) => GraphStatus::Ready(g.stats()),
                (None, Some(e)) => GraphStatus::Failed(e),
                (None, None) => GraphStatus::Loading,
            },
        }
    }

    /// Ask streaming responses to end, so graceful shutdown can finish.
    pub fn begin_shutdown(&self) {
        let _ = self.shutdown_tx.send(true);
    }

    /// Write a snapshot now if persistence is on. Returns cells written.
    pub fn save_snapshot(&self) -> Result<usize, persist::PersistError> {
        let Some(path) = &self.config.snapshot_path else {
            return Ok(0);
        };
        self.dirty.store(false, Ordering::SeqCst);
        let (bytes, n) = {
            let g = self.grid.read();
            (persist::encode(&g, Utc::now()), g.len())
        };
        persist::write_atomic(path, &bytes).inspect_err(|_| {
            // Try again on the next tick.
            self.dirty.store(true, Ordering::SeqCst);
        })?;
        Ok(n)
    }

    /// Ask for the history file to be written at the next history tick (used
    /// after start-up, so history read from an older snapshot, or recovered
    /// after a crash, reaches the history file without waiting for a reading).
    pub fn mark_history_dirty(&self) {
        self.history_dirty.store(true, Ordering::SeqCst);
    }

    /// Write the history file now if persistence is on. The history is copied
    /// under the read lock and encoded outside it. Returns points written.
    pub fn save_history(&self) -> Result<usize, persist::PersistError> {
        let Some(path) = self.config.history_path() else {
            return Ok(0);
        };
        self.history_dirty.store(false, Ordering::SeqCst);
        let snap = persist::copy_history(&self.grid.read(), Utc::now());
        persist::write_atomic(&path, &snap.encode()).inspect_err(|_| {
            self.history_dirty.store(true, Ordering::SeqCst);
        })?;
        Ok(snap.points())
    }

    /// Run until shutdown: every snapshot interval, drop forgotten cells and
    /// save the cell snapshot if it changed; every history interval, save the
    /// history file if it changed. `main` saves both once more on the way out.
    pub async fn snapshot_loop(self: Arc<Self>) {
        let mut tick = tokio::time::interval(self.config.snapshot_interval);
        tick.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
        let mut history_tick = tokio::time::interval(self.config.history_snapshot_interval);
        history_tick.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
        let mut stop = self.shutdown_tx.subscribe();
        loop {
            let history_due = tokio::select! {
                _ = tick.tick() => false,
                _ = history_tick.tick() => true,
                _ = stop.wait_for(|v| *v) => break,
            };
            if history_due {
                if self.history_dirty.load(Ordering::SeqCst) {
                    let st = Arc::clone(&self);
                    match tokio::task::spawn_blocking(move || st.save_history()).await {
                        Ok(Ok(n)) => tracing::debug!(points = n, "history saved"),
                        Ok(Err(e)) => warn!(error = %e, "history snapshot failed"),
                        Err(e) => warn!(error = %e, "history snapshot task failed"),
                    }
                }
                continue;
            }
            {
                let st = Arc::clone(&self);
                match tokio::task::spawn_blocking(move || crate::sites_api::save_ledger(&st)).await
                {
                    Ok(Ok(_)) => {}
                    Ok(Err(e)) => warn!(error = %e, "risk ledger not saved"),
                    Err(e) => warn!(error = %e, "risk ledger task failed"),
                }
            }
            // The replay map is never saved; it only forgets, like the live one.
            self.replay.write().forget_old(Utc::now());
            let forgotten = self.grid.write().forget_old(Utc::now());
            if forgotten > 0 {
                info!(
                    forgotten,
                    "dropped cells with no evidence for the forget window"
                );
                self.dirty.store(true, Ordering::SeqCst);
                self.history_dirty.store(true, Ordering::SeqCst);
            }
            if self.dirty.load(Ordering::SeqCst) {
                let st = Arc::clone(&self);
                match tokio::task::spawn_blocking(move || st.save_snapshot()).await {
                    Ok(Ok(n)) => tracing::debug!(cells = n, "snapshot saved"),
                    Ok(Err(e)) => warn!(error = %e, "snapshot failed"),
                    Err(e) => warn!(error = %e, "snapshot task failed"),
                }
            }
        }
    }
}

/// A JSON error response.
#[derive(Debug)]
pub struct ApiError {
    status: StatusCode,
    message: String,
}

impl ApiError {
    /// An error with any status.
    pub(crate) fn new(status: StatusCode, message: impl Into<String>) -> Self {
        ApiError {
            status,
            message: message.into(),
        }
    }

    pub(crate) fn bad_request(message: impl Into<String>) -> Self {
        ApiError {
            status: StatusCode::BAD_REQUEST,
            message: message.into(),
        }
    }

    pub(crate) fn not_found(message: impl Into<String>) -> Self {
        ApiError {
            status: StatusCode::NOT_FOUND,
            message: message.into(),
        }
    }
}

impl IntoResponse for ApiError {
    fn into_response(self) -> Response {
        (self.status, Json(json!({ "error": self.message }))).into_response()
    }
}

impl From<JsonRejection> for ApiError {
    fn from(r: JsonRejection) -> Self {
        let status = match r {
            JsonRejection::JsonDataError(_) | JsonRejection::JsonSyntaxError(_) => {
                StatusCode::BAD_REQUEST
            }
            ref other => other.status(),
        };
        ApiError {
            status,
            message: r.body_text(),
        }
    }
}

impl From<QueryRejection> for ApiError {
    fn from(r: QueryRejection) -> Self {
        ApiError::bad_request(r.body_text())
    }
}

fn r1(x: f64) -> f64 {
    (x * 10.0).round() / 10.0
}

fn r2(x: f64) -> f64 {
    (x * 100.0).round() / 100.0
}

fn r6(x: f64) -> f64 {
    (x * 1e6).round() / 1e6
}

/// Body of `POST /v1/readings`.
#[derive(Clone, Debug, Deserialize)]
pub struct ReadingIn {
    /// Caller's id, used to ignore retries.
    pub reading_id: Option<String>,
    /// Latitude.
    pub lat: f64,
    /// Longitude.
    pub lon: f64,
    /// Depth, cm.
    pub depth_cm: f64,
    /// Depth sigma, cm.
    pub depth_sigma_cm: f64,
    /// Velocity, m/s.
    pub velocity_mps: Option<f64>,
    /// Velocity sigma, m/s.
    pub velocity_sigma_mps: Option<f64>,
    /// When the water was seen.
    pub observed_at: DateTime<Utc>,
    /// `photo`, `video`, `voice` or `official`.
    pub source: Option<String>,
    /// Trust in (0, 1].
    pub trust: Option<f64>,
}

/// Each covered city with its box, for error messages.
fn coverage(cfg: &Config) -> String {
    cfg.cities
        .iter()
        .map(|c| format!("{} {}", c.id, c.bbox))
        .collect::<Vec<_>>()
        .join("; ")
}

/// The id of the city a cell belongs to (see [`City::holds_cell`]).
fn city_of_cell(cfg: &Config, cell: h3o::CellIndex) -> Option<String> {
    cfg.cities.city_of_cell(cell).map(|c| c.id.clone())
}

/// Check a reading against the contract. Returns the observation to fuse or a
/// message for a 400 response.
pub fn validate_reading(
    r: &ReadingIn,
    cfg: &Config,
    now: DateTime<Utc>,
) -> Result<Observation, String> {
    if let Some(id) = &r.reading_id {
        if id.is_empty() || id.len() > 128 || id.chars().any(char::is_control) {
            return Err("reading_id must be 1 to 128 printable characters".into());
        }
    }
    if !(r.lat.is_finite() && r.lon.is_finite()) || cfg.city_at(r.lat, r.lon).is_none() {
        return Err(format!(
            "lat/lon ({}, {}) is outside the service area: it is not inside any covered city ({})",
            r.lat,
            r.lon,
            coverage(cfg)
        ));
    }
    if !(r.depth_cm.is_finite() && (0.0..=500.0).contains(&r.depth_cm)) {
        return Err(format!(
            "depth_cm must be between 0 and 500, got {}",
            r.depth_cm
        ));
    }
    if !(r.depth_sigma_cm.is_finite() && r.depth_sigma_cm > 0.0 && r.depth_sigma_cm <= 500.0) {
        return Err(format!(
            "depth_sigma_cm must be greater than 0 and at most 500, got {}",
            r.depth_sigma_cm
        ));
    }
    let (velocity_mps, velocity_sigma_mps) = match (r.velocity_mps, r.velocity_sigma_mps) {
        (None, _) => (None, None),
        (Some(v), _) if !(v.is_finite() && (0.0..=10.0).contains(&v)) => {
            return Err(format!("velocity_mps must be between 0 and 10, got {v}"));
        }
        (Some(_), None) => {
            return Err("velocity_sigma_mps is required when velocity_mps is given".into());
        }
        (Some(_), Some(s)) if !(s.is_finite() && s > 0.0 && s <= 10.0) => {
            return Err(format!(
                "velocity_sigma_mps must be greater than 0 and at most 10, got {s}"
            ));
        }
        (Some(v), Some(s)) => (Some(v), Some(s)),
    };
    let ahead_s = (r.observed_at - now).num_seconds();
    if ahead_s > MAX_FUTURE_SKEW_SECS {
        return Err(format!(
            "observed_at {} is in the future; the server time is {} and at most {} seconds of clock drift is allowed",
            r.observed_at.to_rfc3339(),
            now.to_rfc3339(),
            MAX_FUTURE_SKEW_SECS
        ));
    }
    let age_h = -(ahead_s as f64) / 3600.0;
    if age_h > cfg.max_observation_skew_h {
        return Err(format!(
            "observed_at {} is more than {} hours before the server time {}",
            r.observed_at.to_rfc3339(),
            cfg.max_observation_skew_h,
            now.to_rfc3339()
        ));
    }
    // Small clock drift ahead of the server is clamped, so no cell state is
    // ever dated in the future.
    let observed_at = r.observed_at.min(now);
    if let Some(s) = &r.source {
        if !matches!(s.as_str(), "photo" | "video" | "voice" | "official") {
            return Err(format!(
                "source must be photo, video, voice or official, got '{s}'"
            ));
        }
    }
    let trust = r.trust.unwrap_or(1.0);
    if !(trust.is_finite() && trust > 0.0 && trust <= 1.0) {
        return Err(format!(
            "trust must be greater than 0 and at most 1, got {trust}"
        ));
    }
    Ok(Observation {
        lat: r.lat,
        lon: r.lon,
        depth_cm: r.depth_cm,
        depth_sigma_cm: r.depth_sigma_cm,
        velocity_mps,
        velocity_sigma_mps,
        observed_at,
        trust,
    })
}

/// Properties of one cell, shared by `/v1/cells`, `/v1/stream` and the
/// readings response.
#[derive(Clone, Debug, PartialEq, Serialize)]
pub struct CellProperties {
    /// H3 index, hex.
    pub cell: String,
    /// The covered city the cell's centre lies in, if any.
    pub city: Option<String>,
    /// Mean depth, cm.
    pub depth_cm: f64,
    /// Depth standard deviation, cm.
    pub depth_sigma_cm: f64,
    /// The depth routing judges the cell at (see `routing::blocking_estimate`).
    pub depth_upper_cm: f64,
    /// Standard deviation of the fused measurements alone, cm.
    pub depth_sigma_meas_cm: f64,
    /// The deeper side of a conflict in force, cm; routing blocks on it.
    pub conflict_high_mean_cm: Option<f64>,
    /// Mean velocity, m/s, when known.
    pub velocity_mps: Option<f64>,
    /// Velocity standard deviation, m/s, when known.
    pub velocity_sigma_mps: Option<f64>,
    /// True when the hazard used the default velocity.
    pub velocity_assumed: bool,
    /// Defra hazard rating at the mean, or null when unknown.
    pub hazard_rating: Option<f64>,
    /// Hazard class at the mean.
    pub hazard_class: HazardClass,
    /// Hazard class at the upper estimate.
    pub hazard_class_upper: HazardClass,
    /// Debris factor used, or null when unknown.
    pub debris_factor: Option<f64>,
    /// Too little evidence. Never treat as safe.
    pub unknown: bool,
    /// Every piece of evidence is older than the expiry window: unknown, and
    /// no longer blocks routes.
    pub expired: bool,
    /// Direct readings.
    pub n_readings: u32,
    /// Readings from adjacent cells.
    pub n_neighbour: u32,
    /// Direct readings that disagreed with the cell's estimate.
    pub conflicts: u32,
    /// Latest evidence time.
    pub updated_at: DateTime<Utc>,
    /// Direct depth estimate just after the latest direct reading, cm.
    pub last_depth_cm: Option<f64>,
    /// Hours since the latest direct reading.
    pub last_reading_age_h: Option<f64>,
}

/// Build the properties for a cell view.
#[must_use]
pub fn cell_properties(v: &CellView, cfg: &Config) -> CellProperties {
    let p = &cfg.policy;
    let mean = assess(
        v.depth.mean,
        v.velocity.map(|g| g.mean),
        p.default_velocity_mps,
        p.debris_rule,
    );
    let (depth_upper, velocity_upper, assumed) = blocking_estimate(v, p);
    let upper = assess(
        depth_upper,
        (!assumed).then_some(velocity_upper),
        velocity_upper,
        p.debris_rule,
    );
    CellProperties {
        cell: v.cell.to_string(),
        city: city_of_cell(cfg, v.cell),
        depth_cm: r1(v.depth.mean),
        depth_sigma_cm: r1(v.depth.sigma()),
        depth_upper_cm: r1(depth_upper),
        depth_sigma_meas_cm: r1(v.sigma_meas_cm),
        conflict_high_mean_cm: v.conflict_high_mean_cm.map(r1),
        velocity_mps: v.velocity.map(|g| r2(g.mean)),
        velocity_sigma_mps: v.velocity.map(|g| r2(g.sigma())),
        velocity_assumed: mean.velocity_assumed,
        hazard_rating: (!v.unknown).then(|| r2(mean.rating)),
        hazard_class: if v.unknown {
            HazardClass::Unknown
        } else {
            mean.class
        },
        hazard_class_upper: if v.unknown {
            HazardClass::Unknown
        } else {
            upper.class
        },
        debris_factor: (!v.unknown).then_some(mean.debris_factor),
        unknown: v.unknown,
        expired: v.expired,
        n_readings: v.n_readings,
        n_neighbour: v.n_neighbour,
        conflicts: v.conflicts,
        updated_at: v.updated_at,
        last_depth_cm: v.last_depth_cm.map(r1),
        last_reading_age_h: v.last_reading_age_h.map(r2),
    }
}

/// A GeoJSON Feature for a cell, with its hexagon as the geometry.
#[must_use]
pub fn cell_feature(v: &CellView, cfg: &Config) -> Value {
    let mut ring: Vec<[f64; 2]> = v
        .cell
        .boundary()
        .iter()
        .map(|ll| [r6(ll.lng()), r6(ll.lat())])
        .collect();
    if let Some(first) = ring.first().copied() {
        ring.push(first);
    }
    json!({
        "type": "Feature",
        "id": v.cell.to_string(),
        "geometry": { "type": "Polygon", "coordinates": [ring] },
        "properties": cell_properties(v, cfg),
    })
}

#[derive(Serialize)]
struct HazardOut {
    rating: Option<f64>,
    class: HazardClass,
    class_upper: HazardClass,
    debris_factor: Option<f64>,
    velocity_mps: f64,
    velocity_assumed: bool,
}

#[derive(Serialize)]
struct ReadingOut {
    reading_id: Option<String>,
    layer: Layer,
    cell: String,
    city: Option<String>,
    depth_cm: f64,
    depth_sigma_cm: f64,
    depth_upper_cm: f64,
    velocity_mps: Option<f64>,
    velocity_sigma_mps: Option<f64>,
    unknown: bool,
    n_readings: u32,
    conflicts: u32,
    updated_at: DateTime<Utc>,
    hazard: HazardOut,
    duplicate: bool,
}

fn reading_out(
    st: &AppState,
    layer: Layer,
    cell: h3o::CellIndex,
    id: Option<String>,
    duplicate: bool,
    now: DateTime<Utc>,
) -> Result<ReadingOut, ApiError> {
    let view = st
        .grid_of(layer)
        .read()
        .view(cell, now)
        .ok_or_else(|| ApiError {
            status: StatusCode::INTERNAL_SERVER_ERROR,
            message: "cell state missing after update".into(),
        })?;
    let p = cell_properties(&view, &st.config);
    let pol = &st.config.policy;
    Ok(ReadingOut {
        reading_id: id,
        layer,
        cell: p.cell,
        city: p.city,
        depth_cm: p.depth_cm,
        depth_sigma_cm: p.depth_sigma_cm,
        depth_upper_cm: p.depth_upper_cm,
        velocity_mps: p.velocity_mps,
        velocity_sigma_mps: p.velocity_sigma_mps,
        unknown: p.unknown,
        n_readings: p.n_readings,
        conflicts: p.conflicts,
        updated_at: p.updated_at,
        hazard: HazardOut {
            rating: p.hazard_rating,
            class: p.hazard_class,
            class_upper: p.hazard_class_upper,
            debris_factor: p.debris_factor,
            velocity_mps: view
                .velocity
                .map_or(pol.default_velocity_mps, |v| r2(v.mean)),
            velocity_assumed: p.velocity_assumed,
        },
        duplicate,
    })
}

async fn post_reading(
    State(st): State<Arc<AppState>>,
    q: Result<Query<HashMap<String, String>>, QueryRejection>,
    body: Result<Json<ReadingIn>, JsonRejection>,
) -> Result<Json<ReadingOut>, ApiError> {
    let Query(q) = q?;
    let layer = layer_param(&q)?;
    let Json(r) = body?;
    let now = Utc::now();
    let obs = validate_reading(&r, &st.config, now).map_err(ApiError::bad_request)?;

    // The id check, the fusion and the id record happen under one lock, so a
    // retry that races the original waits for it and is then answered from
    // the cell the original was fused into. Lock order: ids, then grid.
    let ingested = {
        let mut ids = st.ids_of(layer).lock();
        if let Some((id, cell)) = r
            .reading_id
            .as_deref()
            .and_then(|id| ids.get(id).map(|c| (id, c)))
        {
            drop(ids);
            info!(reading_id = %id, cell = %cell, "duplicate reading ignored");
            return reading_out(&st, layer, cell, r.reading_id.clone(), true, now)
                .map_err(|_| ApiError {
                    status: StatusCode::CONFLICT,
                    message: format!(
                        "reading_id '{id}' was already fused, and its cell has since been forgotten"
                    ),
                })
                .map(Json);
        }
        let ingested = {
            let mut g = st.grid_of(layer).write();
            let ingested = g
                .ingest(&obs)
                .map_err(|e| ApiError::bad_request(e.to_string()))?;
            if layer == Layer::Replay {
                // A replay never reaches the risk ledger or the snapshot.
                drop(g);
                if let Some(id) = &r.reading_id {
                    ids.insert(id, ingested.cell);
                }
                drop(ids);
                publish(&st, layer, &ingested.touched, now);
                let out = reading_out(&st, layer, ingested.cell, r.reading_id.clone(), false, now)?;
                return Ok(Json(out));
            }
            // The risk ledger keeps the cell's fused direct level just after
            // this reading (its history point), or the reading itself when
            // history is off.
            let (fused, sigma) = g
                .state(ingested.cell)
                .and_then(|s| s.history.iter().rev().find(|p| p.at == obs.observed_at))
                .map_or((obs.depth_cm, obs.depth_sigma_cm), |p| {
                    (p.fused_cm, p.fused_sigma_cm)
                });
            st.sites.ledger.lock().record(
                ingested.cell,
                obs.observed_at,
                fused,
                sigma,
                r.source.is_some(),
            );
            st.sites.ledger_dirty.store(true, Ordering::SeqCst);
            ingested
        };
        if let Some(id) = &r.reading_id {
            ids.insert(id, ingested.cell);
        }
        ingested
    };
    st.dirty.store(true, Ordering::SeqCst);
    st.history_dirty.store(true, Ordering::SeqCst);

    // Publish every changed cell. The events are sent while the read lock is
    // held, so no later write can slip in between building a cell's view and
    // sending it: a subscriber never receives an older state after a newer one.
    publish(&st, layer, &ingested.touched, now);

    let out = reading_out(&st, layer, ingested.cell, r.reading_id.clone(), false, now)?;
    info!(
        reading_id = r.reading_id.as_deref().unwrap_or(""),
        source = r.source.as_deref().unwrap_or(""),
        cell = %ingested.cell,
        depth_cm = obs.depth_cm,
        depth_sigma_cm = obs.depth_sigma_cm,
        fused_depth_cm = out.depth_cm,
        fused_sigma_cm = out.depth_sigma_cm,
        "reading fused"
    );
    Ok(Json(out))
}

/// Publish every changed cell of a layer. The events are sent while the read
/// lock is held, so no later write can slip in between building a cell's view
/// and sending it: a subscriber never receives an older state after a newer
/// one.
fn publish(st: &AppState, layer: Layer, touched: &[h3o::CellIndex], now: DateTime<Utc>) {
    let g = st.grid_of(layer).read();
    for v in touched.iter().filter_map(|c| g.view(*c, now)) {
        // No subscribers is fine.
        let _ = st.events_of(layer).send(Arc::new(CellEvent {
            cell: v.cell,
            data: cell_feature(&v, &st.config).to_string(),
        }));
    }
}

/// Run CPU-heavy work (building views of a whole city, scoring, exporting)
/// on the blocking pool, so a large map never stalls the async workers that
/// serve readings and routes.
pub(crate) async fn blocking<T: Send + 'static>(
    f: impl FnOnce() -> T + Send + 'static,
) -> Result<T, ApiError> {
    tokio::task::spawn_blocking(f).await.map_err(|e| ApiError {
        status: StatusCode::INTERNAL_SERVER_ERROR,
        message: format!("task failed: {e}"),
    })
}

/// The optional `city` query parameter, checked against the configuration.
pub(crate) fn city_param<'a>(
    q: &HashMap<String, String>,
    cfg: &'a Config,
) -> Result<Option<&'a City>, ApiError> {
    match q.get("city").map(|s| s.trim()) {
        None => Ok(None),
        Some(id) => cfg.city(id).map(Some).ok_or_else(|| {
            ApiError::bad_request(format!(
                "unknown city '{id}'; covered cities are {}",
                cfg.cities.ids()
            ))
        }),
    }
}

/// A required `city` query parameter.
pub(crate) fn required_city<'a>(
    q: &HashMap<String, String>,
    cfg: &'a Config,
) -> Result<&'a City, ApiError> {
    city_param(q, cfg)?.ok_or_else(|| {
        ApiError::bad_request(format!(
            "city is required; covered cities are {}",
            cfg.cities.ids()
        ))
    })
}

/// True when the cell's centre is inside the box.
fn cell_in(bbox: &crate::geo::BBox, cell: h3o::CellIndex) -> bool {
    let c = h3o::LatLng::from(cell);
    bbox.contains(c.lat(), c.lng())
}

/// Every cell with state on the city's map (all cells when `city` is
/// `None`), sorted by index. Cells are filtered before their views are built,
/// so the read lock is held only as long as the city needs.
fn city_views(
    st: &AppState,
    layer: Layer,
    city: Option<&City>,
    now: DateTime<Utc>,
) -> Vec<CellView> {
    let mut views: Vec<CellView> = {
        let g = st.grid_of(layer).read();
        g.views_where(now, |cell| city.is_none_or(|c| c.holds_cell(cell)))
            .collect()
    };
    views.sort_unstable_by_key(|v| u64::from(v.cell));
    views
}

async fn get_cells(
    State(st): State<Arc<AppState>>,
    q: Result<Query<HashMap<String, String>>, QueryRejection>,
) -> Result<Json<Value>, ApiError> {
    let Query(q) = q?;
    let layer = layer_param(&q)?;
    let city = city_param(&q, &st.config)?;
    let bbox: Option<crate::geo::BBox> = q
        .get("bbox")
        .map(|raw| raw.parse().map_err(ApiError::bad_request))
        .transpose()?;
    let area = match (bbox, city) {
        (Some(b), _) => b,
        (None, Some(c)) => c.bbox,
        (None, None) => {
            return Err(ApiError::bad_request(
                "bbox or city is required: bbox=minLon,minLat,maxLon,maxLat or city=<id>",
            ))
        }
    };
    if area.area_deg2() > 1.0 {
        return Err(ApiError::bad_request(
            "bbox is too large: at most 1 square degree per request",
        ));
    }
    let now = Utc::now();
    // An explicit box selects by cell centre; a city alone selects its map.
    let mut views: Vec<CellView> = city_views(&st, layer, city, now)
        .into_iter()
        .filter(|v| bbox.is_none() || cell_in(&area, v.cell))
        .collect();
    let truncated = views.len() > st.config.max_cells;
    views.truncate(st.config.max_cells);
    let features: Vec<Value> = views.iter().map(|v| cell_feature(v, &st.config)).collect();
    Ok(Json(json!({
        "type": "FeatureCollection",
        "features": features,
        "truncated": truncated,
        "generated_at": now,
        "layer": layer,
    })))
}

fn parse_point(q: &HashMap<String, String>, key: &str) -> Result<(f64, f64), ApiError> {
    let raw = q
        .get(key)
        .ok_or_else(|| ApiError::bad_request(format!("{key} is required: {key}=lat,lon")))?;
    let parts: Vec<&str> = raw.split(',').map(str::trim).collect();
    let nums: Option<Vec<f64>> = parts
        .iter()
        .map(|p| p.parse::<f64>().ok().filter(|x| x.is_finite()))
        .collect();
    let (lat, lon) = match nums.as_deref() {
        Some([lat, lon]) => (*lat, *lon),
        _ => {
            return Err(ApiError::bad_request(format!(
                "{key} must be two numbers: lat,lon"
            )))
        }
    };
    Ok((lat, lon))
}

fn route_feature(
    s: &RouteSummary,
    vehicle: Vehicle,
    city: &City,
    cfg: &Config,
    now: DateTime<Utc>,
) -> Value {
    let mut coords: Vec<[f64; 2]> = s.coordinates.iter().map(|c| [r6(c[0]), r6(c[1])]).collect();
    if coords.len() == 1 {
        // Start and end snapped to the same node; GeoJSON needs two positions.
        coords.push(coords[0]);
    }
    let depth_limit_cm = match vehicle.profile().limit {
        CrossingLimit::Depth { max_depth_cm, .. } => Some(max_depth_cm),
        CrossingLimit::HazardClass => None,
    };
    json!({
        "type": "Feature",
        "geometry": { "type": "LineString", "coordinates": coords },
        "properties": {
            "vehicle": vehicle,
            "city": city.id,
            "route_kind": s.kind,
            "distance_m": s.distance_m.round(),
            "eta_min": r1(s.eta_min),
            "max_depth_cm_on_route": s.max_depth_cm_on_route.map(r1),
            "max_depth_sigma_cm": s.max_depth_sigma_cm.map(r1),
            "max_known_depth_cm": s.max_depth_cm_on_route.map(r1),
            "max_unknown_depth_cm": s.max_unknown_depth_cm.map(r1),
            "max_unknown_depth_age_h": s.max_unknown_depth_age_h.map(r1),
            "old_deep_cells_crossed": s.old_deep_cells_crossed,
            "cells_on_route": s.cells_on_route,
            "cells_avoided": s.cells_avoided,
            "unknown_cells_crossed": s.unknown_cells_crossed,
            "impassable_cells_crossed": s.impassable_cells_crossed,
            "passable": s.passable,
            "advisory": true,
            "explanation": s.explanation,
            "depth_limit_cm": depth_limit_cm,
            "pedestrian_block_class": (vehicle == Vehicle::Pedestrian).then_some(cfg.policy.pedestrian_block_class),
            "safety_sigmas": cfg.policy.safety_sigmas,
            "computed_at": now,
        }
    })
}

async fn get_route(
    State(st): State<Arc<AppState>>,
    q: Result<Query<HashMap<String, String>>, QueryRejection>,
) -> Result<Json<Value>, ApiError> {
    let Query(q) = q?;
    let layer = layer_param(&q)?;
    let cfg = &st.config;
    let from = parse_point(&q, "from")?;
    let to = parse_point(&q, "to")?;
    let vehicle: Vehicle = q
        .get("vehicle")
        .ok_or_else(|| {
            ApiError::bad_request(
                "vehicle is required: pedestrian, two_wheeler, auto, car, bus or truck",
            )
        })?
        .parse()
        .map_err(ApiError::bad_request)?;
    let outside = |key: &str, (lat, lon): (f64, f64)| {
        ApiError::bad_request(format!(
            "{key} ({lat}, {lon}) is outside every covered city ({}); note the order is lat,lon",
            coverage(cfg)
        ))
    };
    let from_city = cfg
        .city_at(from.0, from.1)
        .ok_or_else(|| outside("from", from))?;
    let to_city = cfg.city_at(to.0, to.1).ok_or_else(|| outside("to", to))?;
    // Where custom boxes overlap, use a city that holds both ends.
    let city = cfg.cities.city_holding(from, to).ok_or_else(|| {
        ApiError::bad_request(format!(
            "from is in {} and to is in {}; a route must start and end in the same city",
            from_city.name, to_city.name
        ))
    })?;
    let graph = match st.graph_status(&city.id) {
        GraphStatus::Ready(_) => st.graph(&city.id).expect("ready graph"),
        GraphStatus::Loading => {
            return Err(ApiError {
                status: StatusCode::SERVICE_UNAVAILABLE,
                message: format!(
                    "the road graph for {} is still loading; try again shortly",
                    city.name
                ),
            })
        }
        GraphStatus::Failed(e) => {
            return Err(ApiError {
                status: StatusCode::SERVICE_UNAVAILABLE,
                message: format!(
                    "the road graph for {} is unavailable, so routes there cannot be planned: {e}",
                    city.name
                ),
            })
        }
    };
    let now = Utc::now();
    let water = st.grid_of(layer).read().water_map(now);
    let policy = cfg.policy;
    let started = std::time::Instant::now();
    let result =
        tokio::task::spawn_blocking(move || plan_route(&graph, &water, from, to, vehicle, &policy))
            .await
            .map_err(|e| ApiError {
                status: StatusCode::INTERNAL_SERVER_ERROR,
                message: format!("routing task failed: {e}"),
            })?;
    let result = result.map_err(|e| match e {
        RouteError::NoPath(_) => ApiError {
            status: StatusCode::NOT_FOUND,
            message: e.to_string(),
        },
        _ => ApiError::bad_request(e.to_string()),
    })?;
    info!(
        vehicle = %vehicle,
        city = %city.id,
        kind = ?result.route.kind,
        distance_m = result.route.distance_m.round(),
        baseline_m = result.baseline.distance_m.round(),
        cells_avoided = result.route.cells_avoided,
        elapsed_us = started.elapsed().as_micros() as u64,
        "route planned"
    );
    let mut feature = route_feature(&result.route, vehicle, city, cfg, now);
    feature["baseline"] = route_feature(&result.baseline, vehicle, city, cfg, now);
    feature["properties"]["layer"] = json!(layer);
    feature["baseline"]["properties"]["layer"] = json!(layer);
    debug_assert!(result.route.kind != RouteKind::Baseline);
    Ok(Json(feature))
}

async fn stream(
    State(st): State<Arc<AppState>>,
    q: Result<Query<HashMap<String, String>>, QueryRejection>,
) -> Result<Sse<impl Stream<Item = Result<Event, Infallible>>>, ApiError> {
    let Query(q) = q?;
    let only: Option<City> = city_param(&q, &st.config)?.cloned();
    let rx = st.events_of(layer_param(&q)?).subscribe();
    let mut stop = st.shutdown_tx.subscribe();
    let stopped = async move {
        let _ = stop.wait_for(|v| *v).await;
    };
    let events = BroadcastStream::new(rx)
        .filter_map(move |item| {
            let out = match item {
                Ok(ev) if only.as_ref().is_none_or(|c| c.holds_cell(ev.cell)) => {
                    Some(Ok(Event::default().event("cell").data(&ev.data)))
                }
                Ok(_) => None,
                Err(BroadcastStreamRecvError::Lagged(n)) => Some(Ok(Event::default()
                    .comment(format!("lagged: {n} events dropped, refetch /v1/cells")))),
            };
            std::future::ready(out)
        })
        .take_until(stopped);
    Ok(Sse::new(events).keep_alive(KeepAlive::default()))
}

fn graph_json(status: &GraphStatus) -> Value {
    match status {
        GraphStatus::Loading => json!({ "status": "loading" }),
        GraphStatus::Failed(e) => json!({ "status": "failed", "error": e }),
        GraphStatus::Ready(s) => json!({
            "status": "ready",
            "ways": s.ways,
            "nodes": s.nodes,
            "edges": s.edges,
            "segments": s.segments,
            "segment_cell_refs": s.segment_cell_refs,
            "memory_bytes": s.memory_bytes,
            "memory_mib": r1(s.memory_bytes as f64 / 1_048_576.0),
            "parse_ms": s.parse_ms,
            "build_ms": s.build_ms,
        }),
    }
}

async fn get_cities(State(st): State<Arc<AppState>>) -> Result<Json<Value>, ApiError> {
    let now = Utc::now();
    // Counting needs only the cell ids, not their views.
    let counts: Vec<usize> = {
        let st = Arc::clone(&st);
        blocking(move || {
            let g = st.grid.read();
            st.config
                .cities
                .iter()
                .map(|c| g.states().filter(|(cell, _)| c.holds_cell(**cell)).count())
                .collect()
        })
        .await?
    };
    let cities: Vec<Value> = st
        .config
        .cities
        .iter()
        .zip(counts)
        .map(|(c, cells)| {
            let (lat, lon) = c.centre();
            json!({
                "id": c.id,
                "name": c.name,
                "bbox": [c.bbox.min_lon, c.bbox.min_lat, c.bbox.max_lon, c.bbox.max_lat],
                "centre": { "lat": r6(lat), "lon": r6(lon) },
                "cells": cells,
                "graph": graph_json(&st.graph_status(&c.id)),
            })
        })
        .collect();
    Ok(Json(json!({ "cities": cities, "generated_at": now })))
}

/// Default and largest `limit` for `/v1/hotspots`.
const HOTSPOT_DEFAULT_LIMIT: usize = 20;
const HOTSPOT_MAX_LIMIT: usize = 200;

async fn get_hotspots(
    State(st): State<Arc<AppState>>,
    q: Result<Query<HashMap<String, String>>, QueryRejection>,
) -> Result<Json<Value>, ApiError> {
    let Query(q) = q?;
    let layer = layer_param(&q)?;
    let city = city_param(&q, &st.config)?;
    let limit = match q.get("limit") {
        None => HOTSPOT_DEFAULT_LIMIT,
        Some(raw) => raw
            .trim()
            .parse::<usize>()
            .ok()
            .filter(|n| (1..=HOTSPOT_MAX_LIMIT).contains(n))
            .ok_or_else(|| {
                ApiError::bad_request(format!(
                    "limit must be a whole number from 1 to {HOTSPOT_MAX_LIMIT}, got '{raw}'"
                ))
            })?,
    };
    let now = Utc::now();
    let ranked = {
        let (st, city) = (Arc::clone(&st), city.cloned());
        blocking(move || {
            let views = city_views(&st, layer, city.as_ref(), now);
            let cfg = &st.config;
            let mut ranked = analytics::hotspots(views, &cfg.policy, &cfg.fusion, now, limit);
            name_hotspots(&st, &mut ranked);
            ranked
        })
        .await?
    };
    Ok(Json(json!({
        "city": city.map(|c| &c.id),
        "layer": layer,
        "generated_at": now,
        "advisory": true,
        "score": "depth 40 x min(depth_block / 150 cm, 1) + hazard 30 x min(HR / 3, 1) with HR the Defra rating at depth_block + freshness 20 x (1 - age_h / 12) + reports 10 x min(ln(1 + n) / ln(31), 1) with n the readings in force",
        "count": ranked.len(),
        "hotspots": ranked,
    })))
}

/// Name each hotspot after the nearest named road in its city's graph. A
/// city whose graph is not loaded leaves its hotspots unnamed.
fn name_hotspots(st: &AppState, ranked: &mut [analytics::Hotspot]) {
    for h in ranked {
        let Some(city) = st.config.city_at(h.lat, h.lon) else {
            continue;
        };
        let Some(g) = st.graph(&city.id) else {
            continue;
        };
        if let Some((name, d)) = g.nearest_road_name(h.lat, h.lon, analytics::PLACE_MAX_M) {
            h.place = Some(analytics::PlaceName {
                name: name.to_string(),
                distance_m: d.round(),
                source: "osm_road",
            });
        }
    }
}

async fn get_summary(
    State(st): State<Arc<AppState>>,
    q: Result<Query<HashMap<String, String>>, QueryRejection>,
) -> Result<Json<Value>, ApiError> {
    let Query(q) = q?;
    let layer = layer_param(&q)?;
    let city = city_param(&q, &st.config)?;
    let now = Utc::now();
    let summary = {
        let (st, city) = (Arc::clone(&st), city.cloned());
        blocking(move || {
            let views = city_views(&st, layer, city.as_ref(), now);
            let cfg = &st.config;
            let history_points = st.grid_of(layer).read().history_points();
            (
                analytics::summarise(views, &cfg.policy, &cfg.fusion, now),
                history_points,
            )
        })
        .await?
    };
    let (summary, history_points) = summary;
    let mut out = serde_json::to_value(&summary).expect("summary serialises");
    out["city"] = json!(city.map(|c| &c.id));
    out["layer"] = json!(layer);
    out["generated_at"] = json!(now);
    // Global, not per city: the cap applies to the whole map.
    out["history_points"] = json!({
        "total": history_points,
        "max": st.config.fusion.history_max_total,
    });
    Ok(Json(out))
}

async fn get_recession(
    State(st): State<Arc<AppState>>,
    q: Result<Query<HashMap<String, String>>, QueryRejection>,
) -> Result<Json<Value>, ApiError> {
    let Query(q) = q?;
    let layer = layer_param(&q)?;
    let raw = q
        .get("cell")
        .map(|s| s.trim())
        .ok_or_else(|| ApiError::bad_request("cell is required: cell=<H3 index in hex>"))?;
    let cell: h3o::CellIndex = raw
        .parse()
        .map_err(|_| ApiError::bad_request(format!("cell '{raw}' is not a valid H3 cell index")))?;
    let res = st.config.resolution;
    if cell.resolution() != res {
        return Err(ApiError::bad_request(format!(
            "cell {raw} is at H3 resolution {}; the engine's cells are at resolution {}",
            u8::from(cell.resolution()),
            u8::from(res)
        )));
    }
    let now = Utc::now();
    let (view, history) = {
        let g = st.grid_of(layer).read();
        match (g.view(cell, now), g.state(cell)) {
            (Some(v), Some(s)) => (v, s.history.iter().copied().collect::<Vec<_>>()),
            _ => {
                return Err(ApiError::not_found(format!(
                    "no reports have touched cell {raw}"
                )))
            }
        }
    };
    let (drain, note) = match analytics::drain_estimate(&history) {
        Ok(d) => (
            Some(d),
            format!(
                "A weighted straight-line fit to this cell's direct readings over the last {} hours. It describes the recent past, is not a forecast, and never opens a road.",
                analytics::DRAIN_WINDOW_H
            ),
        ),
        Err(reason) => (None, format!("No drain rate: {reason}.")),
    };
    let centre = h3o::LatLng::from(cell);
    let history: Vec<Value> = history
        .iter()
        .map(|p| {
            json!({
                "at": p.at,
                "reading_cm": r1(p.reading_cm),
                "reading_sigma_cm": r1(p.reading_sigma_cm),
                "fused_cm": r1(p.fused_cm),
                "fused_sigma_cm": r1(p.fused_sigma_cm),
                "migrated": p.migrated,
            })
        })
        .collect();
    Ok(Json(json!({
        "cell": cell.to_string(),
        "city": city_of_cell(&st.config, cell),
        "layer": layer,
        "lat": r6(centre.lat()),
        "lon": r6(centre.lng()),
        "advisory": true,
        "current": cell_properties(&view, &st.config),
        "history_capacity": st.config.fusion.history_len,
        "history": history,
        "drain": drain.map(|d| json!({
            "rate_cm_per_h": r2(d.rate_cm_per_h),
            "rate_sigma_cm_per_h": r2(d.rate_sigma_cm_per_h),
            "trend": d.trend,
            "points_used": d.points_used,
            "from": d.from,
            "to": d.to,
            "hours_to_15_cm": d.hours_to_15_cm.map(r1),
        })),
        "drain_note": note,
        "generated_at": now,
    })))
}

/// Columns of `/v1/export?format=csv`, in order.
pub const EXPORT_CSV_COLUMNS: [&str; 24] = [
    "cell",
    "city",
    "lat",
    "lon",
    "depth_cm",
    "depth_sigma_cm",
    "depth_upper_cm",
    "depth_sigma_meas_cm",
    "conflict_high_mean_cm",
    "velocity_mps",
    "velocity_sigma_mps",
    "velocity_assumed",
    "hazard_rating",
    "hazard_class",
    "hazard_class_upper",
    "debris_factor",
    "unknown",
    "expired",
    "n_readings",
    "n_neighbour",
    "conflicts",
    "updated_at",
    "last_depth_cm",
    "last_reading_age_h",
];

/// Quote a CSV field when it holds a comma, a quote or a line break.
fn csv_field(s: &str) -> String {
    if s.contains([',', '"', '\n', '\r']) {
        format!("\"{}\"", s.replace('"', "\"\""))
    } else {
        s.to_string()
    }
}

fn csv_row(v: &CellView, cfg: &Config) -> String {
    let p = serde_json::to_value(cell_properties(v, cfg)).expect("properties serialise");
    let c = h3o::LatLng::from(v.cell);
    EXPORT_CSV_COLUMNS
        .iter()
        .map(|col| match *col {
            "lat" => r6(c.lat()).to_string(),
            "lon" => r6(c.lng()).to_string(),
            key => match &p[key] {
                Value::Null => String::new(),
                Value::String(s) => csv_field(s),
                other => csv_field(&other.to_string()),
            },
        })
        .collect::<Vec<_>>()
        .join(",")
}

async fn get_export(
    State(st): State<Arc<AppState>>,
    q: Result<Query<HashMap<String, String>>, QueryRejection>,
) -> Result<Response, ApiError> {
    let Query(q) = q?;
    let cfg = &st.config;
    let layer = layer_param(&q)?;
    let city = required_city(&q, cfg)?;
    let format = q.get("format").map_or("geojson", |s| s.trim());
    if !matches!(format, "geojson" | "csv") {
        return Err(ApiError::bad_request(format!(
            "format must be geojson or csv, got '{format}'"
        )));
    }
    let now = Utc::now();
    let stamp = now.format("%Y%m%dT%H%M%SZ");
    let csv = format == "csv";
    let (body, truncated, total_cells) = {
        let (st, city) = (Arc::clone(&st), city.clone());
        blocking(move || {
            let cfg = &st.config;
            let mut views = city_views(&st, layer, Some(&city), now);
            let total_cells = views.len();
            let truncated = total_cells > cfg.max_cells;
            if truncated {
                // Keep the most important cells, not the lowest indexes.
                views = analytics::sort_by_score(views, &cfg.policy, &cfg.fusion, now);
                views.truncate(cfg.max_cells);
            }
            let body = if csv {
                let mut out = EXPORT_CSV_COLUMNS.join(",");
                out.push('\n');
                for v in &views {
                    out.push_str(&csv_row(v, cfg));
                    out.push('\n');
                }
                out
            } else {
                let features: Vec<Value> = views.iter().map(|v| cell_feature(v, cfg)).collect();
                json!({
                    "type": "FeatureCollection",
                    "city": city.id,
                    "features": features,
                    "truncated": truncated,
                    "total_cells": total_cells,
                    "generated_at": now,
                })
                .to_string()
            };
            (body, truncated, total_cells)
        })
        .await?
    };
    let (content_type, ext) = if csv {
        ("text/csv; charset=utf-8", "csv")
    } else {
        ("application/geo+json", "geojson")
    };
    let mut resp = body.into_response();
    let h = resp.headers_mut();
    h.insert(header::CONTENT_TYPE, HeaderValue::from_static(content_type));
    if let Ok(v) = HeaderValue::from_str(&format!(
        "attachment; filename=\"neermattam-{}-{stamp}.{ext}\"",
        city.id
    )) {
        h.insert(header::CONTENT_DISPOSITION, v);
    }
    h.insert(
        "x-neermattam-truncated",
        HeaderValue::from_static(if truncated { "true" } else { "false" }),
    );
    h.insert("x-neermattam-total-cells", HeaderValue::from(total_cells));
    Ok(resp)
}

async fn healthz() -> Json<Value> {
    Json(json!({ "status": "ok" }))
}

/// `/readyz`: 200 once the primary city's road graph is loaded. The body
/// lists every city's graph. Another city's graph that is still loading or
/// has failed is reported (`status: degraded`) but does not make the service
/// unready: its readings and map still work, and only its routes answer 503.
async fn readyz(State(st): State<Arc<AppState>>) -> Response {
    let cells = st.grid.read().len();
    let primary = st.config.primary_city.clone();
    let mut loading = Vec::new();
    let mut failed = Vec::new();
    let mut per_city = serde_json::Map::new();
    for c in &st.config.cities {
        let status = st.graph_status(&c.id);
        match &status {
            GraphStatus::Loading => loading.push(c.id.clone()),
            GraphStatus::Failed(e) => failed.push(format!("{}: {e}", c.id)),
            GraphStatus::Ready(_) => {}
        }
        per_city.insert(c.id.clone(), graph_json(&status));
    }
    let primary_status = st.graph_status(&primary);
    let mut reasons = Vec::new();
    if !loading.is_empty() {
        reasons.push(format!("road graphs still loading: {}", loading.join(", ")));
    }
    if !failed.is_empty() {
        reasons.push(format!(
            "road graphs failed, so routes there answer 503: {}",
            failed.join("; ")
        ));
    }
    let (code, status) = match primary_status {
        GraphStatus::Ready(_) if reasons.is_empty() => (StatusCode::OK, "ready"),
        GraphStatus::Ready(_) => (StatusCode::OK, "degraded"),
        GraphStatus::Loading | GraphStatus::Failed(_) => {
            (StatusCode::SERVICE_UNAVAILABLE, "not_ready")
        }
    };
    let mut body = json!({
        "status": status,
        "primary_city": primary,
        "cities": per_city,
        "cells": cells,
    });
    if !reasons.is_empty() {
        body["reason"] = json!(reasons.join(". "));
    }
    (code, Json(body)).into_response()
}

async fn method_not_allowed() -> ApiError {
    ApiError {
        status: StatusCode::METHOD_NOT_ALLOWED,
        message: "method not allowed on this endpoint; see docs/CONTRACTS.md section 2".into(),
    }
}

async fn not_found() -> ApiError {
    ApiError {
        status: StatusCode::NOT_FOUND,
        message: "no such endpoint; see docs/CONTRACTS.md section 2".into(),
    }
}

/// The full router with middleware.
pub fn router(state: Arc<AppState>) -> Router {
    let cfg = &state.config;
    let timed = Router::new()
        .route("/v1/readings", post(post_reading))
        .route("/v1/cells", get(get_cells))
        .route("/v1/route", get(get_route))
        .route("/v1/cities", get(get_cities))
        .route("/v1/hotspots", get(get_hotspots))
        .route("/v1/summary", get(get_summary))
        .route("/v1/recession", get(get_recession))
        .route("/v1/export", get(get_export))
        .route("/healthz", get(healthz))
        .route("/readyz", get(readyz))
        .merge(crate::sites_api::routes())
        .layer(TimeoutLayer::with_status_code(
            StatusCode::SERVICE_UNAVAILABLE,
            cfg.request_timeout,
        ));
    Router::new()
        .merge(timed)
        .route("/v1/stream", get(stream))
        .method_not_allowed_fallback(method_not_allowed)
        .fallback(not_found)
        .layer(DefaultBodyLimit::max(cfg.body_limit_bytes))
        .layer(
            TraceLayer::new_for_http()
                .make_span_with(DefaultMakeSpan::new().level(Level::INFO))
                .on_response(DefaultOnResponse::new().level(Level::INFO)),
        )
        .with_state(state)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn reading() -> ReadingIn {
        ReadingIn {
            reading_id: Some("r_1".into()),
            lat: 12.9791,
            lon: 80.2209,
            depth_cm: 23.4,
            depth_sigma_cm: 3.1,
            velocity_mps: Some(0.4),
            velocity_sigma_mps: Some(0.2),
            observed_at: Utc::now(),
            source: Some("photo".into()),
            trust: Some(0.9),
        }
    }

    #[test]
    fn validation_rules() {
        let cfg = Config::default();
        let now = Utc::now();
        assert!(validate_reading(&reading(), &cfg, now).is_ok());
        let check = |f: &dyn Fn(&mut ReadingIn), needle: &str| {
            let mut r = reading();
            f(&mut r);
            let e = validate_reading(&r, &cfg, now).unwrap_err();
            assert!(e.contains(needle), "{e}");
        };
        check(&|r| r.lat = 13.5, "outside the service area");
        check(&|r| r.lon = 80.0, "outside the service area");
        check(&|r| r.depth_cm = 501.0, "depth_cm");
        check(&|r| r.depth_cm = -0.1, "depth_cm");
        check(&|r| r.depth_sigma_cm = 0.0, "depth_sigma_cm");
        check(
            &|r| r.velocity_sigma_mps = None,
            "velocity_sigma_mps is required",
        );
        check(&|r| r.velocity_mps = Some(11.0), "velocity_mps");
        check(
            &|r| r.observed_at = now + chrono::Duration::hours(25),
            "observed_at",
        );
        check(
            &|r| r.observed_at = now - chrono::Duration::hours(25),
            "observed_at",
        );
        check(&|r| r.source = Some("rumour".into()), "source");
        check(&|r| r.trust = Some(0.0), "trust");
        check(&|r| r.reading_id = Some(String::new()), "reading_id");
        let mut r = reading();
        r.velocity_mps = None;
        r.velocity_sigma_mps = Some(0.3);
        assert!(validate_reading(&r, &cfg, now)
            .unwrap()
            .velocity_sigma_mps
            .is_none());
    }

    #[test]
    fn recent_ids_are_bounded() {
        let c = |lat: f64| {
            h3o::LatLng::new(lat, 80.2)
                .unwrap()
                .to_cell(h3o::Resolution::Ten)
        };
        let mut ids = RecentIds::new(2);
        ids.insert("a", c(13.0));
        ids.insert("a", c(13.1));
        assert_eq!(ids.get("a"), Some(c(13.0)), "first cell is kept");
        ids.insert("b", c(13.0));
        ids.insert("c", c(13.0));
        assert_eq!(ids.get("a"), None, "oldest id should have been evicted");
        assert_eq!(ids.get("c"), Some(c(13.0)));
    }

    #[test]
    fn future_observations_are_refused_or_clamped() {
        // Regression: observed_at up to 24 hours ahead was accepted, and one
        // future-dated dry report then hid real deep water for that long.
        let cfg = Config::default();
        let now = Utc::now();
        let mut r = reading();
        r.observed_at = now + chrono::Duration::hours(2);
        let e = validate_reading(&r, &cfg, now).unwrap_err();
        assert!(e.contains("in the future"), "{e}");
        r.observed_at = now + chrono::Duration::seconds(MAX_FUTURE_SKEW_SECS + 1);
        assert!(validate_reading(&r, &cfg, now).is_err());
        r.observed_at = now + chrono::Duration::seconds(120);
        assert_eq!(validate_reading(&r, &cfg, now).unwrap().observed_at, now);
        r.observed_at = now - chrono::Duration::hours(3);
        assert_eq!(
            validate_reading(&r, &cfg, now).unwrap().observed_at,
            r.observed_at
        );
    }
}
