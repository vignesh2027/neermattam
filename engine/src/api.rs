//! The HTTP API, as specified in `docs/CONTRACTS.md` section 2.
//!
//! - `POST /v1/readings`: fuse one depth reading.
//! - `GET /v1/cells?bbox=minLon,minLat,maxLon,maxLat`: GeoJSON hex cells.
//! - `GET /v1/route?from=lat,lon&to=lat,lon&vehicle=...`: water-aware route
//!   as a GeoJSON LineString Feature, with the baseline route in a `baseline`
//!   member alongside.
//! - `GET /v1/stream`: server-sent events, one `cell` event per changed cell.
//! - `GET /healthz`, `GET /readyz`.
//!
//! Every error is JSON, `{"error": "..."}`, with status 400 for invalid
//! input, 404 for an unknown path or when no road connects the points, 413
//! for an oversized body and 503 while the road graph is loading.

use crate::config::Config;
use crate::fusion::{CellView, Grid, Observation};
use crate::graph::RoadGraph;
use crate::hazard::{assess, HazardClass};
use crate::persist;
use crate::routing::{blocking_estimate, plan_route, RouteError, RouteKind, RouteSummary};
use crate::vehicle::{CrossingLimit, Vehicle};
use axum::extract::rejection::{JsonRejection, QueryRejection};
use axum::extract::{DefaultBodyLimit, Query, State};
use axum::http::StatusCode;
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

/// Shared server state.
pub struct AppState {
    /// Configuration.
    pub config: Config,
    /// The live cell map.
    pub grid: RwLock<Grid>,
    graph: OnceLock<Arc<RoadGraph>>,
    graph_error: Mutex<Option<String>>,
    events: broadcast::Sender<Arc<str>>,
    dirty: AtomicBool,
    recent_ids: Mutex<RecentIds>,
    shutdown_tx: watch::Sender<bool>,
}

impl AppState {
    /// Fresh state with an empty map and no graph yet.
    #[must_use]
    pub fn new(config: Config) -> Arc<Self> {
        let grid = Grid::new(config.fusion, config.resolution);
        let (events, _) = broadcast::channel(EVENT_BUFFER);
        let (shutdown_tx, _) = watch::channel(false);
        Arc::new(AppState {
            recent_ids: Mutex::new(RecentIds::new(config.dedupe_capacity)),
            config,
            grid: RwLock::new(grid),
            graph: OnceLock::new(),
            graph_error: Mutex::new(None),
            events,
            dirty: AtomicBool::new(false),
            shutdown_tx,
        })
    }

    /// Install the loaded road graph. Later calls are ignored.
    pub fn set_graph(&self, g: RoadGraph) {
        let _ = self.graph.set(Arc::new(g));
    }

    /// Record why the graph failed to load (reported by `/readyz`).
    pub fn set_graph_error(&self, e: String) {
        *self.graph_error.lock() = Some(e);
    }

