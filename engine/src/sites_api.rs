//! HTTP endpoints for critical sites and the risk ledger, as specified in
//! `docs/CONTRACTS.md` section 2, "Critical sites".
//!
//! - `GET /v1/sites?city=&kind=&owner=`: sites with their watch zones.
//! - `GET /v1/sites/{id}?owner=&access=`: one site, its access routes and
//!   its area's risk record.
//! - `POST /v1/sites`, `DELETE /v1/sites/{id}?owner=`: operator sites.
//! - `GET /v1/siting?city=&bucket=`: the risk ledger, ranked by area.
//!
//! The engine has no authentication; the gateway authenticates operators and
//! passes the API key id as `owner`.

use crate::api::{blocking, required_city, ApiError, AppState, GraphStatus};
use crate::ledger::{area_out, rank, AreaOut, WET_CM};
use crate::persist;
use crate::sites::{
    access_routes, sibling, site_view, sort_views, SiteError, SiteKind, SiteView, ZoneParams,
};
use axum::extract::rejection::{JsonRejection, QueryRejection};
use axum::extract::{Path, Query, State};
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use axum::routing::get;
use axum::{Json, Router};
use chrono::Utc;
use h3o::CellIndex;
use serde::Deserialize;
use serde_json::{json, Value};
use std::collections::HashMap;
use std::sync::atomic::Ordering;
use std::sync::Arc;
use tracing::{info, warn};

/// Default and largest `limit` for the site list and the siting view.
const LIST_DEFAULT: usize = 100;
const LIST_MAX: usize = 500;

/// Read `rings`, `threshold_cm` and `min_coverage`.
fn zone_params(q: &HashMap<String, String>) -> Result<ZoneParams, ApiError> {
    let mut zp = ZoneParams::default();
    if let Some(raw) = q.get("rings") {
        let rings: Option<Vec<f64>> = raw
            .split(',')
            .map(|s| s.trim().parse::<f64>().ok())
            .collect();
        let mut rings = rings
            .filter(|r| (1..=3).contains(&r.len()))
            .filter(|r| r.iter().all(|x| x.is_finite() && (100.0..=3000.0).contains(x)))
            .ok_or_else(|| {
                ApiError::bad_request(format!(
                    "rings must be 1 to 3 radii in metres from 100 to 3000, comma-separated, got '{raw}'"
                ))
            })?;
        rings.sort_by(f64::total_cmp);
        rings.dedup();
        zp.rings_m = rings;
    }
    if let Some(raw) = q.get("threshold_cm") {
        zp.threshold_cm = raw
            .trim()
            .parse::<f64>()
            .ok()
            .filter(|x| x.is_finite() && (1.0..=200.0).contains(x))
            .ok_or_else(|| {
                ApiError::bad_request(format!(
                    "threshold_cm must be a number from 1 to 200, got '{raw}'"
                ))
            })?;
    }
    if let Some(raw) = q.get("min_coverage") {
        zp.min_coverage = raw
            .trim()
            .parse::<f64>()
            .ok()
            .filter(|x| x.is_finite() && (0.0..=1.0).contains(x))
            .ok_or_else(|| {
                ApiError::bad_request(format!(
                    "min_coverage must be a number from 0 to 1, got '{raw}'"
                ))
            })?;
    }
    Ok(zp)
}

fn kind_param(q: &HashMap<String, String>) -> Result<Option<SiteKind>, ApiError> {
    q.get("kind")
        .map(|k| k.trim().parse().map_err(ApiError::bad_request))
        .transpose()
}

fn owner_param(q: &HashMap<String, String>) -> Result<Option<String>, ApiError> {
    match q.get("owner").map(|s| s.trim()) {
        None => Ok(None),
        Some(o) if crate::sites::valid_owner(o) => Ok(Some(o.to_string())),
        Some(_) => Err(ApiError::bad_request(
            "owner must be 1 to 64 characters of A-Z, a-z, 0-9, _ and -",
        )),
    }
}

