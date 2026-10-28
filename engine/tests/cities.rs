//! HTTP tests for multi-city coverage and the control-room endpoints, on small
//! synthetic street grids so they always run.

use axum::body::Body;
use axum::http::{Request, StatusCode};
use axum::Router;
use chrono::{Duration, Utc};
use h3o::Resolution;
use http_body_util::BodyExt;
use neermattam_engine::api::{router, AppState, EXPORT_CSV_COLUMNS};
use neermattam_engine::config::Config;
use neermattam_engine::graph::{GraphBuilder, RoadGraph};
use serde_json::{json, Value};
use std::sync::Arc;
use tower::ServiceExt;

/// Chennai and Mumbai only, no snapshot.
fn config() -> Config {
    let d = Config::default();
    Config {
        snapshot_path: None,
        cities: d
            .cities
            .into_iter()
            .filter(|c| c.id == "chennai" || c.id == "mumbai")
            .collect(),
        ..Config::default()
    }
}

/// A 3 by 3 block street grid with its south-west corner at (lat, lon).
fn town(lat: f64, lon: f64) -> RoadGraph {
    let mut b = GraphBuilder::new();
    let step = 0.0009;
    let mut id = std::collections::HashMap::new();
    for i in 0..=12 {
        for j in 0..=12 {
            if i % 4 == 0 || j % 4 == 0 {
                id.insert(
                    (i, j),
                    b.add_node(lat + step * f64::from(i), lon + step * f64::from(j)),
                );
            }
        }
    }
    for i in (0..=12).step_by(4) {
        b.add_way((0..=12).map(|j| id[&(i, j)]).collect(), 0b11111, 0b11111);
        b.add_way((0..=12).map(|j| id[&(j, i)]).collect(), 0b11111, 0b11111);
    }
    b.build(Resolution::Ten)
}

const CHENNAI: (f64, f64) = (12.97, 80.21);
const MUMBAI: (f64, f64) = (19.07, 72.88);

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

fn reading_at(id: &str, lat: f64, lon: f64, depth: f64, hours_ago: f64) -> Request<Body> {
    let at = Utc::now() - Duration::milliseconds((hours_ago * 3_600_000.0) as i64);
    let body = json!({
        "reading_id": id, "lat": lat, "lon": lon, "depth_cm": depth, "depth_sigma_cm": 3.0,
        "observed_at": at.to_rfc3339(), "source": "photo", "trust": 1.0
    });
    Request::post("/v1/readings")
        .header("content-type", "application/json")
        .body(Body::from(body.to_string()))
        .unwrap()
}

fn reading(id: &str, lat: f64, lon: f64, depth: f64) -> Request<Body> {
    reading_at(id, lat, lon, depth, 0.0)
}

async fn assert_400(app: &Router, uri: &str, needle: &str) {
    let (s, b) = send(app, get(uri)).await;
    assert_eq!(s, StatusCode::BAD_REQUEST, "{uri}: {b}");
    let msg = b["error"].as_str().unwrap();
    assert!(
        msg.contains(needle),
        "{uri}: expected '{needle}' in '{msg}'"
    );
}

#[tokio::test]
async fn cities_are_listed_with_their_graphs() {
    let state = AppState::new(config());
    let app = router(Arc::clone(&state));
    state.set_graph("chennai", town(CHENNAI.0, CHENNAI.1));
    state.set_graph_error("mumbai", "file not found".into());
    let (s, b) = send(&app, get("/v1/cities")).await;
    assert_eq!(s, StatusCode::OK);
    let cities = b["cities"].as_array().unwrap();
    assert_eq!(cities.len(), 2);
    let c = &cities[0];
    assert_eq!(c["id"], "chennai");
    assert_eq!(c["name"], "Chennai");
    assert_eq!(c["bbox"], json!([80.10, 12.85, 80.33, 13.24]));
    assert!((c["centre"]["lat"].as_f64().unwrap() - 13.045).abs() < 1e-9);
    assert_eq!(c["graph"]["status"], "ready");
    assert!(c["graph"]["nodes"].as_u64().unwrap() > 0);
    assert!(c["graph"]["memory_mib"].is_number());
    assert_eq!(cities[1]["graph"]["status"], "failed");
    assert_eq!(cities[1]["graph"]["error"], "file not found");
    // A failed city keeps readiness down, with the reason.
    let (s, b) = send(&app, get("/readyz")).await;
    assert_eq!(s, StatusCode::SERVICE_UNAVAILABLE);
    assert!(b["reason"]
        .as_str()
        .unwrap()
        .contains("mumbai: file not found"));
}

