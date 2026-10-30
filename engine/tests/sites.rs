//! HTTP tests for critical sites: watch zones, access routes on a flooded
//! synthetic street grid, the tanker's limit, operator sites and the risk
//! ledger. They use a small site file of their own, so they always run.

use axum::body::Body;
use axum::http::{Request, StatusCode};
use axum::Router;
use chrono::{Duration, Utc};
use h3o::Resolution;
use http_body_util::BodyExt;
use neermattam_engine::api::{router, AppState};
use neermattam_engine::config::Config;
use neermattam_engine::graph::{GraphBuilder, RoadGraph};
use serde_json::{json, Value};
use std::sync::Arc;
use tower::ServiceExt;

const LAT0: f64 = 12.97;
const LON0: f64 = 80.21;
const STEP: f64 = 0.0009; // about 100 m

/// A 3 by 3 block street grid with its south-west corner at the site. Every
/// vehicle may use every street. A short main road leaves the north-west
/// corner westwards, so the nearest main road is about 1.2 km north of the
/// site and there is only one stretch of it to reach.
fn town() -> RoadGraph {
    let mut b = GraphBuilder::new();
    let mut id = std::collections::HashMap::new();
    for i in 0..=12 {
        for j in 0..=12 {
            if i % 4 == 0 || j % 4 == 0 {
                id.insert(
                    (i, j),
                    b.add_node(LAT0 + STEP * f64::from(i), LON0 + STEP * f64::from(j)),
                );
            }
        }
    }
    let all = 0b11_1111;
    for i in (0..=12).step_by(4) {
        b.add_way((0..=12).map(|j| id[&(i, j)]).collect(), all, all);
        b.add_way((0..=12).map(|j| id[&(j, i)]).collect(), all, all);
    }
    let w1 = b.add_node(LAT0 + STEP * 12.0, LON0 - STEP);
    let w2 = b.add_node(LAT0 + STEP * 12.0, LON0 - 2.0 * STEP);
    b.add_way_class(vec![id[&(12, 0)], w1, w2], all, all, true);
    b.build(Resolution::Ten)
}

fn at(i: f64, j: f64) -> (f64, f64) {
    (LAT0 + STEP * i, LON0 + STEP * j)
}

/// One data centre at the south-west corner and one fuel station at the
/// north-east corner, in the format `scripts/extract_sites.py` writes.
fn site_file(dir: &std::path::Path) -> std::path::PathBuf {
    let (flat, flon) = at(12.0, 12.0);
    let doc = json!({
        "format": "neermattam-sites-osm", "version": 1,
        "attribution": "(c) OpenStreetMap contributors, ODbL 1.0",
        "extracts": {"southern": {"file": "test", "osm_data_until": "2026-10-25T21:21:21Z"}},
        "counts": {},
        "sites": [
            {"id": "osm:way:1", "kind": "data_centre", "name": "Test DC", "operator": "Acme",
             "lat": LAT0, "lon": LON0, "city": "chennai", "osm": "way/1", "tag": "telecom=data_center"},
            {"id": "osm:node:2", "kind": "hospital", "name": "Far Hospital",
             "lat": 13.2, "lon": 80.25, "city": "chennai", "osm": "node/2", "tag": "amenity=hospital"}
        ],
        "fuel": [
            {"id": "osm:node:9", "name": "Test Fuel", "lat": flat, "lon": flon, "city": "chennai", "diesel": true}
        ]
    });
    let p = dir.join("sites-osm.json");
    std::fs::write(&p, doc.to_string()).unwrap();
    p
}

struct Env {
    app: Router,
    state: Arc<AppState>,
    _dir: tempfile::TempDir,
}

fn env(persist: bool) -> Env {
    let dir = tempfile::tempdir().unwrap();
    let mut cfg = Config::default();
    cfg.cities.retain(|c| c.id == "chennai");
    cfg.snapshot_path = persist.then(|| dir.path().join("cells.json"));
    cfg.sites.osm_path = Some(site_file(dir.path()));
    let state = AppState::new(cfg);
    assert!(state.sites.registry.load_error.is_none());
    state.set_graph("chennai", town());
    Env {
        app: router(Arc::clone(&state)),
        state,
        _dir: dir,
    }
}

async fn send(app: &Router, req: Request<Body>) -> (StatusCode, Value) {
    let resp = app.clone().oneshot(req).await.unwrap();
    let status = resp.status();
    let bytes = resp.into_body().collect().await.unwrap().to_bytes();
    let body = serde_json::from_slice(&bytes)
        .unwrap_or_else(|_| json!({ "raw": String::from_utf8_lossy(&bytes) }));
    (status, body)
}