fn bounded(
    q: &HashMap<String, String>,
    key: &str,
    default: usize,
    max: usize,
) -> Result<usize, ApiError> {
    match q.get(key) {
        None => Ok(default),
        Some(raw) => raw
            .trim()
            .parse::<usize>()
            .ok()
            .filter(|n| *n <= max && (key == "offset" || *n >= 1))
            .ok_or_else(|| {
                ApiError::bad_request(format!(
                    "{key} must be a whole number up to {max}, got '{raw}'"
                ))
            }),
    }
}

async fn list_sites(
    State(st): State<Arc<AppState>>,
    q: Result<Query<HashMap<String, String>>, QueryRejection>,
) -> Result<Json<Value>, ApiError> {
    let Query(q) = q?;
    let city = required_city(&q, &st.config)?.clone();
    let kind = kind_param(&q)?;
    let owner = owner_param(&q)?;
    let zp = zone_params(&q)?;
    let limit = bounded(&q, "limit", LIST_DEFAULT, LIST_MAX)?;
    let offset = bounded(&q, "offset", 0, 1_000_000)?;
    let now = Utc::now();
    let body = blocking(move || {
        let reg = &st.sites.registry;
        let sites = reg.list(&city.id, kind, owner.as_deref());
        let total = sites.len();
        let mut views: Vec<SiteView> = {
            let g = st.grid.read();
            sites
                .into_iter()
                .map(|s| site_view(&g, s, now, &st.config.policy, &zp))
                .collect()
        };
        sort_views(&mut views);
        let page: Vec<SiteView> = views.into_iter().skip(offset).take(limit).collect();
        let counts: serde_json::Map<String, Value> = reg
            .counts(&city.id, owner.as_deref())
            .into_iter()
            .map(|(k, (o, op))| (k.to_string(), json!({ "osm": o, "operator": op })))
            .collect();
        json!({
            "city": city.id,
            "kind": kind,
            "threshold_cm": zp.threshold_cm,
            "rings_m": zp.rings_m,
            "min_coverage": zp.min_coverage,
            "total": total,
            "count": page.len(),
            "offset": offset,
            "counts": counts,
            "osm": reg.info,
            "advisory": true,
            "generated_at": now,
            "sites": page,
        })
    })
    .await?;
    Ok(Json(body))
}

/// The risk ledger for an area and the six around it.
fn site_risk(st: &AppState, cell: CellIndex) -> Value {
    let l = st.sites.ledger.lock();
    let b = l.bucket(true);
    let open = b.open_by_area();
    let areas: Vec<AreaOut> = match l.area_of(cell) {
        None => Vec::new(),
        Some(home) => home
            .grid_disk::<Vec<_>>(1)
            .into_iter()
            .filter_map(|a| {
                b.area(u64::from(a))
                    .map(|r| area_out(a, r, open.get(&u64::from(a)).copied().unwrap_or(0)))
            })
            .collect(),
    };
    json!({
        "area_resolution": l.resolution,
        "area": l.area_of(cell).map(|a| a.to_string()),
        "since": l.since,
        "wet_threshold_cm": WET_CM,
        "areas": areas,
        "note": ledger_note(&l.since),
    })
}

fn ledger_note(since: &chrono::DateTime<Utc>) -> String {
    format!(
        "This record holds only what people have reported since {}. It builds up over a monsoon; an area with no reports is unknown, not dry.",
        since.format("%Y-%m-%d")
    )
}