#[tokio::test]
async fn readings_are_accepted_in_any_city_and_tagged() {
    let app = router(AppState::new(config()));
    let (s, b) = send(&app, reading("m1", MUMBAI.0, MUMBAI.1, 20.0)).await;
    assert_eq!(s, StatusCode::OK, "{b}");
    assert_eq!(b["city"], "mumbai");
    let (_, b) = send(&app, reading("c1", CHENNAI.0, CHENNAI.1, 20.0)).await;
    assert_eq!(b["city"], "chennai");
    // Bengaluru is a built-in city, but not configured here.
    let (s, b) = send(&app, reading("b1", 12.9767, 77.5713, 20.0)).await;
    assert_eq!(s, StatusCode::BAD_REQUEST);
    let msg = b["error"].as_str().unwrap();
    assert!(
        msg.contains("outside the service area") && msg.contains("mumbai"),
        "{msg}"
    );
}

#[tokio::test]
async fn routes_pick_the_city_graph_and_refuse_cross_city_trips() {
    let state = AppState::new(config());
    let app = router(Arc::clone(&state));
    state.set_graph("chennai", town(CHENNAI.0, CHENNAI.1));
    let in_mumbai = format!(
        "/v1/route?from={},{}&to={},{}&vehicle=car",
        MUMBAI.0,
        MUMBAI.1,
        MUMBAI.0,
        MUMBAI.1 + 0.0108
    );
    let (s, b) = send(&app, get(&in_mumbai)).await;
    assert_eq!(s, StatusCode::SERVICE_UNAVAILABLE, "{b}");
    assert!(b["error"]
        .as_str()
        .unwrap()
        .contains("Mumbai is still loading"));

    state.set_graph("mumbai", town(MUMBAI.0, MUMBAI.1));
    let (s, b) = send(&app, get(&in_mumbai)).await;
    assert_eq!(s, StatusCode::OK, "{b}");
    assert_eq!(b["properties"]["city"], "mumbai");
    assert_eq!(b["properties"]["advisory"], true);
    assert_eq!(b["baseline"]["properties"]["city"], "mumbai");
    // The Mumbai graph routes; flooding it bends the Mumbai route only.
    send(&app, reading("mf", MUMBAI.0, MUMBAI.1 + 0.0054, 45.0)).await;
    let (_, b) = send(&app, get(&in_mumbai)).await;
    assert!(
        b["properties"]["cells_avoided"].as_u64().unwrap() > 0,
        "{b}"
    );

    assert_400(
        &app,
        &format!(
            "/v1/route?from={},{}&to={},{}&vehicle=car",
            CHENNAI.0, CHENNAI.1, MUMBAI.0, MUMBAI.1
        ),
        "from is in Chennai and to is in Mumbai",
    )
    .await;
    assert_400(
        &app,
        "/v1/route?from=12.97,80.21&to=12.9767,77.5713&vehicle=car",
        "to (12.9767, 77.5713) is outside every covered city",
    )
    .await;
    assert_400(
        &app,
        "/v1/route?from=1,2&to=12.97,80.21&vehicle=car",
        "from (1, 2) is outside every covered city",
    )
    .await;
    assert_400(
        &app,
        "/v1/route?from=12.97&to=12.97,80.21&vehicle=car",
        "lat,lon",
    )
    .await;
}

#[tokio::test]
async fn failed_city_graph_is_a_clear_503() {
    let state = AppState::new(config());
    let app = router(Arc::clone(&state));
    state.set_graph_error("mumbai", "cannot read OSM extract".into());
    let (s, b) = send(
        &app,
        get("/v1/route?from=19.07,72.88&to=19.08,72.89&vehicle=bus"),
    )
    .await;
    assert_eq!(s, StatusCode::SERVICE_UNAVAILABLE);
    let msg = b["error"].as_str().unwrap();
    assert!(
        msg.contains("Mumbai is unavailable") && msg.contains("cannot read"),
        "{msg}"
    );
}