fn get(uri: &str) -> Request<Body> {
    Request::get(uri).body(Body::empty()).unwrap()
}

fn json_req(method: &str, uri: &str, body: Value) -> Request<Body> {
    Request::builder()
        .method(method)
        .uri(uri)
        .header("content-type", "application/json")
        .body(Body::from(body.to_string()))
        .unwrap()
}

async fn report(
    app: &Router,
    id: &str,
    p: (f64, f64),
    depth: f64,
    source: Option<&str>,
    minutes_ago: i64,
) {
    let mut body = json!({
        "reading_id": id, "lat": p.0, "lon": p.1, "depth_cm": depth, "depth_sigma_cm": 3.0,
        "observed_at": (Utc::now() - Duration::minutes(minutes_ago)).to_rfc3339(), "trust": 1.0
    });
    if let Some(s) = source {
        body["source"] = json!(s);
    }
    let (s, b) = send(app, json_req("POST", "/v1/readings", body)).await;
    assert_eq!(s, StatusCode::OK, "{b}");
}

fn route<'a>(b: &'a Value, target: &str, vehicle: &str) -> &'a Value {
    b["access"]["routes"]
        .as_array()
        .unwrap()
        .iter()
        .find(|r| r["target"] == target && r["vehicle"] == vehicle)
        .unwrap_or_else(|| panic!("no {target} route for {vehicle}: {b}"))
}

#[tokio::test]
async fn a_dry_map_gives_open_routes_and_unknown_zones() {
    let e = env(false);
    let (s, b) = send(&e.app, get("/v1/sites/osm:way:1")).await;
    assert_eq!(s, StatusCode::OK, "{b}");
    assert_eq!(b["name"], "Test DC");
    assert_eq!(b["source"], "osm");
    assert_eq!(
        b["status"], "unknown",
        "nothing reported is unknown, never safe"
    );
    assert_eq!(b["zones"][0]["radius_m"], 300.0);
    assert_eq!(b["zones"][0]["status"], "unknown");
    assert_eq!(b["access"]["advisory"], true);
    let routes = b["access"]["routes"].as_array().unwrap();
    assert_eq!(routes.len(), 6);
    for r in routes {
        assert_eq!(r["status"], "open", "{r}");
        assert_eq!(r["detour_m"], 0.0, "{r}");
        assert!(r["coordinates"].as_array().unwrap().len() >= 2);
    }
    let main = route(&b, "main_road", "truck");
    assert!(
        (main["distance_m"].as_f64().unwrap() - 1200.0).abs() < 30.0,
        "{main}"
    );
    let fuel = route(&b, "fuel", "car");
    assert_eq!(fuel["destination"]["name"], "Test Fuel");
    assert_eq!(fuel["destination"]["id"], "osm:node:9");
    assert!(b["risk"]["areas"].as_array().unwrap().is_empty());
    assert!(b["risk"]["note"].as_str().unwrap().contains("not dry"));
}

#[tokio::test]
async fn water_a_car_cannot_cross_detours_the_car_but_not_the_tanker() {
    let e = env(false);
    // 20 cm half way up the west street, the shortest way to the main road.
    // With the safety margin that is over the 15 cm car and two-wheeler
    // limit but under the 30 cm tanker limit.
    report(&e.app, "r1", at(6.0, 0.0), 20.0, Some("photo"), 0).await;
    let (s, b) = send(&e.app, get("/v1/sites/osm:way:1")).await;
    assert_eq!(s, StatusCode::OK, "{b}");
    let car = route(&b, "main_road", "car");
    assert_eq!(car["status"], "open", "{car}");
    assert!(car["detour_m"].as_f64().unwrap() > 500.0, "{car}");
    assert_eq!(car["impassable_cells_crossed"], 0);
    let two = route(&b, "main_road", "two_wheeler");
    assert!(two["detour_m"].as_f64().unwrap() > 500.0, "{two}");
    let truck = route(&b, "main_road", "truck");
    assert_eq!(truck["status"], "open");
    assert_eq!(truck["detour_m"], 0.0, "{truck}");
    assert!(
        truck["max_known_depth_cm"].as_f64().unwrap() >= 19.0,
        "{truck}"
    );
    // The same water through the engine's route API.
    let (from, to) = (at(0.0, 0.0), at(12.0, 0.0));
    let uri = |v: &str| {
        format!(
            "/v1/route?from={},{}&to={},{}&vehicle={v}",
            from.0, from.1, to.0, to.1
        )
    };
    let (_, t) = send(&e.app, get(&uri("truck"))).await;
    assert_eq!(t["properties"]["depth_limit_cm"], 30.0);
    assert_eq!(t["properties"]["cells_avoided"], 0);
    let (_, c) = send(&e.app, get(&uri("car"))).await;
    assert!(
        c["properties"]["cells_avoided"].as_u64().unwrap() > 0,
        "{c}"
    );
}

