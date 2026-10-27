//! Benchmarks: reading ingestion and routing on the real Chennai graph.
//!
//! Run with `cargo bench --bench engine`. The routing benchmarks need the
//! extract at `../data/osm/chennai-roads.osm.pbf` (or `ENGINE_OSM_PATH`) and
//! are skipped without it.

use axum::body::Body;
use axum::http::Request;
use chrono::Utc;
use criterion::{criterion_group, criterion_main, BenchmarkId, Criterion, Throughput};
use h3o::Resolution;
use neermattam_engine::api::{router, AppState};
use neermattam_engine::config::Config;
use neermattam_engine::fusion::{FusionParams, Grid, Observation, WaterMap};
use neermattam_engine::geo::BBox;
use neermattam_engine::graph::RoadGraph;
use neermattam_engine::routing::{plan_route, RoutePolicy};
use neermattam_engine::vehicle::Vehicle;
use std::hint::black_box;
use std::path::PathBuf;
use tower::ServiceExt;

/// Deterministic pseudo-random points inside the service area.
struct Lcg(u64);

impl Lcg {
    fn next_f64(&mut self) -> f64 {
        self.0 = self
            .0
            .wrapping_mul(6_364_136_223_846_793_005)
            .wrapping_add(1_442_695_040_888_963_407);
        (self.0 >> 11) as f64 / (1u64 << 53) as f64
    }

    fn observation(&mut self) -> Observation {
        let b = BBox::CHENNAI;
        Observation {
            lat: b.min_lat + self.next_f64() * (b.max_lat - b.min_lat),
            lon: b.min_lon + self.next_f64() * (b.max_lon - b.min_lon),
            depth_cm: self.next_f64() * 80.0,
            depth_sigma_cm: 2.0 + self.next_f64() * 6.0,
            velocity_mps: Some(self.next_f64()),
            velocity_sigma_mps: Some(0.2),
            observed_at: Utc::now(),
            trust: 0.9,
        }
    }
}

fn ingest(c: &mut Criterion) {
    let mut group = c.benchmark_group("ingest");
    group.throughput(Throughput::Elements(1));
    group.bench_function("grid_ingest", |b| {
        let mut grid = Grid::new(FusionParams::default(), Resolution::Ten);
        let mut rng = Lcg(7);
        b.iter(|| black_box(grid.ingest(&rng.observation()).unwrap()));
    });

    let rt = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .unwrap();
    let state = AppState::new(Config {
        snapshot_path: None,
        ..Config::default()
    });
    let app = router(state);
    let mut rng = Lcg(11);
    group.bench_function("http_post_readings", |b| {
        b.iter(|| {
            let o = rng.observation();
            let body = serde_json::json!({
                "lat": o.lat, "lon": o.lon, "depth_cm": o.depth_cm, "depth_sigma_cm": o.depth_sigma_cm,
                "velocity_mps": o.velocity_mps, "velocity_sigma_mps": o.velocity_sigma_mps,
                "observed_at": o.observed_at.to_rfc3339(), "source": "photo", "trust": 0.9
            });
            let req = Request::post("/v1/readings")
                .header("content-type", "application/json")
                .body(Body::from(body.to_string()))
                .unwrap();
            let resp = rt.block_on(app.clone().oneshot(req)).unwrap();
            assert!(resp.status().is_success());
        });
    });
    group.finish();
}

fn extract() -> Option<PathBuf> {
    let p = std::env::var_os("ENGINE_OSM_PATH")
        .map(PathBuf::from)
        .unwrap_or_else(|| {
            PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../data/osm/chennai-roads.osm.pbf")
        });
    p.exists().then_some(p)
}

fn routing(c: &mut Criterion) {
    let Some(path) = extract() else {
        eprintln!("skipping routing benchmarks: Chennai extract not found");
        return;
    };
    let mut group = c.benchmark_group("graph");
    group.sample_size(10);
    group.bench_function("load_chennai_pbf", |b| {
        b.iter(|| black_box(RoadGraph::load_pbf(&path, Resolution::Ten).unwrap()));
    });
    group.finish();

    let g = RoadGraph::load_pbf(&path, Resolution::Ten).unwrap();
    let s = g.stats();
    eprintln!(
        "graph: {} ways, {} nodes, {} edges, {} segments, {} cell refs, {:.1} MiB",
        s.ways,
        s.nodes,
        s.edges,
        s.segments,
        s.segment_cell_refs,
        s.memory_bytes as f64 / 1_048_576.0
    );
    let policy = RoutePolicy::default();
    let dry: WaterMap = Grid::new(FusionParams::default(), Resolution::Ten).water_map(Utc::now());
    // Random reports spread over the whole service area. With 2000 of them
    // so much of the city is blocked that no dry car route exists, so those
    // cases measure the worst case: proving there is no dry path, then the
    // least-bad fallback search.
    let flooded = |n: usize| {
        let mut grid = Grid::new(FusionParams::default(), Resolution::Ten);
        let mut rng = Lcg(3);
        for _ in 0..n {
            grid.ingest(&rng.observation()).unwrap();
        }
        grid.water_map(Utc::now())
    };
    let some = flooded(200);
    let wet = flooded(2_000);

    let trips = [
        (
            "velachery_guindy_3km",
            (12.9791, 80.2209),
            (13.0067, 80.2206),
        ),
        (
            "tnagar_sholinganallur_16km",
            (13.0418, 80.2341),
            (12.9010, 80.2279),
        ),
        (
            "central_tambaram_25km",
            (13.0827, 80.2707),
            (12.9249, 80.1275),
        ),
    ];
    for (name, from, to) in trips {
        for (map_name, water) in [("200_readings", &some), ("2000_readings", &wet)] {
            let r = plan_route(&g, water, from, to, Vehicle::Car, &policy).unwrap();
            eprintln!(
                "{name} with {map_name}: {:?}, {:.0} m vs baseline {:.0} m, baseline crosses {} impassable cells",
                r.route.kind,
                r.route.distance_m,
                r.baseline.distance_m,
                r.baseline.impassable_cells_crossed
            );
        }
    }
    let mut group = c.benchmark_group("route");
    for (name, from, to) in trips {
        for (map_name, water) in [
            ("dry", &dry),
            ("200_readings", &some),
            ("2000_readings", &wet),
        ] {
            group.bench_with_input(
                BenchmarkId::new(name, map_name),
                &(from, to),
                |b, &(f, t)| {
                    b.iter(|| {
                        black_box(plan_route(&g, water, f, t, Vehicle::Car, &policy).unwrap())
                    });
                },
            );
        }
    }
    group.finish();
}

criterion_group!(benches, ingest, routing);
criterion_main!(benches);