#[tokio::test]
async fn cells_and_export_are_scoped_by_city() {
    let app = router(AppState::new(config()));
    send(&app, reading("m", MUMBAI.0, MUMBAI.1, 40.0)).await;
    send(&app, reading("c", CHENNAI.0, CHENNAI.1, 10.0)).await;

    let (s, b) = send(&app, get("/v1/cells?city=mumbai")).await;
    assert_eq!(s, StatusCode::OK, "{b}");
    let f = b["features"].as_array().unwrap();
    assert_eq!(f.len(), 7);
    assert!(f.iter().all(|x| x["properties"]["city"] == "mumbai"));
    // A box and a city together intersect; a box alone ignores cities.
    let (_, b) = send(&app, get("/v1/cells?city=chennai&bbox=72.8,19.0,72.9,19.1")).await;
    assert_eq!(b["features"].as_array().unwrap().len(), 0);
    let (_, b) = send(&app, get("/v1/cells?bbox=72.8,19.0,72.9,19.1")).await;
    assert_eq!(b["features"].as_array().unwrap().len(), 7);
    assert_400(&app, "/v1/cells", "bbox or city is required").await;
    assert_400(&app, "/v1/cells?city=atlantis", "unknown city 'atlantis'").await;
    assert_400(
        &app,
        "/v1/cells?city=bengaluru",
        "covered cities are chennai, mumbai",
    )
    .await;

    // CSV export.
    let resp = app
        .clone()
        .oneshot(get("/v1/export?city=mumbai&format=csv"))
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
    let h = resp.headers();
    assert_eq!(h["content-type"], "text/csv; charset=utf-8");
    let cd = h["content-disposition"].to_str().unwrap();
    assert!(cd.starts_with("attachment; filename=\"neermattam-mumbai-") && cd.ends_with(".csv\""));
    assert_eq!(h["x-neermattam-truncated"], "false");
    let text = String::from_utf8(
        resp.into_body()
            .collect()
            .await
            .unwrap()
            .to_bytes()
            .to_vec(),
    )
    .unwrap();
    let lines: Vec<&str> = text.lines().collect();
    assert_eq!(lines[0], EXPORT_CSV_COLUMNS.join(","));
    assert_eq!(lines.len(), 8);
    for row in &lines[1..] {
        assert_eq!(row.split(',').count(), EXPORT_CSV_COLUMNS.len(), "{row}");
        assert!(row.contains(",mumbai,"));
    }
    let deep = lines.iter().find(|l| l.contains(",danger_most,")).unwrap();
    assert!(deep.contains(",40.0,") || deep.contains(",40,"), "{deep}");

    // GeoJSON export, the default format.
    let resp = app
        .clone()
        .oneshot(get("/v1/export?city=chennai"))
        .await
        .unwrap();
    assert_eq!(resp.headers()["content-type"], "application/geo+json");
    let b: Value =
        serde_json::from_slice(&resp.into_body().collect().await.unwrap().to_bytes()).unwrap();
    assert_eq!(b["type"], "FeatureCollection");
    assert_eq!(b["city"], "chennai");
    assert_eq!(b["features"].as_array().unwrap().len(), 7);

    assert_400(&app, "/v1/export?format=csv", "city is required").await;
    assert_400(
        &app,
        "/v1/export?city=mumbai&format=xlsx",
        "format must be geojson or csv",
    )
    .await;
    assert_400(&app, "/v1/export?city=pune", "unknown city").await;
}

#[tokio::test]
async fn stream_can_be_scoped_to_one_city() {
    let state = AppState::new(config());
    let app = router(Arc::clone(&state));
    let resp = app
        .clone()
        .oneshot(get("/v1/stream?city=mumbai"))
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
    let mut body = resp.into_body();
    send(&app, reading("c", CHENNAI.0, CHENNAI.1, 10.0)).await;
    send(&app, reading("m", MUMBAI.0, MUMBAI.1, 10.0)).await;
    let mut text = String::new();
    let deadline = tokio::time::Instant::now() + std::time::Duration::from_secs(5);
    while !text.contains("event: cell") {
        let frame = tokio::time::timeout_at(deadline, body.frame())
            .await
            .expect("no SSE event in time")
            .expect("stream ended")
            .unwrap();
        if let Some(d) = frame.data_ref() {
            text.push_str(&String::from_utf8_lossy(d));
        }
    }
    let data = text.lines().find(|l| l.starts_with("data: ")).unwrap();
    let feature: Value = serde_json::from_str(&data[6..]).unwrap();
    assert_eq!(
        feature["properties"]["city"], "mumbai",
        "a Chennai event leaked through"
    );
    state.begin_shutdown();
    assert_400(&app, "/v1/stream?city=atlantis", "unknown city").await;
}