#[tokio::test]
async fn deep_water_at_the_site_closes_every_access_route() {
    let e = env(false);
    report(&e.app, "r1", at(0.0, 0.0), 60.0, Some("photo"), 0).await;
    let (_, b) = send(&e.app, get("/v1/sites/osm:way:1")).await;
    assert_eq!(b["status"], "over");
    assert_eq!(b["zones"][0]["status"], "over");
    assert_eq!(b["zones"][0]["deepest"]["depth_cm"], 60.0);
    for r in b["access"]["routes"].as_array().unwrap() {
        assert_eq!(r["status"], "no_safe_route", "{r}");
        assert!(r["impassable_cells_crossed"].as_u64().unwrap() > 0);
        assert!(r["explanation"].as_str().unwrap().contains("Advisory only"));
    }
}

#[tokio::test]
async fn the_list_ranks_sites_by_status_and_validates_its_input() {
    let e = env(false);
    let (s, b) = send(&e.app, get("/v1/sites?city=chennai")).await;
    assert_eq!(s, StatusCode::OK, "{b}");
    assert_eq!(b["total"], 2);
    assert_eq!(b["counts"]["data_centre"]["osm"], 1);
    assert_eq!(b["osm"]["data_until"], "2026-10-25T21:21:21Z");
    assert_eq!(b["advisory"], true);
    report(&e.app, "r1", at(0.0, 0.0), 40.0, Some("photo"), 0).await;
    let (_, b) = send(&e.app, get("/v1/sites?city=chennai&rings=300,1000")).await;
    assert_eq!(b["sites"][0]["id"], "osm:way:1");
    assert_eq!(b["sites"][0]["status"], "over");
    assert_eq!(b["sites"][1]["status"], "unknown");
    let (_, b) = send(&e.app, get("/v1/sites?city=chennai&kind=hospital")).await;
    assert_eq!(b["total"], 1);
    // A threshold above the water turns the zone from over to not over.
    let (_, b) = send(
        &e.app,
        get("/v1/sites?city=chennai&kind=data_centre&threshold_cm=100"),
    )
    .await;
    assert_ne!(b["sites"][0]["status"], "over");
    for (uri, needle) in [
        ("/v1/sites", "city is required"),
        ("/v1/sites?city=chennai&kind=castle", "unknown kind"),
        ("/v1/sites?city=chennai&rings=50", "rings"),
        ("/v1/sites?city=chennai&rings=300,600,900,1200", "rings"),
        ("/v1/sites?city=chennai&threshold_cm=0", "threshold_cm"),
        ("/v1/sites?city=chennai&min_coverage=2", "min_coverage"),
        ("/v1/sites?city=chennai&owner=a%20b", "owner"),
    ] {
        let (s, b) = send(&e.app, get(uri)).await;
        assert_eq!(s, StatusCode::BAD_REQUEST, "{uri}: {b}");
        assert!(b["error"].as_str().unwrap().contains(needle), "{uri}: {b}");
    }
    let (s, _) = send(&e.app, get("/v1/sites/osm:way:404")).await;
    assert_eq!(s, StatusCode::NOT_FOUND);
}

