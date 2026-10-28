//! Loads every built-in city's real road extract and routes across each one
//! through the HTTP API.
//!
//! The extracts live in the gitignored `data/osm` folder (see
//! `docs/ENGINE.md`, Cities, for how they are cut). A city whose extract is
//! missing is skipped with a message, so a fresh clone still builds green;
//! set `NEERMATTAM_REQUIRE_OSM=1` to make any missing extract a failure.

use axum::body::Body;
use axum::http::{Request, StatusCode};
use chrono::Utc;
use h3o::Resolution;
use http_body_util::BodyExt;
use neermattam_engine::api::{router, AppState};
use neermattam_engine::city::default_cities;
use neermattam_engine::config::Config;
use neermattam_engine::graph::RoadGraph;
use serde_json::{json, Value};
use std::path::PathBuf;
use std::sync::Arc;
use std::time::Instant;
use tower::ServiceExt;

/// A `(lat, lon)` point.
type Point = (f64, f64);

/// A trip per city between two well-known places, about 3 to 15 km apart.
const TRIPS: [(&str, Point, Point); 6] = [
    ("chennai", (13.0827, 80.2707), (12.9791, 80.2209)), // Central to Velachery
    ("mumbai", (18.9398, 72.8355), (19.0544, 72.8406)),  // CSMT to Bandra
    ("bengaluru", (12.9767, 77.5713), (12.9756, 77.6066)), // Majestic to MG Road
    ("hyderabad", (17.4344, 78.5013), (17.3616, 78.4747)), // Secunderabad to Charminar
    ("kolkata", (22.5839, 88.3426), (22.5675, 88.3700)), // Howrah to Sealdah
    ("delhi", (28.6431, 77.2197), (28.6129, 77.2295)),   // New Delhi station to India Gate
];

fn osm_dir() -> PathBuf {
    std::env::var_os("ENGINE_OSM_DIR")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../data/osm"))
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

#[tokio::test(flavor = "multi_thread")]
async fn every_city_routes_on_its_own_graph() {
    let require = std::env::var_os("NEERMATTAM_REQUIRE_OSM").is_some();
    let all = default_cities(&osm_dir());
    let present: Vec<_> = all
        .iter()
        .filter(|c| {
            let ok = c.osm_path.exists();
            if !ok {
                let msg = format!(
                    "{} extract not found at {}; cut it as described in docs/ENGINE.md (Cities)",
                    c.id,
                    c.osm_path.display()
                );
                assert!(!require, "{msg}");
                eprintln!("skipping {}: {msg}", c.id);
            }
            ok
        })
        .cloned()
        .collect();
    if present.is_empty() {
        return;
    }
    let config = Config {
        snapshot_path: None,
        cities: present.clone(),
        ..Config::default()
    };
    let state = AppState::new(config);

    // Load every graph in parallel, as the server does.
    let started = Instant::now();
    let handles: Vec<_> = present
        .iter()
        .cloned()
        .map(|c| {
            std::thread::spawn(move || {
                let t = Instant::now();
                let g = RoadGraph::load_pbf(&c.osm_path, Resolution::Ten).expect("load extract");
                (c.id, g, t.elapsed())
            })
        })
        .collect();
    let mut total_bytes = 0;
    for h in handles {
        let (id, g, took) = h.join().unwrap();
        let s = g.stats();
        total_bytes += s.memory_bytes;
        eprintln!(
            "{id:<10} ways {:>7}  nodes {:>8}  edges {:>8}  segments {:>8}  cell refs {:>8}  {:>6.1} MiB  {:>5} ms",
            s.ways,
            s.nodes,
            s.edges,
            s.segments,
            s.segment_cell_refs,
            s.memory_bytes as f64 / 1_048_576.0,
            took.as_millis()
        );
        assert!(s.nodes > 100_000, "{id}: only {} nodes", s.nodes);
        assert!(state.set_graph(&id, g));
    }
    eprintln!(
        "all {} graphs: {:.1} MiB, {} ms wall (debug-test build)",
        present.len(),
        total_bytes as f64 / 1_048_576.0,
        started.elapsed().as_millis()
    );
    // Well inside an 8 GB laptop.
    assert!(total_bytes < 1024 * 1024 * 1024);

    let app = router(Arc::clone(&state));
    let (s, b) = get(&app, "/readyz").await;
    assert_eq!(s, StatusCode::OK, "{b}");
    let (_, b) = get(&app, "/v1/cities").await;
    assert!(b["cities"]
        .as_array()
        .unwrap()
        .iter()
        .all(|c| c["graph"]["status"] == "ready"));

    for (id, from, to) in TRIPS {
        if state.graph(id).is_none() {
            continue;
        }
        let uri = format!(
            "/v1/route?from={},{}&to={},{}&vehicle=car",
            from.0, from.1, to.0, to.1
        );
        let (s, dry) = get(&app, &uri).await;
        assert_eq!(s, StatusCode::OK, "{id}: {dry}");
        assert_eq!(dry["properties"]["city"], id);
        let base_m = dry["baseline"]["properties"]["distance_m"]
            .as_f64()
            .unwrap();
        assert!((1_000.0..30_000.0).contains(&base_m), "{id}: {base_m} m");

        // Flood the middle of the shortest path; the route must go round it.
        let coords = dry["baseline"]["geometry"]["coordinates"]
            .as_array()
            .unwrap();
        let mid = &coords[coords.len() / 2];
        let body = json!({
            "reading_id": format!("{id}-flood"), "lat": mid[1], "lon": mid[0],
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
        let t = Instant::now();
        let (s, wet) = get(&app, &uri).await;
        assert_eq!(s, StatusCode::OK, "{id}: {wet}");
        let p = &wet["properties"];
        eprintln!(
            "{id:<10} {:>6.0} m baseline, {:>6.0} m around the flood, {} cells avoided, {:?}",
            base_m,
            p["distance_m"].as_f64().unwrap(),
            p["cells_avoided"],
            t.elapsed()
        );
        assert_eq!(p["route_kind"], "water_aware", "{id}: {p}");
        assert_eq!(p["passable"], true);
        assert_eq!(p["impassable_cells_crossed"], 0);
        assert!(p["cells_avoided"].as_u64().unwrap() >= 1, "{id}");
        assert!(
            wet["baseline"]["properties"]["impassable_cells_crossed"]
                .as_u64()
                .unwrap()
                >= 1
        );
    }

    // A trip between two loaded cities is refused, not routed.
    if state.graph("mumbai").is_some() && state.graph("kolkata").is_some() {
        let (s, b) = get(
            &app,
            "/v1/route?from=18.9398,72.8355&to=22.5839,88.3426&vehicle=car",
        )
        .await;
        assert_eq!(s, StatusCode::BAD_REQUEST);
        assert!(b["error"]
            .as_str()
            .unwrap()
            .contains("from is in Mumbai and to is in Kolkata"));
    }
}