#[tokio::test]
async fn hotspots_rank_with_reasons() {
    let app = router(AppState::new(config()));
    // Deep and fresh, deep and four hours old, shallow and fresh, dry.
    send(&app, reading("a", 19.00, 72.85, 50.0)).await;
    send(&app, reading_at("b", 19.01, 72.85, 50.0, 4.0)).await;
    send(&app, reading("c", 19.02, 72.85, 12.0)).await;
    send(&app, reading("d", 19.03, 72.85, 0.0)).await;
    send(&app, reading("e", CHENNAI.0, CHENNAI.1, 60.0)).await;

    let (s, b) = send(&app, get("/v1/hotspots?city=mumbai&limit=200")).await;
    assert_eq!(s, StatusCode::OK, "{b}");
    assert_eq!(b["city"], "mumbai");
    assert_eq!(b["advisory"], true);
    assert!(b["score"].as_str().unwrap().contains("depth 40"));
    let list = b["hotspots"].as_array().unwrap();
    assert_eq!(list.len(), 28);
    let cell_at = |lat: f64| {
        h3o::LatLng::new(lat, 72.85)
            .unwrap()
            .to_cell(Resolution::Ten)
            .to_string()
    };
    let pos = |lat: f64| list.iter().position(|h| h["cell"] == cell_at(lat)).unwrap();
    assert_eq!(pos(19.00), 0);
    assert!(pos(19.00) < pos(19.01) && pos(19.01) < pos(19.02) && pos(19.02) < pos(19.03));
    let top = &list[0];
    assert!(top["score"].as_f64().unwrap() > 70.0);
    for key in ["depth", "hazard", "freshness", "reports"] {
        assert!(top["parts"][key].is_number(), "missing part {key}");
    }
    assert!(top["reasons"].as_array().unwrap().len() >= 4);
    assert!(top["blocks"].as_array().unwrap().contains(&json!("car")));
    let scores: Vec<f64> = list.iter().map(|h| h["score"].as_f64().unwrap()).collect();
    assert!(scores.windows(2).all(|w| w[0] >= w[1]));

    let (_, b) = send(&app, get("/v1/hotspots?limit=1")).await;
    assert_eq!(b["count"], 1);
    assert_eq!(b["hotspots"][0]["cell"], {
        h3o::LatLng::new(CHENNAI.0, CHENNAI.1)
            .unwrap()
            .to_cell(Resolution::Ten)
            .to_string()
    });
    let (_, b) = send(&app, get("/v1/hotspots?city=mumbai")).await;
    assert_eq!(b["count"], 20, "default limit");
    for bad in ["0", "201", "ten", "-1", "1.5"] {
        assert_400(&app, &format!("/v1/hotspots?limit={bad}"), "limit must be").await;
    }
    assert_400(&app, "/v1/hotspots?city=gotham", "unknown city").await;
}

#[tokio::test]
async fn summary_counts_a_city() {
    let app = router(AppState::new(config()));
    send(&app, reading("a", 19.00, 72.85, 50.0)).await;
    send(&app, reading("b", 19.01, 72.85, 0.0)).await;
    send(&app, reading_at("c", 19.02, 72.85, 40.0, 8.0)).await; // stale and deep
    send(&app, reading_at("d1", 19.03, 72.85, 40.0, 0.2)).await; // then a conflict
    send(&app, reading("d2", 19.03, 72.85, 0.0)).await;
    send(&app, reading("e", CHENNAI.0, CHENNAI.1, 60.0)).await;

    let (s, b) = send(&app, get("/v1/summary?city=mumbai")).await;
    assert_eq!(s, StatusCode::OK, "{b}");
    assert_eq!(b["city"], "mumbai");
    assert_eq!(b["cells"], 28);
    assert_eq!(b["readings"], 5);
    assert_eq!(b["stale"], 7);
    assert_eq!(b["expired"], 0);
    assert_eq!(b["cells_in_conflict"], 1);
    assert_eq!(b["needs_fresh_report"], 1);
    assert_eq!(b["needs_fresh_report_cells"][0]["last_depth_cm"], 40.0);
    let c = &b["by_class"];
    let total: u64 = ["low", "danger_some", "danger_most", "danger_all", "unknown"]
        .iter()
        .map(|k| c[k].as_u64().unwrap())
        .sum();
    assert_eq!(total, 28);
    assert!(b["blocked"]["car"].as_u64().unwrap() >= 2);
    let newest: chrono::DateTime<Utc> = b["newest_reading_at"].as_str().unwrap().parse().unwrap();
    assert!(Utc::now() - newest < Duration::minutes(1));

    let (_, all) = send(&app, get("/v1/summary")).await;
    assert_eq!(all["cells"], 35);
    assert!(all["city"].is_null());
    assert_400(&app, "/v1/summary?city=x", "unknown city").await;
}