#[tokio::test]
async fn operator_sites_belong_to_their_owner_and_survive_a_restart() {
    let e = env(true);
    let add = |owner: &str, name: &str, lat: f64| {
        json_req(
            "POST",
            "/v1/sites",
            json!({"owner": owner, "kind": "data_centre", "name": name, "lat": lat, "lon": 80.2, "operator": "Acme"}),
        )
    };
    let (s, site) = send(&e.app, add("key1", "Acme DC 1", 13.05)).await;
    assert_eq!(s, StatusCode::CREATED, "{site}");
    let id = site["id"].as_str().unwrap().to_string();
    assert!(id.starts_with("op_"));
    assert_eq!(site["source"], "operator");
    assert_eq!(site["owner"], "key1");
    let (s, b) = send(&e.app, add("key1", "Outside", 14.5)).await;
    assert_eq!(s, StatusCode::BAD_REQUEST, "{b}");
    let (s, b) = send(
        &e.app,
        json_req(
            "POST",
            "/v1/sites",
            json!({"owner": "key1", "kind": "castle", "name": "x", "lat": 13.0, "lon": 80.2}),
        ),
    )
    .await;
    assert_eq!(s, StatusCode::BAD_REQUEST, "{b}");

    // Only the owner sees it.
    let (s, _) = send(&e.app, get(&format!("/v1/sites/{id}?access=false"))).await;
    assert_eq!(s, StatusCode::NOT_FOUND);
    let (s, _) = send(
        &e.app,
        get(&format!("/v1/sites/{id}?owner=key2&access=false")),
    )
    .await;
    assert_eq!(s, StatusCode::NOT_FOUND);
    let (s, b) = send(
        &e.app,
        get(&format!("/v1/sites/{id}?owner=key1&access=false")),
    )
    .await;
    assert_eq!(s, StatusCode::OK, "{b}");
    assert!(b.get("access").is_none());
    let (_, b) = send(&e.app, get("/v1/sites?city=chennai&owner=key1")).await;
    assert_eq!(b["total"], 3);
    assert_eq!(b["counts"]["data_centre"]["operator"], 1);
    let (_, b) = send(&e.app, get("/v1/sites?city=chennai")).await;
    assert_eq!(b["total"], 2, "another key's view never holds it");

    // A restart reloads it from the sites file.
    let cfg = e.state.config.clone();
    let again = AppState::new(cfg);
    neermattam_engine::sites_api::load_state(&again);
    assert_eq!(again.sites.registry.operator_len(), 1);

    let del = |owner: &str, id: &str| {
        Request::delete(format!("/v1/sites/{id}?owner={owner}"))
            .body(Body::empty())
            .unwrap()
    };
    let (s, _) = send(&e.app, del("key2", &id)).await;
    assert_eq!(s, StatusCode::NOT_FOUND);
    let (s, _) = send(&e.app, del("key1", "osm:way:1")).await;
    assert_eq!(s, StatusCode::BAD_REQUEST);
    let (s, _) = send(&e.app, del("key1", &id)).await;
    assert_eq!(s, StatusCode::NO_CONTENT);
    assert_eq!(e.state.sites.registry.operator_len(), 0);
}

#[tokio::test]
async fn the_risk_ledger_keeps_reported_and_unattributed_water_apart() {
    let e = env(true);
    // A wet spell at the site that recedes, from reports with a source.
    report(&e.app, "a1", at(0.0, 0.0), 40.0, Some("photo"), 300).await;
    report(&e.app, "a2", at(0.0, 0.0), 25.0, Some("photo"), 200).await;
    report(&e.app, "a3", at(0.0, 0.0), 0.0, Some("photo"), 100).await;
    // Simulated water elsewhere, with no source.
    report(&e.app, "sim_1", at(12.0, 12.0), 70.0, None, 10).await;

    let (s, b) = send(&e.app, get("/v1/siting?city=chennai")).await;
    assert_eq!(s, StatusCode::OK, "{b}");
    assert_eq!(b["bucket"], "reported");
    assert_eq!(b["advisory"], true);
    assert!(b["note"]
        .as_str()
        .unwrap()
        .contains("builds up over a monsoon"));
    let areas = b["areas"].as_array().unwrap();
    assert_eq!(areas.len(), 1, "{b}");
    let a = &areas[0];
    assert_eq!(a["readings"], 3);
    assert_eq!(a["wet_readings"], 2);
    assert!(a["peak_cm"].as_f64().unwrap() >= 39.0, "{a}");
    // The spell ran from the first wet report to the dry one (a newer
    // conflicting reading replaces the mean), 200 minutes.
    assert_eq!(a["episodes"], 1);
    assert!(
        (a["max_recession_h"].as_f64().unwrap() - 3.3).abs() < 0.11,
        "{a}"
    );
    // No simulated water is in the reported record.
    assert!(a["peak_cm"].as_f64().unwrap() < 70.0);

    let (_, u) = send(&e.app, get("/v1/siting?city=chennai&bucket=unattributed")).await;
    assert_eq!(u["areas"].as_array().unwrap().len(), 1);
    assert_eq!(u["areas"][0]["peak_cm"], 70.0);
    let (s, _) = send(&e.app, get("/v1/siting?city=chennai&bucket=all")).await;
    assert_eq!(s, StatusCode::BAD_REQUEST);

    // The site's own risk block shows its area.
    let (_, site) = send(&e.app, get("/v1/sites/osm:way:1?access=false")).await;
    assert_eq!(site["risk"]["areas"].as_array().unwrap().len(), 1, "{site}");

    // Saved and reloaded.
    assert!(neermattam_engine::sites_api::save_ledger(&e.state).unwrap());
    let again = AppState::new(e.state.config.clone());
    neermattam_engine::sites_api::load_state(&again);
    assert_eq!(*again.sites.ledger.lock(), *e.state.sites.ledger.lock());
}
