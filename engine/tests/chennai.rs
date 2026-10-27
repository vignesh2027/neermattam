//! Loads the real Chennai road extract and routes across it.
//!
//! The extract lives in the gitignored `data/` folder. When it is missing the
//! test prints how to get it and passes without checking anything, so a fresh
//! clone still builds green. Set `NEERMATTAM_REQUIRE_OSM=1` to make a missing
//! extract a failure instead.

use chrono::Utc;
use h3o::Resolution;
use neermattam_engine::fusion::{FusionParams, Grid, Observation};
use neermattam_engine::graph::RoadGraph;
use neermattam_engine::routing::{plan_route, RouteKind, RoutePolicy};
use neermattam_engine::vehicle::Vehicle;
use std::path::PathBuf;
use std::time::Instant;

const VELACHERY_STATION: (f64, f64) = (12.9791, 80.2209);
const GUINDY: (f64, f64) = (13.0067, 80.2206);

fn extract() -> Option<PathBuf> {
    let p = std::env::var_os("ENGINE_OSM_PATH")
        .map(PathBuf::from)
        .unwrap_or_else(|| {
            PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../data/osm/chennai-roads.osm.pbf")
        });
    if p.exists() {
        return Some(p);
    }
    let msg = format!(
        "Chennai extract not found at {}. Build it with: osmium tags-filter <tamil-nadu.osm.pbf> w/highway -o data/osm/chennai-roads.osm.pbf (clipped to 80.06,12.79,80.34,13.28).",
        p.display()
    );
    assert!(
        std::env::var_os("NEERMATTAM_REQUIRE_OSM").is_none(),
        "{msg}"
    );
    eprintln!("skipping: {msg}");
    None
}

#[test]
fn routes_velachery_to_guindy_around_a_seeded_flood() {
    let Some(path) = extract() else { return };
    let started = Instant::now();
    let g = RoadGraph::load_pbf(&path, Resolution::Ten).expect("load extract");
    let stats = g.stats();
    eprintln!(
        "loaded {} ways, {} nodes, {} edges, {} segments, {:.1} MiB in {:?}",
        stats.ways,
        stats.nodes,
        stats.edges,
        stats.segments,
        stats.memory_bytes as f64 / 1_048_576.0,
        started.elapsed()
    );
    assert!(
        stats.nodes > 300_000,
        "expected the full extract, got {} nodes",
        stats.nodes
    );
    assert!(stats.memory_bytes < 64 * 1024 * 1024);

    let policy = RoutePolicy::default();
    let now = Utc::now();
    let mut grid = Grid::new(FusionParams::default(), Resolution::Ten);
    let dry = plan_route(
        &g,
        &grid.water_map(now),
        VELACHERY_STATION,
        GUINDY,
        Vehicle::Car,
        &policy,
    )
    .expect("dry route");
    assert!(dry.route.passable);
    assert!(
        (3_000.0..12_000.0).contains(&dry.baseline.distance_m),
        "baseline {} m",
        dry.baseline.distance_m
    );

    // Seed 50 cm of water at the middle of the shortest path.
    let mid = dry.baseline.coordinates[dry.baseline.coordinates.len() / 2];
    grid.ingest(&Observation {
        lat: mid[1],
        lon: mid[0],
        depth_cm: 50.0,
        depth_sigma_cm: 3.0,
        velocity_mps: None,
        velocity_sigma_mps: None,
        observed_at: now,
        trust: 1.0,
    })
    .unwrap();
    let water = grid.water_map(now);
    let t = Instant::now();
    let wet = plan_route(&g, &water, VELACHERY_STATION, GUINDY, Vehicle::Car, &policy)
        .expect("wet route");
    eprintln!(
        "route in {:?}: {:.0} m vs baseline {:.0} m, avoided {} cells, {}",
        t.elapsed(),
        wet.route.distance_m,
        wet.baseline.distance_m,
        wet.route.cells_avoided,
        wet.route.explanation
    );
    assert_eq!(wet.route.kind, RouteKind::WaterAware);
    assert!(wet.route.passable);
    assert!(wet.route.cells_avoided >= 1);
    assert!(wet.baseline.impassable_cells_crossed >= 1);
    assert_eq!(wet.route.impassable_cells_crossed, 0);
    assert!(wet.route.distance_m >= wet.baseline.distance_m);
    assert!(wet.route.advisory);

    // Pedestrians are also routed around it.
    let walk = plan_route(
        &g,
        &water,
        VELACHERY_STATION,
        GUINDY,
        Vehicle::Pedestrian,
        &policy,
    )
    .expect("walk");
    assert!(walk.route.passable);
}