#[tokio::test]
async fn recession_reports_history_and_drain_rate() {
    let app = router(AppState::new(config()));
    // 50, 40, 30, 20 cm, an hour apart, ending now.
    for (i, d) in [50.0, 40.0, 30.0, 20.0].into_iter().enumerate() {
        let (s, b) = send(
            &app,
            reading_at(&format!("r{i}"), MUMBAI.0, MUMBAI.1, d, 3.0 - i as f64),
        )
        .await;
        assert_eq!(s, StatusCode::OK, "{b}");
        assert_eq!(b["conflicts"], 0, "{b}");
    }
    let cell = h3o::LatLng::new(MUMBAI.0, MUMBAI.1)
        .unwrap()
        .to_cell(Resolution::Ten)
        .to_string();
    let (s, b) = send(&app, get(&format!("/v1/recession?cell={cell}"))).await;
    assert_eq!(s, StatusCode::OK, "{b}");
    assert_eq!(b["city"], "mumbai");
    assert_eq!(b["advisory"], true);
    assert_eq!(b["history_capacity"], 48);
    let h = b["history"].as_array().unwrap();
    assert_eq!(h.len(), 4);
    assert_eq!(h[0]["reading_cm"], 50.0);
    assert_eq!(h[3]["reading_cm"], 20.0);
    let d = &b["drain"];
    assert!(
        (d["rate_cm_per_h"].as_f64().unwrap() - 10.0).abs() < 0.05,
        "{d}"
    );
    assert_eq!(d["trend"], "falling");
    assert_eq!(d["points_used"], 4);
    assert!(
        (d["hours_to_15_cm"].as_f64().unwrap() - 0.5).abs() < 0.05,
        "{d}"
    );
    assert!(b["drain_note"]
        .as_str()
        .unwrap()
        .contains("never opens a road"));
    // The drain rate does not open the road: the cell still blocks cars.
    assert!(b["current"]["depth_upper_cm"].as_f64().unwrap() > 15.0);

    // A neighbour-only cell has state but no readings of its own.
    let ring: Vec<h3o::CellIndex> = cell.parse::<h3o::CellIndex>().unwrap().grid_disk(1);
    let n = ring.iter().find(|c| c.to_string() != cell).unwrap();
    let (s, b) = send(&app, get(&format!("/v1/recession?cell={n}"))).await;
    assert_eq!(s, StatusCode::OK);
    assert!(b["drain"].is_null());
    assert!(b["drain_note"]
        .as_str()
        .unwrap()
        .contains("no direct readings"));

    assert_400(&app, "/v1/recession", "cell is required").await;
    assert_400(&app, "/v1/recession?cell=nonsense", "not a valid H3 cell").await;
    let coarse = h3o::LatLng::new(MUMBAI.0, MUMBAI.1)
        .unwrap()
        .to_cell(Resolution::Nine);
    assert_400(
        &app,
        &format!("/v1/recession?cell={coarse}"),
        "resolution 9",
    )
    .await;
    let far = h3o::LatLng::new(19.2, 72.9)
        .unwrap()
        .to_cell(Resolution::Ten);
    let (s, b) = send(&app, get(&format!("/v1/recession?cell={far}"))).await;
    assert_eq!(s, StatusCode::NOT_FOUND, "{b}");
    assert!(b["error"].as_str().unwrap().contains("no reports"));
}