async fn get_site(
    State(st): State<Arc<AppState>>,
    Path(id): Path<String>,
    q: Result<Query<HashMap<String, String>>, QueryRejection>,
) -> Result<Json<Value>, ApiError> {
    let Query(q) = q?;
    let owner = owner_param(&q)?;
    let zp = zone_params(&q)?;
    let with_access = !matches!(q.get("access").map(String::as_str), Some("false" | "0"));
    let site = st
        .sites
        .registry
        .get(&id, owner.as_deref())
        .ok_or_else(|| ApiError::not_found(format!("no site '{id}'")))?;
    let now = Utc::now();
    let body = blocking(move || {
        let (view, water) = {
            let g = st.grid.read();
            let view = site_view(&g, site.clone(), now, &st.config.policy, &zp);
            (view, with_access.then(|| g.water_map(now)))
        };
        let mut out = serde_json::to_value(&view).expect("site serialises");
        if let Some(water) = water {
            let graph = match st.graph_status(&site.city) {
                GraphStatus::Ready(_) => st.graph(&site.city).ok_or("routing_loading"),
                GraphStatus::Loading => Err("routing_loading"),
                GraphStatus::Failed(_) => Err("routing_failed"),
            };
            let started = std::time::Instant::now();
            let routes = access_routes(
                graph.as_deref().map_err(|e| *e),
                &water,
                &site,
                st.sites.registry.fuel_in(&site.city),
                &st.config.policy,
            );
            info!(
                site = %site.id,
                elapsed_ms = started.elapsed().as_millis() as u64,
                "access routes planned"
            );
            out["access"] = json!({ "advisory": true, "computed_at": now, "routes": routes });
        }
        if let Ok(cell) = view.cell.parse::<CellIndex>() {
            out["risk"] = site_risk(&st, cell);
        }
        out["threshold_cm"] = json!(zp.threshold_cm);
        out["min_coverage"] = json!(zp.min_coverage);
        out["osm_attribution"] = json!(st.sites.registry.info.attribution);
        out["advisory"] = json!(true);
        out["generated_at"] = json!(now);
        out
    })
    .await?;
    Ok(Json(body))
}

/// Body of `POST /v1/sites`.
#[derive(Debug, Deserialize)]
pub struct SiteIn {
    owner: String,
    kind: String,
    name: String,
    lat: f64,
    lon: f64,
    operator: Option<String>,
}

fn site_error(e: SiteError) -> ApiError {
    match e {
        SiteError::Invalid(m) => ApiError::bad_request(m),
        SiteError::Full(m) => ApiError::new(StatusCode::CONFLICT, m),
        SiteError::NotFound => ApiError::not_found("no such site for this owner"),
        SiteError::NotOperator => ApiError::bad_request(e.to_string()),
    }
}

/// Write the operator sites now, if persistence is on.
fn save_operator_sites(st: &AppState) {
    if let Some(path) = &st.config.snapshot_path {
        let p = sibling(path, "sites");
        if let Err(e) = persist::write_atomic(&p, &st.sites.registry.encode_operator()) {
            warn!(error = %e, "operator sites not saved");
        }
    }
}

async fn post_site(
    State(st): State<Arc<AppState>>,
    body: Result<Json<SiteIn>, JsonRejection>,
) -> Result<Response, ApiError> {
    let Json(b) = body?;
    let kind: SiteKind = b.kind.trim().parse().map_err(ApiError::bad_request)?;
    let site = st
        .sites
        .registry
        .add(
            &st.config,
            b.owner.trim(),
            kind,
            &b.name,
            b.operator.as_deref(),
            b.lat,
            b.lon,
            Utc::now(),
        )
        .map_err(site_error)?;
    info!(site = %site.id, owner = %b.owner, city = %site.city, kind = %site.kind, "operator site added");
    let st2 = Arc::clone(&st);
    blocking(move || save_operator_sites(&st2)).await?;
    Ok((StatusCode::CREATED, Json(site)).into_response())
}

async fn delete_site(
    State(st): State<Arc<AppState>>,
    Path(id): Path<String>,
    q: Result<Query<HashMap<String, String>>, QueryRejection>,
) -> Result<StatusCode, ApiError> {
    let Query(q) = q?;
    let owner = owner_param(&q)?.ok_or_else(|| ApiError::bad_request("owner is required"))?;
    st.sites.registry.remove(&id, &owner).map_err(site_error)?;
    info!(site = %id, owner = %owner, "operator site removed");
    let st2 = Arc::clone(&st);
    blocking(move || save_operator_sites(&st2)).await?;
    Ok(StatusCode::NO_CONTENT)
}

