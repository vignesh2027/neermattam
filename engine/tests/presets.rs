//! The gateway's route presets (`gateway/internal/places/presets.json`)
//! against the real road graphs: every preset snaps to its city's graph for a
//! car and a two-wheeler, every preset routes to the city's first preset for a
//! car, and each city's default pair (the route form's first two presets)
//! routes for a two-wheeler and a car, and bends around water put in the
//! middle of its shortest path.
//!
//! A city whose extract is missing is skipped with a message, as in
//! `tests/multicity.rs`; `NEERMATTAM_REQUIRE_OSM=1` makes that a failure.

use axum::body::Body;
use axum::http::{Request, StatusCode};
use chrono::Utc;
use h3o::Resolution;
use http_body_util::BodyExt;
use neermattam_engine::api::{router, AppState};
use neermattam_engine::city::default_cities;
use neermattam_engine::config::Config;
use neermattam_engine::graph::RoadGraph;
use neermattam_engine::vehicle::Vehicle;
use serde_json::{json, Value};
use std::path::PathBuf;
use std::sync::Arc;
use tower::ServiceExt;

fn osm_dir() -> PathBuf {
    std::env::var_os("ENGINE_OSM_DIR")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../data/osm"))
}

fn presets() -> Value {
    let path =
        PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../gateway/internal/places/presets.json");
    serde_json::from_str(&std::fs::read_to_string(&path).expect("read presets.json"))
        .expect("presets.json is JSON")
}

async fn get(app: &axum::Router, uri: &str) -> (StatusCode, Value) {
    let resp = app
        .clone()
        .oneshot(Request::get(uri).body(Body::empty()).unwrap())
        .await
        .unwrap();
    let s = resp.status();
    let b = resp.into_body().collect().await.unwrap().to_bytes();
    (s, serde_json::from_slice(&b).unwrap())
}

fn point(p: &Value) -> (f64, f64) {
    (p["lat"].as_f64().unwrap(), p["lon"].as_f64().unwrap())
}

fn route_uri(a: (f64, f64), b: (f64, f64), vehicle: &str) -> String {
    format!(
        "/v1/route?from={},{}&to={},{}&vehicle={vehicle}",
        a.0, a.1, b.0, b.1
    )
}

#[tokio::test(flavor = "multi_thread")]
async fn every_city_preset_snaps_and_routes_on_the_real_graph() {
    let require = std::env::var_os("NEERMATTAM_REQUIRE_OSM").is_some();
    let doc = presets();
    let all = default_cities(&osm_dir());
    for c in &all {
        assert!(
            doc["cities"][&c.id]
                .as_array()
                .is_some_and(|l| l.len() >= 5),
            "{} has no route presets in presets.json",
            c.id
        );
    }
    let present: Vec<_> = all
        .into_iter()
        .filter(|c| {
            let ok = c.osm_path.exists();
            if !ok {
                let msg = format!("{} extract not found at {}", c.id, c.osm_path.display());
                assert!(!require, "{msg}");
                eprintln!("skipping {}: {msg}", c.id);
            }
            ok
        })
        .collect();
    if present.is_empty() {
        return;
    }
    let state = AppState::new(Config {
        snapshot_path: None,
        cities: present.clone(),
        ..Config::default()
    });
    let handles: Vec<_> = present
        .iter()
        .cloned()
        .map(|c| {
            std::thread::spawn(move || {
                let g = RoadGraph::load_pbf(&c.osm_path, Resolution::Ten).expect("load extract");
                (c, g)
            })
        })
        .collect();
    for h in handles {
        let (c, g) = h.join().unwrap();
        for p in doc["cities"][&c.id].as_array().unwrap() {
            let (lat, lon) = point(p);
            assert!(
                c.contains(lat, lon),
                "{}: {} is outside the box",
                c.id,
                p["name"]
            );
            for v in [Vehicle::Car, Vehicle::TwoWheeler] {
                let (_, d) = g.nearest_node(lat, lon, v, 300.0).unwrap_or_else(|| {
                    panic!("{}: {} has no {v:?} road within 300 m", c.id, p["name"])
                });
                assert!(d < 300.0);
            }
        }
        assert!(state.set_graph(&c.id, g));
    }
    let app = router(Arc::clone(&state));

    for c in &present {
        let list = doc["cities"][&c.id].as_array().unwrap();
        let (a, b) = (point(&list[0]), point(&list[1]));
        // The default pair, for both vehicles, on a dry map.
        for v in ["two_wheeler", "car"] {
            let (s, r) = get(&app, &route_uri(a, b, v)).await;
            assert_eq!(s, StatusCode::OK, "{} default pair, {v}: {r}", c.id);
            assert_eq!(
                r["properties"]["route_kind"], "water_aware",
                "{}: {r}",
                c.id
            );
            assert_eq!(r["properties"]["city"], c.id.as_str());
        }
        // Every other preset reaches the first one by car.
        for p in &list[1..] {
            let (s, r) = get(&app, &route_uri(point(p), a, "car")).await;
            assert_eq!(s, StatusCode::OK, "{}: {} by car: {r}", c.id, p["name"]);
            let m = r["properties"]["distance_m"].as_f64().unwrap();
            assert!(
                (100.0..60_000.0).contains(&m),
                "{}: {} {m} m",
                c.id,
                p["name"]
            );
        }
    }

    // Chennai's default pair bends around water in the middle of its shortest
    // path, for a two-wheeler and a car.
    if state.graph("chennai").is_some() {
        let list = doc["cities"]["chennai"].as_array().unwrap();
        let (a, b) = (point(&list[0]), point(&list[1]));
        let (_, dry) = get(&app, &route_uri(a, b, "two_wheeler")).await;
        let coords = dry["baseline"]["geometry"]["coordinates"]
            .as_array()
            .unwrap();
        let mid = &coords[coords.len() / 2];
        let body = json!({
            "reading_id": "presets-flood", "lat": mid[1], "lon": mid[0],
            "depth_cm": 50.0, "depth_sigma_cm": 3.0,
            "observed_at": Utc::now().to_rfc3339(), "source": "photo", "trust": 1.0
        });
        let resp = app
            .clone()
            .oneshot(
                Request::post("/v1/readings")
                    .header("content-type", "application/json")
                    .body(Body::from(body.to_string()))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
        for v in ["two_wheeler", "car"] {
            let (s, wet) = get(&app, &route_uri(a, b, v)).await;
            assert_eq!(s, StatusCode::OK);
            let p = &wet["properties"];
            assert_eq!(p["route_kind"], "water_aware", "{v}: {p}");
            assert_eq!(p["passable"], true);
            assert!(p["cells_avoided"].as_u64().unwrap() >= 1, "{v}: {p}");
        }
    }
}
