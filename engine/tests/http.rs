//! HTTP tests against the full router, driven with `tower::ServiceExt`.

use axum::body::Body;
use axum::http::{Request, StatusCode};
use axum::Router;
use chrono::Utc;
use h3o::Resolution;
use http_body_util::BodyExt;
use neermattam_engine::api::{router, AppState};
use neermattam_engine::config::Config;
use neermattam_engine::graph::{GraphBuilder, RoadGraph};
use neermattam_engine::persist;
use serde_json::{json, Value};
use std::sync::Arc;
use std::time::Duration;
use tower::ServiceExt;

/// Chennai only, no snapshot.
fn config() -> Config {
    let d = Config::default();
    Config {
        snapshot_path: None,
        cities: d.cities.into_iter().filter(|c| c.id == "chennai").collect(),
        ..Config::default()
    }
}

/// A 3 by 3 block street grid near Velachery, nodes every 100 m.
fn town() -> RoadGraph {
    let mut b = GraphBuilder::new();
    let step = 0.0009;
    let mut id = std::collections::HashMap::new();
    for i in 0..=12 {
        for j in 0..=12 {
            if i % 4 == 0 || j % 4 == 0 {
                id.insert(
                    (i, j),
                    b.add_node(12.97 + step * f64::from(i), 80.21 + step * f64::from(j)),
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

fn app_with(state: &Arc<AppState>) -> Router {
    router(Arc::clone(state))
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

fn post_json(uri: &str, body: &Value) -> Request<Body> {
    Request::post(uri)
        .header("content-type", "application/json")
        .body(Body::from(body.to_string()))
        .unwrap()
}

fn reading(id: &str, lat: f64, lon: f64, depth: f64) -> Value {
    json!({
        "reading_id": id, "lat": lat, "lon": lon, "depth_cm": depth, "depth_sigma_cm": 3.1,
        "velocity_mps": 0.4, "velocity_sigma_mps": 0.2,
        "observed_at": Utc::now().to_rfc3339(), "source": "photo", "trust": 0.9
    })
}

#[tokio::test]
async fn health_and_readiness() {
    let state = AppState::new(config());
    let app = app_with(&state);
    let (s, b) = send(&app, get("/healthz")).await;
    assert_eq!(s, StatusCode::OK);
    assert_eq!(b["status"], "ok");
    let (s, b) = send(&app, get("/readyz")).await;
    assert_eq!(s, StatusCode::SERVICE_UNAVAILABLE);
    assert!(b["reason"].as_str().unwrap().contains("loading"));
    assert!(state.set_graph("chennai", town()));
    assert!(!state.set_graph("atlantis", town()));
    let (s, b) = send(&app, get("/readyz")).await;
    assert_eq!(s, StatusCode::OK);
    assert!(b["cities"]["chennai"]["nodes"].as_u64().unwrap() > 0);
    let (s, b) = send(&app, get("/nope")).await;
    assert_eq!(s, StatusCode::NOT_FOUND);
    assert!(b["error"].is_string());
}

#[tokio::test]
async fn reading_is_fused_and_returned() {
    let state = AppState::new(config());
    let app = app_with(&state);
    let (s, b) = send(
        &app,
        post_json("/v1/readings", &reading("r_1", 12.9791, 80.2209, 23.4)),
    )
    .await;
    assert_eq!(s, StatusCode::OK, "{b}");
    assert!(b["cell"].as_str().unwrap().starts_with("8a"));
    // trust 0.9 widens sigma to 3.1 / sqrt(0.9).
    assert_eq!(b["depth_cm"], 23.4);
    assert_eq!(b["depth_sigma_cm"], 3.3);
    assert_eq!(b["unknown"], false);
    assert_eq!(b["hazard"]["class"], "low");
    assert_eq!(b["hazard"]["velocity_assumed"], false);
    assert!(b["hazard"]["rating"].as_f64().unwrap() > 0.5);
    assert_eq!(b["duplicate"], false);

    // A retry with the same id is not fused twice.
    let (s, b) = send(
        &app,
        post_json("/v1/readings", &reading("r_1", 12.9791, 80.2209, 23.4)),
    )
    .await;
    assert_eq!(s, StatusCode::OK);
    assert_eq!(b["duplicate"], true);
    assert_eq!(b["n_readings"], 1);

    // A second, independent reading tightens the estimate.
    let (_, b) = send(
        &app,
        post_json("/v1/readings", &reading("r_2", 12.9791, 80.2209, 23.4)),
    )
    .await;
    assert_eq!(b["n_readings"], 2);
    assert!(b["depth_sigma_cm"].as_f64().unwrap() < 3.0);
}

#[tokio::test]
async fn invalid_readings_are_rejected_with_clear_messages() {
    let app = app_with(&AppState::new(config()));
    let cases: Vec<(Value, &str)> = vec![
        (reading("a", 13.5, 80.2, 10.0), "outside the service area"),
        (reading("b", 12.98, 80.22, 600.0), "depth_cm"),
        (reading("c", 12.98, 80.22, -1.0), "depth_cm"),
        (
            {
                let mut r = reading("d", 12.98, 80.22, 10.0);
                r["depth_sigma_cm"] = json!(0);
                r
            },
            "depth_sigma_cm",
        ),
        (
            {
                let mut r = reading("e", 12.98, 80.22, 10.0);
                r["observed_at"] = json!("2020-01-01T00:00:00Z");
                r
            },
            "observed_at",
        ),
        (
            {
                let mut r = reading("f", 12.98, 80.22, 10.0);
                r["observed_at"] = json!("yesterday");
                r
            },
            "observed_at",
        ),
        (
            {
                let mut r = reading("g", 12.98, 80.22, 10.0);
                r.as_object_mut().unwrap().remove("depth_cm");
                r
            },
            "depth_cm",
        ),
        (
            {
                let mut r = reading("h", 12.98, 80.22, 10.0);
                r["source"] = json!("rumour");
                r
            },
            "source",
        ),
        (
            {
                let mut r = reading("i", 12.98, 80.22, 10.0);
                r["trust"] = json!(1.5);
                r
            },
            "trust",
        ),
    ];
    for (body, needle) in cases {
        let (s, b) = send(&app, post_json("/v1/readings", &body)).await;
        assert_eq!(s, StatusCode::BAD_REQUEST, "{body} gave {b}");
        let msg = b["error"].as_str().unwrap();
        assert!(msg.contains(needle), "expected '{needle}' in '{msg}'");
    }
    let req = Request::post("/v1/readings")
        .header("content-type", "application/json")
        .body(Body::from("{not json"))
        .unwrap();
    let (s, b) = send(&app, req).await;
    assert_eq!(s, StatusCode::BAD_REQUEST);
    assert!(b["error"].is_string());
}

#[tokio::test]
async fn a_future_dated_dry_report_cannot_hide_deep_water() {
    // Regression: observed_at 20 hours ahead was accepted. The cell's state
    // then sat in the future, the real 60 cm report arrived "late", was
    // decayed across 20 hours and the cell read 0 cm, low hazard.
    let app = app_with(&AppState::new(config()));
    let mut fake = reading("fake", 12.9791, 80.2209, 0.0);
    fake["observed_at"] = json!((Utc::now() + chrono::Duration::hours(20)).to_rfc3339());
    let (s, b) = send(&app, post_json("/v1/readings", &fake)).await;
    assert_eq!(s, StatusCode::BAD_REQUEST, "{b}");
    assert!(b["error"].as_str().unwrap().contains("in the future"));
    // A couple of minutes of clock drift is accepted and clamped to now.
    let mut drift = reading("drift", 12.9791, 80.2209, 0.0);
    drift["observed_at"] = json!((Utc::now() + chrono::Duration::seconds(120)).to_rfc3339());
    let (s, b) = send(&app, post_json("/v1/readings", &drift)).await;
    assert_eq!(s, StatusCode::OK, "{b}");
    let updated: chrono::DateTime<Utc> = b["updated_at"].as_str().unwrap().parse().unwrap();
    assert!(updated <= Utc::now());
    let (_, b) = send(
        &app,
        post_json("/v1/readings", &reading("real", 12.9791, 80.2209, 60.0)),
    )
    .await;
    assert!(b["depth_cm"].as_f64().unwrap() > 25.0, "{b}");
    assert_ne!(b["hazard"]["class"], "low");
}

#[tokio::test]
async fn a_duplicate_id_is_answered_from_the_original_cell() {
    // Regression: a duplicate id whose body named another place answered
    // 500 "cell state missing after update"; so did a retry that raced the
    // original.
    let app = app_with(&AppState::new(config()));
    let (_, first) = send(
        &app,
        post_json("/v1/readings", &reading("dup", 12.9791, 80.2209, 40.0)),
    )
    .await;
    let (s, b) = send(
        &app,
        post_json("/v1/readings", &reading("dup", 13.1, 80.25, 0.0)),
    )
    .await;
    assert_eq!(s, StatusCode::OK, "{b}");
    assert_eq!(b["duplicate"], true);
    assert_eq!(b["cell"], first["cell"]);
    assert_eq!(b["depth_cm"], first["depth_cm"]);
    // Many concurrent retries of one id: one fusion, no errors.
    let mut tasks = Vec::new();
    for _ in 0..32 {
        let app = app.clone();
        tasks.push(tokio::spawn(async move {
            send(
                &app,
                post_json("/v1/readings", &reading("race", 12.99, 80.23, 10.0)),
            )
            .await
        }));
    }
    let mut fused = 0;
    for t in tasks {
        let (s, b) = t.await.unwrap();
        assert_eq!(s, StatusCode::OK, "{b}");
        if b["duplicate"] == false {
            fused += 1;
        }
    }
    assert_eq!(fused, 1);
}

#[tokio::test]
async fn wrong_method_is_a_json_error() {
    let app = app_with(&AppState::new(config()));
    let req = Request::delete("/v1/readings").body(Body::empty()).unwrap();
    let (s, b) = send(&app, req).await;
    assert_eq!(s, StatusCode::METHOD_NOT_ALLOWED);
    assert!(b["error"].is_string(), "{b}");
}

#[tokio::test]
async fn oversized_body_is_refused() {
    let app = app_with(&AppState::new(config()));
    let mut r = reading("big", 12.98, 80.22, 10.0);
    r["padding"] = json!("x".repeat(40_000));
    let (s, b) = send(&app, post_json("/v1/readings", &r)).await;
    assert_eq!(s, StatusCode::PAYLOAD_TOO_LARGE);
    assert!(b["error"].is_string());
}

#[tokio::test]
async fn cells_are_geojson() {
    let state = AppState::new(config());
    let app = app_with(&state);
    send(
        &app,
        post_json("/v1/readings", &reading("r", 12.9791, 80.2209, 40.0)),
    )
    .await;
    let (s, b) = send(&app, get("/v1/cells?bbox=80.20,12.96,80.24,12.99")).await;
    assert_eq!(s, StatusCode::OK, "{b}");
    assert_eq!(b["type"], "FeatureCollection");
    let features = b["features"].as_array().unwrap();
    assert_eq!(features.len(), 7);
    let centre = features
        .iter()
        .find(|f| f["properties"]["n_readings"] == 1)
        .unwrap();
    let p = &centre["properties"];
    for key in [
        "depth_cm",
        "depth_sigma_cm",
        "velocity_mps",
        "hazard_rating",
        "hazard_class",
        "n_readings",
        "conflicts",
        "conflict_high_mean_cm",
        "depth_sigma_meas_cm",
        "expired",
        "last_depth_cm",
        "last_reading_age_h",
        "updated_at",
    ] {
        assert!(p.get(key).is_some(), "missing {key}");
    }
    assert_eq!(p["hazard_class"], "danger_most");
    assert_eq!(centre["geometry"]["type"], "Polygon");
    let ring = centre["geometry"]["coordinates"][0].as_array().unwrap();
    assert_eq!(ring.first(), ring.last());

    let (s, _) = send(&app, get("/v1/cells?bbox=80.30,13.1,80.33,13.2")).await;
    assert_eq!(s, StatusCode::OK);
    for bad in [
        "/v1/cells",
        "/v1/cells?bbox=1,2,3",
        "/v1/cells?bbox=80.3,12.9,80.2,13.0",
        "/v1/cells?bbox=70,0,90,20",
    ] {
        let (s, b) = send(&app, get(bad)).await;
        assert_eq!(s, StatusCode::BAD_REQUEST, "{bad}");
        assert!(b["error"].as_str().unwrap().contains("bbox"));
    }
}

#[tokio::test]
async fn route_returns_advisory_linestring_with_baseline() {
    let state = AppState::new(config());
    let app = app_with(&state);
    let uri = "/v1/route?from=12.97,80.21&to=12.97,80.2208&vehicle=car";
    let (s, b) = send(&app, get(uri)).await;
    assert_eq!(s, StatusCode::SERVICE_UNAVAILABLE);
    assert!(b["error"].as_str().unwrap().contains("loading"));

    state.set_graph("chennai", town());
    // Flood the southern street.
    send(
        &app,
        post_json("/v1/readings", &reading("f", 12.97, 80.2154, 45.0)),
    )
    .await;
    let (s, b) = send(&app, get(uri)).await;
    assert_eq!(s, StatusCode::OK, "{b}");
    assert_eq!(b["type"], "Feature");
    assert_eq!(b["geometry"]["type"], "LineString");
    let p = &b["properties"];
    assert_eq!(p["advisory"], true);
    assert_eq!(p["passable"], true);
    assert_eq!(p["route_kind"], "water_aware");
    assert!(p["cells_avoided"].as_u64().unwrap() > 0);
    for key in [
        "distance_m",
        "eta_min",
        "max_depth_cm_on_route",
        "max_known_depth_cm",
        "max_unknown_depth_cm",
        "max_unknown_depth_age_h",
        "old_deep_cells_crossed",
        "unknown_cells_crossed",
        "explanation",
    ] {
        assert!(p.get(key).is_some(), "missing {key}");
    }
    let base = &b["baseline"];
    assert_eq!(base["properties"]["route_kind"], "baseline");
    assert_eq!(base["properties"]["advisory"], true);
    assert!(p["distance_m"].as_f64().unwrap() > base["properties"]["distance_m"].as_f64().unwrap());
    let text = p["explanation"].as_str().unwrap();
    assert!(!text.contains('\u{2014}') && !text.contains("->"), "{text}");

    for (bad, needle) in [
        ("/v1/route?from=12.97,80.21&to=12.97,80.2208", "vehicle"),
        (
            "/v1/route?from=12.97,80.21&to=12.97,80.2208&vehicle=boat",
            "vehicle",
        ),
        (
            "/v1/route?from=80.21,12.97&to=12.97,80.2208&vehicle=car",
            "lat,lon",
        ),
        ("/v1/route?to=12.97,80.2208&vehicle=car", "from"),
        (
            "/v1/route?from=13.2,80.3&to=12.97,80.2208&vehicle=car",
            "no road",
        ),
    ] {
        let (s, b) = send(&app, get(bad)).await;
        assert_eq!(s, StatusCode::BAD_REQUEST, "{bad}: {b}");
        assert!(b["error"].as_str().unwrap().contains(needle), "{bad}: {b}");
    }
}

#[tokio::test]
async fn stream_emits_changed_cells() {
    let state = AppState::new(config());
    let app = app_with(&state);
    let resp = app.clone().oneshot(get("/v1/stream")).await.unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
    assert_eq!(resp.headers()["content-type"], "text/event-stream");
    let mut body = resp.into_body();
    send(
        &app,
        post_json("/v1/readings", &reading("s", 12.9791, 80.2209, 12.0)),
    )
    .await;
    let mut text = String::new();
    let deadline = tokio::time::Instant::now() + Duration::from_secs(5);
    while !text.contains("event: cell") {
        let frame = tokio::time::timeout_at(deadline, body.frame())
            .await
            .expect("no SSE event in time");
        let frame = frame.expect("stream ended").unwrap();
        if let Some(data) = frame.data_ref() {
            text.push_str(&String::from_utf8_lossy(data));
        }
    }
    let data_line = text.lines().find(|l| l.starts_with("data: ")).unwrap();
    let feature: Value = serde_json::from_str(&data_line[6..]).unwrap();
    assert_eq!(feature["type"], "Feature");
    assert!(feature["properties"]["depth_cm"].is_number());

    // Shutdown ends the stream so graceful shutdown can finish.
    state.begin_shutdown();
    let end = tokio::time::timeout(Duration::from_secs(5), async {
        while let Some(f) = body.frame().await {
            f.unwrap();
        }
    })
    .await;
    assert!(end.is_ok(), "stream did not end on shutdown");
}

#[tokio::test]
async fn snapshot_survives_restart() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("cells.json");
    let cfg = Config {
        snapshot_path: Some(path.clone()),
        ..config()
    };
    let first = AppState::new(cfg.clone());
    let app = app_with(&first);
    send(
        &app,
        post_json("/v1/readings", &reading("p", 12.9791, 80.2209, 30.0)),
    )
    .await;
    send(
        &app,
        post_json("/v1/readings", &reading("q", 12.9791, 80.2209, 28.0)),
    )
    .await;
    assert_eq!(first.save_snapshot().unwrap(), 7);
    assert_eq!(first.save_history().unwrap(), 2);
    let hpath = persist::history_path(&path);
    assert!(hpath.exists());

    let second = AppState::new(cfg);
    assert_eq!(persist::load(&path, &mut second.grid.write()).unwrap(), 7);
    assert_eq!(
        persist::load_history(&hpath, &mut second.grid.write()).unwrap(),
        2
    );
    let app2 = app_with(&second);
    let (_, b) = send(&app2, get("/v1/cells?bbox=80.20,12.96,80.24,12.99")).await;
    assert_eq!(b["features"].as_array().unwrap().len(), 7);
    let cell = b["features"]
        .as_array()
        .unwrap()
        .iter()
        .find(|f| f["properties"]["n_readings"] == 2)
        .unwrap()["id"]
        .as_str()
        .unwrap()
        .to_string();
    let (_, r) = send(&app2, get(&format!("/v1/recession?cell={cell}"))).await;
    assert_eq!(r["history"].as_array().unwrap().len(), 2);
    assert!(r["history"]
        .as_array()
        .unwrap()
        .iter()
        .all(|p| p["migrated"] == false));
}

#[tokio::test]
async fn history_is_saved_on_its_own_slower_cadence() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("cells.json");
    let hpath = persist::history_path(&path);
    let state = AppState::new(Config {
        snapshot_path: Some(path.clone()),
        snapshot_interval: Duration::from_millis(50),
        history_snapshot_interval: Duration::from_secs(3),
        ..config()
    });
    let looping = tokio::spawn(Arc::clone(&state).snapshot_loop());
    // Let both intervals take their immediate first tick on an empty map.
    tokio::time::sleep(Duration::from_millis(200)).await;
    let app = app_with(&state);
    send(
        &app,
        post_json("/v1/readings", &reading("c1", 12.9791, 80.2209, 30.0)),
    )
    .await;
    // The cell snapshot follows within its 50 ms cadence...
    let deadline = tokio::time::Instant::now() + Duration::from_secs(5);
    while !path.exists() {
        assert!(tokio::time::Instant::now() < deadline, "no cell snapshot");
        tokio::time::sleep(Duration::from_millis(20)).await;
    }
    // ...while the history waits for its own, slower one.
    assert!(!hpath.exists(), "history written on the cell cadence");
    let deadline = tokio::time::Instant::now() + Duration::from_secs(10);
    while !hpath.exists() {
        assert!(tokio::time::Instant::now() < deadline, "no history file");
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    state.begin_shutdown();
    looping.await.unwrap();
    let text = std::fs::read_to_string(&hpath).unwrap();
    assert!(text.contains("neermattam-engine-history"));
    assert!(!std::fs::read_to_string(&path).unwrap().contains("history"));
}

#[tokio::test]
async fn replay_layer_never_reaches_the_live_map() {
    let state = AppState::new(config());
    state.set_graph("chennai", town());
    let app = app_with(&state);
    // A deep simulated reading on the replay layer.
    let (s, b) = send(
        &app,
        post_json(
            "/v1/readings?layer=replay",
            &reading("sim_1", 12.97, 80.2154, 60.0),
        ),
    )
    .await;
    assert_eq!(s, StatusCode::OK, "{b}");
    assert_eq!(b["layer"], "replay");
    assert_eq!(b["depth_cm"], 60.0);

    // The live map, its summary, hotspots and routes do not see it.
    let (_, live) = send(&app, get("/v1/cells?city=chennai")).await;
    assert_eq!(live["layer"], "live");
    assert_eq!(live["features"].as_array().unwrap().len(), 0, "{live}");
    let (_, sum) = send(&app, get("/v1/summary?city=chennai")).await;
    assert_eq!(sum["readings"], 0, "{sum}");
    let (_, hot) = send(&app, get("/v1/hotspots?city=chennai")).await;
    assert_eq!(hot["count"], 0);
    let uri = "/v1/route?from=12.97,80.21&to=12.97,80.2208&vehicle=car";
    let (s, r) = send(&app, get(uri)).await;
    assert_eq!(s, StatusCode::OK, "{r}");
    assert_eq!(r["properties"]["layer"], "live");
    assert_eq!(r["properties"]["cells_avoided"], 0);
    assert_eq!(state.grid.read().len(), 0);

    // The replay map has it, and routes around it.
    let (_, rep) = send(&app, get("/v1/cells?city=chennai&layer=replay")).await;
    assert_eq!(rep["layer"], "replay");
    assert_eq!(rep["features"].as_array().unwrap().len(), 7);
    let (_, sum) = send(&app, get("/v1/summary?city=chennai&layer=replay")).await;
    assert_eq!(sum["readings"], 1, "{sum}");
    let (s, r) = send(&app, get(&format!("{uri}&layer=replay"))).await;
    assert_eq!(s, StatusCode::OK, "{r}");
    assert_eq!(r["properties"]["layer"], "replay");
    assert!(
        r["properties"]["cells_avoided"].as_u64().unwrap() > 0,
        "{r}"
    );
    let cell = rep["features"][0]["id"].as_str().unwrap().to_string();
    let (s, _) = send(&app, get(&format!("/v1/recession?cell={cell}"))).await;
    assert_eq!(s, StatusCode::NOT_FOUND);
    let (s, _) = send(
        &app,
        get(&format!("/v1/recession?cell={cell}&layer=replay")),
    )
    .await;
    assert_eq!(s, StatusCode::OK);

    // The same id on the live layer is a new reading, not a duplicate: the
    // two layers keep their own ids.
    let (_, b) = send(
        &app,
        post_json("/v1/readings", &reading("sim_1", 12.99, 80.22, 5.0)),
    )
    .await;
    assert_eq!(b["layer"], "live");
    assert_eq!(b["duplicate"], false);

    // The risk ledger only ever saw the live reading.
    let (_, siting) = send(&app, get("/v1/siting?city=chennai")).await;
    assert!(!siting.to_string().contains("60.0"), "{siting}");

    for bad in ["/v1/cells?city=chennai&layer=demo", "/v1/summary?layer=x"] {
        let (s, b) = send(&app, get(bad)).await;
        assert_eq!(s, StatusCode::BAD_REQUEST, "{bad}");
        assert!(b["error"].as_str().unwrap().contains("layer"));
    }
}