async fn get_siting(
    State(st): State<Arc<AppState>>,
    q: Result<Query<HashMap<String, String>>, QueryRejection>,
) -> Result<Json<Value>, ApiError> {
    let Query(q) = q?;
    let city = required_city(&q, &st.config)?.clone();
    let reported = match q.get("bucket").map(|s| s.trim()) {
        None | Some("reported") => true,
        Some("unattributed") => false,
        Some(other) => {
            return Err(ApiError::bad_request(format!(
                "bucket must be reported or unattributed, got '{other}'"
            )))
        }
    };
    let limit = bounded(&q, "limit", LIST_DEFAULT, LIST_MAX)?;
    let now = Utc::now();
    let body = blocking(move || {
        let l = st.sites.ledger.lock();
        let b = l.bucket(reported);
        let open = b.open_by_area();
        let mut areas: Vec<AreaOut> = b
            .areas()
            .filter_map(|(raw, r)| {
                let a = CellIndex::try_from(*raw).ok()?;
                let c = h3o::LatLng::from(a);
                city.contains(c.lat(), c.lng())
                    .then(|| area_out(a, r, open.get(raw).copied().unwrap_or(0)))
            })
            .collect();
        let total = areas.len();
        rank(&mut areas);
        areas.truncate(limit);
        json!({
            "city": city.id,
            "bucket": if reported { "reported" } else { "unattributed" },
            "since": l.since,
            "history_days": ((now - l.since).num_minutes() as f64 / 1440.0 * 10.0).round() / 10.0,
            "area_resolution": l.resolution,
            "wet_threshold_cm": WET_CM,
            "total": total,
            "count": areas.len(),
            "advisory": true,
            "note": ledger_note(&l.since),
            "generated_at": now,
            "areas": areas,
        })
    })
    .await?;
    Ok(Json(body))
}

/// The site routes, merged into the engine's router.
pub fn routes() -> Router<Arc<AppState>> {
    Router::new()
        .route("/v1/sites", get(list_sites).post(post_site))
        .route("/v1/sites/{id}", get(get_site).delete(delete_site))
        .route("/v1/siting", get(get_siting))
}

/// Load the ledger and the operator sites saved next to the snapshot. A file
/// that cannot be read is moved aside; nothing else is lost.
pub fn load_state(st: &AppState) {
    let Some(path) = &st.config.snapshot_path else {
        return;
    };
    let aside = |p: &std::path::Path| {
        let to = p.with_extension(format!("rejected-{}.json", Utc::now().timestamp()));
        let _ = std::fs::rename(p, &to);
        to
    };
    let lp = sibling(path, "ledger");
    match std::fs::read(&lp) {
        Ok(bytes) => {
            match crate::ledger::Ledger::decode(&bytes, st.config.sites.ledger_resolution) {
                Ok(l) => {
                    info!(areas = l.reported.len(), since = %l.since, "risk ledger loaded");
                    *st.sites.ledger.lock() = l;
                }
                Err(e) => {
                    let to = aside(&lp);
                    warn!(error = %e, moved_to = %to.display(), "risk ledger not loaded; starting a new one");
                }
            }
        }
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
        Err(e) => warn!(error = %e, "risk ledger not read"),
    }
    let sp = sibling(path, "sites");
    match std::fs::read(&sp) {
        Ok(bytes) => match st.sites.registry.decode_operator(&bytes, &st.config) {
            Ok(n) => info!(sites = n, "operator sites loaded"),
            Err(e) => {
                let to = aside(&sp);
                warn!(error = %e, moved_to = %to.display(), "operator sites not loaded");
            }
        },
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
        Err(e) => warn!(error = %e, "operator sites not read"),
    }
}

/// Close quiet wet spells and write the ledger if it changed.
pub fn save_ledger(st: &AppState) -> Result<bool, persist::PersistError> {
    let Some(path) = &st.config.snapshot_path else {
        return Ok(false);
    };
    let bytes = {
        let mut l = st.sites.ledger.lock();
        if l.maintain(Utc::now()) > 0 {
            st.sites.ledger_dirty.store(true, Ordering::SeqCst);
        }
        if !st.sites.ledger_dirty.swap(false, Ordering::SeqCst) {
            return Ok(false);
        }
        l.encode()
    };
    persist::write_atomic(&sibling(path, "ledger"), &bytes).inspect_err(|_| {
        st.sites.ledger_dirty.store(true, Ordering::SeqCst);
    })?;
    Ok(true)
}