    /// The road graph, once loaded.
    #[must_use]
    pub fn graph(&self) -> Option<Arc<RoadGraph>> {
        self.graph.get().cloned()
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

    /// Run until shutdown: every snapshot interval, drop forgotten cells and
    /// save the map if it changed. Saves once more on the way out.
    pub async fn snapshot_loop(self: Arc<Self>) {
        let mut tick = tokio::time::interval(self.config.snapshot_interval);
        tick.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
        let mut stop = self.shutdown_tx.subscribe();
        loop {
            tokio::select! {
                _ = tick.tick() => {}
                _ = stop.wait_for(|v| *v) => break,
            }
            let forgotten = self.grid.write().forget_old(Utc::now());
            if forgotten > 0 {
                info!(
                    forgotten,
                    "dropped cells with no evidence for the forget window"
                );
                self.dirty.store(true, Ordering::SeqCst);
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
    fn bad_request(message: impl Into<String>) -> Self {
        ApiError {
            status: StatusCode::BAD_REQUEST,
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
    if !(r.lat.is_finite() && r.lon.is_finite()) || !cfg.service_bbox.contains(r.lat, r.lon) {
        return Err(format!(
            "lat/lon ({}, {}) is outside the service area {} (minLon,minLat,maxLon,maxLat)",
            r.lat, r.lon, cfg.service_bbox
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
    cell: String,
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
    cell: h3o::CellIndex,
    id: Option<String>,
    duplicate: bool,
    now: DateTime<Utc>,
) -> Result<ReadingOut, ApiError> {
    let view = st.grid.read().view(cell, now).ok_or_else(|| ApiError {
        status: StatusCode::INTERNAL_SERVER_ERROR,
        message: "cell state missing after update".into(),
    })?;
    let p = cell_properties(&view, &st.config);
    let pol = &st.config.policy;
    Ok(ReadingOut {
        reading_id: id,
        cell: p.cell,
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
    body: Result<Json<ReadingIn>, JsonRejection>,
) -> Result<Json<ReadingOut>, ApiError> {
    let Json(r) = body?;
    let now = Utc::now();
    let obs = validate_reading(&r, &st.config, now).map_err(ApiError::bad_request)?;

    // The id check, the fusion and the id record happen under one lock, so a
    // retry that races the original waits for it and is then answered from
    // the cell the original was fused into. Lock order: ids, then grid.
    let ingested = {
        let mut ids = st.recent_ids.lock();
        if let Some((id, cell)) = r
            .reading_id
            .as_deref()
            .and_then(|id| ids.get(id).map(|c| (id, c)))
        {
            drop(ids);
            info!(reading_id = %id, cell = %cell, "duplicate reading ignored");
            return reading_out(&st, cell, r.reading_id.clone(), true, now)
                .map_err(|_| ApiError {
                    status: StatusCode::CONFLICT,
                    message: format!(
                        "reading_id '{id}' was already fused, and its cell has since been forgotten"
                    ),
                })
                .map(Json);
        }
        let ingested = st
            .grid
            .write()
            .ingest(&obs)
            .map_err(|e| ApiError::bad_request(e.to_string()))?;
        if let Some(id) = &r.reading_id {
            ids.insert(id, ingested.cell);
        }
        ingested
    };
    st.dirty.store(true, Ordering::SeqCst);

    // Publish every changed cell. The events are sent while the read lock is
    // held, so no later write can slip in between building a cell's view and
    // sending it: a subscriber never receives an older state after a newer one.
    {
        let g = st.grid.read();
        for v in ingested.touched.iter().filter_map(|c| g.view(*c, now)) {
            // No subscribers is fine.
            let _ = st
                .events
                .send(Arc::from(cell_feature(&v, &st.config).to_string()));
        }
    }

    let out = reading_out(&st, ingested.cell, r.reading_id.clone(), false, now)?;
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

async fn get_cells(
    State(st): State<Arc<AppState>>,
    q: Result<Query<HashMap<String, String>>, QueryRejection>,
) -> Result<Json<Value>, ApiError> {
    let Query(q) = q?;
    let raw = q.get("bbox").ok_or_else(|| {
        ApiError::bad_request("bbox is required: bbox=minLon,minLat,maxLon,maxLat")
    })?;
    let bbox: crate::geo::BBox = raw.parse().map_err(ApiError::bad_request)?;
    if bbox.area_deg2() > 1.0 {
        return Err(ApiError::bad_request(
            "bbox is too large: at most 1 square degree per request",
        ));
    }
    let now = Utc::now();
    let mut views: Vec<CellView> = {
        let g = st.grid.read();
        g.views(now)
            .filter(|v| {
                let c = h3o::LatLng::from(v.cell);
                bbox.contains(c.lat(), c.lng())
            })
            .collect()
    };
    views.sort_unstable_by_key(|v| u64::from(v.cell));
    let truncated = views.len() > st.config.max_cells;
    views.truncate(st.config.max_cells);
    let features: Vec<Value> = views.iter().map(|v| cell_feature(v, &st.config)).collect();
    Ok(Json(json!({
        "type": "FeatureCollection",
        "features": features,
        "truncated": truncated,
        "generated_at": now,
    })))
}

fn parse_point(
    q: &HashMap<String, String>,
    key: &str,
    cfg: &Config,
) -> Result<(f64, f64), ApiError> {
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
    if !cfg.service_bbox.contains(lat, lon) {
        return Err(ApiError::bad_request(format!(
            "{key} ({lat}, {lon}) is outside the service area {} (minLon,minLat,maxLon,maxLat); note the order is lat,lon",
            cfg.service_bbox
        )));
    }
    Ok((lat, lon))
}

fn route_feature(s: &RouteSummary, vehicle: Vehicle, cfg: &Config, now: DateTime<Utc>) -> Value {
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
    let cfg = &st.config;
    let from = parse_point(&q, "from", cfg)?;
    let to = parse_point(&q, "to", cfg)?;
    let vehicle: Vehicle = q
        .get("vehicle")
        .ok_or_else(|| {
            ApiError::bad_request("vehicle is required: pedestrian, two_wheeler, auto, car or bus")
        })?
        .parse()
        .map_err(ApiError::bad_request)?;
    let graph = st.graph().ok_or_else(|| ApiError {
        status: StatusCode::SERVICE_UNAVAILABLE,
        message: "the road graph is still loading; try again shortly".into(),
    })?;
    let now = Utc::now();
    let water = st.grid.read().water_map(now);
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
        kind = ?result.route.kind,
        distance_m = result.route.distance_m.round(),
        baseline_m = result.baseline.distance_m.round(),
        cells_avoided = result.route.cells_avoided,
        elapsed_us = started.elapsed().as_micros() as u64,
        "route planned"
    );
    let mut feature = route_feature(&result.route, vehicle, cfg, now);
    feature["baseline"] = route_feature(&result.baseline, vehicle, cfg, now);
    debug_assert!(result.route.kind != RouteKind::Baseline);
    Ok(Json(feature))
}

async fn stream(
    State(st): State<Arc<AppState>>,
) -> Sse<impl Stream<Item = Result<Event, Infallible>>> {
    let rx = st.events.subscribe();
    let mut stop = st.shutdown_tx.subscribe();
    let stopped = async move {
        let _ = stop.wait_for(|v| *v).await;
    };
    let events = BroadcastStream::new(rx)
        .map(|item| {
            Ok(match item {
                Ok(data) => Event::default().event("cell").data(data.as_ref()),
                Err(BroadcastStreamRecvError::Lagged(n)) => Event::default()
                    .comment(format!("lagged: {n} events dropped, refetch /v1/cells")),
            })
        })
        .take_until(stopped);
    Sse::new(events).keep_alive(KeepAlive::default())
}

async fn healthz() -> Json<Value> {
    Json(json!({ "status": "ok" }))
}

async fn readyz(State(st): State<Arc<AppState>>) -> Response {
    let cells = st.grid.read().len();
    if let Some(g) = st.graph() {
        return Json(json!({ "status": "ready", "graph": g.stats(), "cells": cells }))
            .into_response();
    }
    let reason = st
        .graph_error
        .lock()
        .clone()
        .unwrap_or_else(|| "road graph is still loading".into());
    (
        StatusCode::SERVICE_UNAVAILABLE,
        Json(json!({ "status": "not_ready", "reason": reason, "cells": cells })),
    )
        .into_response()
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
        .route("/healthz", get(healthz))
        .route("/readyz", get(readyz))
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
