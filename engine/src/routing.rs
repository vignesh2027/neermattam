//! Vehicle-aware routing with A*.
//!
//! Three searches share one A* implementation with a haversine heuristic:
//!
//! 1. **Baseline**: shortest distance, ignoring water. Returned for comparison.
//! 2. **Water-aware**: edges through any cell that is impassable for the
//!    vehicle are removed. Edges through unknown cells cost `unknown_penalty`
//!    times their length (1.5 by default) but stay usable, so the router
//!    prefers streets with recent reports of shallow water.
//! 3. **Least-bad fallback**, only when the water-aware search finds nothing:
//!    impassable edges are allowed again at a steep cost that rises with how
//!    far the water exceeds the vehicle's limit. The result is marked
//!    `passable: false` with an explanation.
//!
//! # When is a cell impassable?
//!
//! For a known cell, the engine uses the upper estimate `mean + k x sigma`
//! (`k = safety_sigmas`, 1.5 by default) for depth and, when measured, for
//! velocity; when velocity is unknown it uses the conservative default.
//!
//! - Motor vehicles: impassable when upper depth exceeds the vehicle depth
//!   limit, or upper depth (m) times velocity exceeds its D x V limit
//!   (see [`crate::vehicle`]).
//! - Pedestrians: impassable when the Defra hazard rating of the upper depth
//!   reaches `pedestrian_block_class` (danger for some by default, because a
//!   router cannot know whether the walker is a child or elderly).
//!
//! An unknown cell is passable at a penalty, unless its *mean* alone already
//! breaks the limit. Uncertainty never turns evidence of deep water into a
//! green light. A cell nobody has reported on is unknown.
//!
//! The heuristic is admissible because every edge costs at least its length
//! and segment lengths are themselves haversine distances.

use crate::fusion::{CellView, WaterMap};
use crate::geo::haversine_m;
use crate::graph::RoadGraph;
use crate::hazard::{hazard_rating, DebrisRule, HazardClass};
use crate::vehicle::{CrossingLimit, Vehicle};
use serde::Serialize;
use std::cmp::Ordering;
use std::collections::{BinaryHeap, HashSet};

/// Cost multiplier for impassable edges in the least-bad fallback.
const FALLBACK_FACTOR: f64 = 50.0;

/// Routing policy, from configuration.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct RoutePolicy {
    /// `k` in `mean + k x sigma`.
    pub safety_sigmas: f64,
    /// Cost multiplier for edges through unknown cells.
    pub unknown_penalty: f64,
    /// Lowest hazard class that blocks pedestrians.
    pub pedestrian_block_class: HazardClass,
    /// Velocity assumed when it was not measured, m/s.
    pub default_velocity_mps: f64,
    /// Debris factor rule.
    pub debris_rule: DebrisRule,
    /// Furthest a request point may be from a usable road, metres.
    pub max_snap_m: f64,
}

impl Default for RoutePolicy {
    fn default() -> Self {
        RoutePolicy {
            safety_sigmas: 1.5,
            unknown_penalty: 1.5,
            pedestrian_block_class: HazardClass::DangerSome,
            default_velocity_mps: 1.0,
            debris_rule: DebrisRule::Fd2320Precautionary,
            max_snap_m: 1500.0,
        }
    }
}

/// How a vehicle may treat one cell.
#[derive(Clone, Copy, Debug, PartialEq)]
pub enum Passability {
    /// Known and within limits.
    Clear,
    /// Not enough evidence. Passable at a penalty.
    Unknown,
    /// Too deep or too fast. `severity` is the relative excess over the limit.
    Blocked {
        /// Relative excess over the limit, at least 0.
        severity: f64,
    },
}

/// Relative excess of a depth/velocity pair over the vehicle's limit, or
/// `None` when within it.
fn excess(depth_cm: f64, velocity_mps: f64, vehicle: Vehicle, policy: &RoutePolicy) -> Option<f64> {
    let depth_cm = depth_cm.max(0.0);
    match vehicle.profile().limit {
        CrossingLimit::Depth {
            max_depth_cm,
            max_dv_m2s,
        } => {
            let by_depth = (depth_cm - max_depth_cm) / max_depth_cm;
            let by_dv = (depth_cm / 100.0 * velocity_mps - max_dv_m2s) / max_dv_m2s;
            let worst = by_depth.max(by_dv);
            (worst > 0.0).then_some(worst)
        }
        CrossingLimit::HazardClass => {
            let threshold = policy
                .pedestrian_block_class
                .lower_bound()
                .unwrap_or(crate::hazard::HR_DANGER_SOME);
            let hr = hazard_rating(depth_cm / 100.0, velocity_mps, policy.debris_rule);
            (hr >= threshold).then(|| (hr - threshold) / threshold)
        }
    }
}

/// Classify one cell for one vehicle. `None` means no one has reported on it.
#[must_use]
pub fn cell_passability(
    view: Option<&CellView>,
    vehicle: Vehicle,
    policy: &RoutePolicy,
) -> Passability {
    let Some(v) = view else {
        return Passability::Unknown;
    };
    let k = if v.unknown { 0.0 } else { policy.safety_sigmas };
    let depth = v.depth.upper(k);
    let velocity = v
        .velocity
        .map_or(policy.default_velocity_mps, |g| g.upper(k).max(0.0));
    match excess(depth, velocity, vehicle, policy) {
        Some(severity) => Passability::Blocked { severity },
        None if v.unknown => Passability::Unknown,
        None => Passability::Clear,
    }
}

/// Which search produced a route.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum RouteKind {
    /// Avoids every impassable cell.
    WaterAware,
    /// No dry path exists; crosses the least water possible.
    LeastBad,
    /// Shortest path, ignoring water.
    Baseline,
}

/// One computed route with its statistics.
#[derive(Clone, Debug, PartialEq, Serialize)]
pub struct RouteSummary {
    /// Which search produced it.
    pub kind: RouteKind,
    /// `[lon, lat]` pairs.
    pub coordinates: Vec<[f64; 2]>,
    /// Length, metres.
    pub distance_m: f64,
    /// Travel time at the vehicle's average speed, minutes.
    pub eta_min: f64,
    /// Deepest mean depth among known cells on the route, cm.
    pub max_depth_cm_on_route: Option<f64>,
    /// Standard deviation of that depth, cm.
    pub max_depth_sigma_cm: Option<f64>,
    /// Distinct cells the route passes through.
    pub cells_on_route: usize,
    /// Impassable cells on the baseline route that this route avoids.
    pub cells_avoided: usize,
    /// Cells on the route with too little evidence.
    pub unknown_cells_crossed: usize,
    /// Cells on the route that may be too deep for the vehicle.
    pub impassable_cells_crossed: usize,
    /// False when the route crosses water that may be too deep.
    pub passable: bool,
    /// Always true. Routes are advice, not guarantees.
    pub advisory: bool,
    /// Plain-English explanation.
    pub explanation: String,
}

/// A routing answer: the recommended route and the baseline.
#[derive(Clone, Debug, PartialEq, Serialize)]
pub struct RouteResult {
    /// The vehicle routed for.
    pub vehicle: Vehicle,
    /// The recommended route (water-aware, or least-bad).
    pub route: RouteSummary,
    /// The shortest route ignoring water.
    pub baseline: RouteSummary,
}

/// Why no route was produced.
#[derive(Clone, Debug, PartialEq, thiserror::Error)]
pub enum RouteError {
    /// The start point is too far from a usable road.
    #[error("no road usable by {vehicle} within {max_m:.0} m of the start point")]
    StartNotNearRoad {
        /// Vehicle.
        vehicle: Vehicle,
        /// Snap radius.
        max_m: f64,
    },
    /// The end point is too far from a usable road.
    #[error("no road usable by {vehicle} within {max_m:.0} m of the destination")]
    EndNotNearRoad {
        /// Vehicle.
        vehicle: Vehicle,
        /// Snap radius.
        max_m: f64,
    },
    /// The road network does not connect the two points for this vehicle.
    #[error("the road network does not connect these points for {0}")]
    NoPath(Vehicle),
}

#[derive(Clone, Copy, PartialEq)]
struct Entry {
    f: f64,
    node: u32,
}

impl Eq for Entry {}

impl Ord for Entry {
    fn cmp(&self, other: &Self) -> Ordering {
        // Reverse for a min-heap.
        other
            .f
            .total_cmp(&self.f)
            .then_with(|| other.node.cmp(&self.node))
    }
}

impl PartialOrd for Entry {
    fn partial_cmp(&self, other: &Self) -> Option<Ordering> {
        Some(self.cmp(other))
    }
}

/// A* from `src` to `dst` over edges open to `bit`, with `cost(edge, len)`
/// returning `None` for unusable edges. `h_scale` is the smallest cost per
/// metre any edge can have, so `h_scale x distance` stays admissible.
/// Returns the edge ids of the path.
fn astar<F>(
    g: &RoadGraph,
    src: u32,
    dst: u32,
    bit: u8,
    h_scale: f64,
    mut cost: F,
) -> Option<Vec<usize>>
where
    F: FnMut(usize, f64) -> Option<f64>,
{
    let n = g.node_count();
    let mut dist = vec![f64::INFINITY; n];
    let mut prev_edge = vec![u32::MAX; n];
    let mut prev_node = vec![u32::MAX; n];
    let mut closed = vec![false; n];
    let (tlat, tlon) = g.coord(dst);
    // Slightly under one so f32 rounding of lengths never makes it inadmissible.
    let h = |node: u32| {
        let (la, lo) = g.coord(node);
        haversine_m(la, lo, tlat, tlon) * h_scale * 0.999
    };
    let mut heap = BinaryHeap::new();
    dist[src as usize] = 0.0;
    heap.push(Entry {
        f: h(src),
        node: src,
    });
    while let Some(Entry { node, .. }) = heap.pop() {
        let u = node as usize;
        if closed[u] {
            continue;
        }
        if node == dst {
            break;
        }
        closed[u] = true;
        for e in g.out_edges(node) {
            if g.edge_access(e) & bit == 0 {
                continue;
            }
            let to = g.edge_to(e);
            if closed[to as usize] {
                continue;
            }
            let len = g.segment_len_m(g.edge_segment(e));
            let Some(c) = cost(e, len) else { continue };
            let nd = dist[u] + c;
            if nd < dist[to as usize] {
                dist[to as usize] = nd;
                prev_edge[to as usize] = e as u32;
                prev_node[to as usize] = node;
                heap.push(Entry {
                    f: nd + h(to),
                    node: to,
                });
            }
        }
    }
    if !dist[dst as usize].is_finite() {
        return None;
    }
    let mut path = Vec::new();
    let mut at = dst;
    while at != src {
        path.push(prev_edge[at as usize] as usize);
        at = prev_node[at as usize];
    }
    path.reverse();
    Some(path)
}

/// Per-request memo of segment passability, packed into one `f32` per
/// segment: NaN is not computed, -1 clear, -2 unknown, and a value of zero or
/// more is a blocked segment's severity.
struct SegmentCache(Vec<f32>);

impl SegmentCache {
    const CLEAR: f32 = -1.0;
    const UNKNOWN: f32 = -2.0;

    fn new(segments: usize) -> Self {
        SegmentCache(vec![f32::NAN; segments])
    }

    fn get_or_insert(&mut self, seg: u32, compute: impl FnOnce() -> Passability) -> Passability {
        let slot = &mut self.0[seg as usize];
        if slot.is_nan() {
            *slot = match compute() {
                Passability::Clear => Self::CLEAR,
                Passability::Unknown => Self::UNKNOWN,
                Passability::Blocked { severity } => severity.max(0.0) as f32,
            };
        }
        match *slot {
            x if x == Self::CLEAR => Passability::Clear,
            x if x == Self::UNKNOWN => Passability::Unknown,
            x => Passability::Blocked {
                severity: f64::from(x),
            },
        }
    }
}

/// The worst passability over a segment's cells.
fn segment_passability(
    g: &RoadGraph,
    seg: u32,
    water: &WaterMap,
    vehicle: Vehicle,
    policy: &RoutePolicy,
) -> Passability {
    let mut worst = Passability::Clear;
    for c in g.segment_cells(seg) {
        match cell_passability(water.get(c), vehicle, policy) {
            Passability::Blocked { severity } => {
                let prior = match worst {
                    Passability::Blocked { severity: s } => s,
                    _ => -1.0,
                };
                worst = Passability::Blocked {
                    severity: severity.max(prior),
                };
            }
            Passability::Unknown if worst == Passability::Clear => worst = Passability::Unknown,
            _ => {}
        }
    }
    worst
}

struct Stats {
    coordinates: Vec<[f64; 2]>,
    distance_m: f64,
    cells: Vec<u64>,
    blocked: HashSet<u64>,
    unknown: usize,
    max_depth: Option<(f64, f64)>,
}

fn path_stats(
    g: &RoadGraph,
    src: u32,
    path: &[usize],
    water: &WaterMap,
    vehicle: Vehicle,
    policy: &RoutePolicy,
) -> Stats {
    let (la, lo) = g.coord(src);
    let mut coordinates = vec![[lo, la]];
    let mut distance_m = 0.0;
    let mut cells = Vec::new();
    let mut seen = HashSet::new();
    for &e in path {
        let seg = g.edge_segment(e);
        distance_m += g.segment_len_m(seg);
        let (la, lo) = g.coord(g.edge_to(e));
        coordinates.push([lo, la]);
        for &c in g.segment_cells(seg) {
            if seen.insert(c) {
                cells.push(c);
            }
        }
    }
    let mut blocked = HashSet::new();
    let mut unknown = 0;
    let mut max_depth: Option<(f64, f64)> = None;
    for &c in &cells {
        let view = water.get(&c);
        match cell_passability(view, vehicle, policy) {
            Passability::Blocked { .. } => {
                blocked.insert(c);
            }
            Passability::Unknown => unknown += 1,
            Passability::Clear => {}
        }
        if let Some(v) = view.filter(|v| !v.unknown) {
            if max_depth.is_none_or(|(d, _)| v.depth.mean > d) {
                max_depth = Some((v.depth.mean, v.depth.sigma()));
            }
        }
    }
    Stats {
        coordinates,
        distance_m,
        cells,
        blocked,
        unknown,
        max_depth,
    }
}

fn limit_phrase(vehicle: Vehicle, policy: &RoutePolicy) -> String {
    match vehicle.profile().limit {
        CrossingLimit::Depth { max_depth_cm, .. } => format!(
            "water that may be deeper than {max_depth_cm:.0} cm, or flowing too fast, for {}",
            vehicle.noun()
        ),
        CrossingLimit::HazardClass => format!(
            "water rated {} or worse on the Defra flood hazard scale for {}",
            policy.pedestrian_block_class.describe(),
            vehicle.noun()
        ),
    }
}

/// What is known about the water along a route, in one or two sentences.
fn evidence_sentences(s: &Stats) -> String {
    let total = s.cells.len();
    if total > 0 && s.unknown == total {
        return format!(
            "None of the {total} map cells along it has a recent report, so conditions on it are unknown."
        );
    }
    let depth = match s.max_depth {
        Some((d, sigma)) => format!(
            "The deepest reported water on it is about {d:.0} cm, plus or minus {sigma:.0} cm."
        ),
        None => "No recent depth reports cover it.".to_string(),
    };
    if s.unknown == 0 {
        depth
    } else {
        format!(
            "{depth} {} of {total} map cells along it have no recent reports, so conditions there are unknown.",
            s.unknown
        )
    }
}

fn summarise(
    kind: RouteKind,
    s: Stats,
    vehicle: Vehicle,
    explanation: String,
    cells_avoided: usize,
) -> RouteSummary {
    let speed_m_per_min = vehicle.profile().speed_kmh * 1000.0 / 60.0;
    RouteSummary {
        kind,
        distance_m: s.distance_m,
        eta_min: s.distance_m / speed_m_per_min,
        max_depth_cm_on_route: s.max_depth.map(|m| m.0),
        max_depth_sigma_cm: s.max_depth.map(|m| m.1),
        cells_on_route: s.cells.len(),
        cells_avoided,
        unknown_cells_crossed: s.unknown,
        impassable_cells_crossed: s.blocked.len(),
        passable: s.blocked.is_empty(),
        advisory: true,
        explanation,
        coordinates: s.coordinates,
    }
}

/// Plan a water-aware route and the baseline between two points.
pub fn plan_route(
    g: &RoadGraph,
    water: &WaterMap,
    from: (f64, f64),
    to: (f64, f64),
    vehicle: Vehicle,
    policy: &RoutePolicy,
) -> Result<RouteResult, RouteError> {
    let max_m = policy.max_snap_m;
    let (src, _) = g
        .nearest_node(from.0, from.1, vehicle, max_m)
        .ok_or(RouteError::StartNotNearRoad { vehicle, max_m })?;
    let (dst, _) = g
        .nearest_node(to.0, to.1, vehicle, max_m)
        .ok_or(RouteError::EndNotNearRoad { vehicle, max_m })?;
    let bit = vehicle.bit();

    let baseline_path =
        astar(g, src, dst, bit, 1.0, |_, len| Some(len)).ok_or(RouteError::NoPath(vehicle))?;

    let penalty = policy.unknown_penalty.max(1.0);
    // When no reported cell is clear for this vehicle, every usable edge costs
    // at least `penalty` per metre, so the heuristic can be scaled up by it.
    // This keeps A* tight on a map with few reports.
    let any_clear = water
        .values()
        .any(|v| cell_passability(Some(v), vehicle, policy) == Passability::Clear);
    let h_scale = if any_clear { 1.0 } else { penalty };
    // Passability per segment, computed once per request and shared by the
    // water-aware and fallback searches. NaN means not yet computed.
    let mut cache = SegmentCache::new(g.segment_count());
    let mut seg_state = |e: usize| {
        let seg = g.edge_segment(e);
        cache.get_or_insert(seg, || segment_passability(g, seg, water, vehicle, policy))
    };
    let water_path = astar(g, src, dst, bit, h_scale, |e, len| match seg_state(e) {
        Passability::Clear => Some(len),
        Passability::Unknown => Some(len * penalty),
        Passability::Blocked { .. } => None,
    });
    let (kind, path) = match water_path {
        Some(p) => (RouteKind::WaterAware, p),
        None => {
            let p = astar(g, src, dst, bit, h_scale, |e, len| {
                Some(match seg_state(e) {
                    Passability::Clear => len,
                    Passability::Unknown => len * penalty,
                    Passability::Blocked { severity } => {
                        len * penalty * FALLBACK_FACTOR * (1.0 + severity)
                    }
                })
            })
            .ok_or(RouteError::NoPath(vehicle))?;
            (RouteKind::LeastBad, p)
        }
    };

    let base = path_stats(g, src, &baseline_path, water, vehicle, policy);
    let main = path_stats(g, src, &path, water, vehicle, policy);
    let avoided = base.blocked.difference(&main.blocked).count();
    let limit = limit_phrase(vehicle, policy);

    let main_text = match kind {
        RouteKind::LeastBad => format!(
            "Advisory only. No route avoids {limit}. This is the least risky route found, and it still crosses {} map cell{} where the water may be too deep. {} Do not attempt it unless you can see the water is shallow; waiting for it to recede is safer.",
            main.blocked.len(),
            if main.blocked.len() == 1 { "" } else { "s" },
            evidence_sentences(&main),
        ),
        _ => {
            let extra = main.distance_m - base.distance_m;
            let lead = if avoided > 0 {
                format!(
                    "This route avoids {avoided} map cell{} on the shortest path with {limit}. It is {:.0} m ({:.0}%) longer than the shortest route.",
                    if avoided == 1 { "" } else { "s" },
                    extra.max(0.0),
                    100.0 * extra.max(0.0) / base.distance_m.max(1.0)
                )
            } else if extra > 1.0 {
                format!(
                    "Nothing reported blocks the shortest path for {}, but this route is {:.0} m longer because it prefers streets with recent reports of shallow water.",
                    vehicle.noun(),
                    extra
                )
            } else {
                format!("Nothing reported blocks the shortest path for {}, so this is the shortest route.", vehicle.noun())
            };
            format!(
                "Advisory only. {lead} {} Check conditions as you go and turn back if the water looks deeper than expected.",
                evidence_sentences(&main),
            )
        }
    };
    let base_text = if base.blocked.is_empty() {
        "Shortest route ignoring water, shown for comparison. It crosses no cell reported as too deep.".to_string()
    } else {
        format!(
            "Shortest route ignoring water, shown for comparison only. It crosses {} map cell{} with {limit}.",
            base.blocked.len(),
            if base.blocked.len() == 1 { "" } else { "s" }
        )
    };

    Ok(RouteResult {
        vehicle,
        route: summarise(kind, main, vehicle, main_text, avoided),
        baseline: summarise(RouteKind::Baseline, base, vehicle, base_text, 0),
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::fusion::{FusionParams, Grid, Observation};
    use crate::graph::GraphBuilder;
    use chrono::{DateTime, TimeZone, Utc};
    use h3o::Resolution;

    fn now() -> DateTime<Utc> {
        Utc.with_ymd_and_hms(2026, 10, 28, 10, 0, 0).unwrap()
    }

    /// A 3 by 3 block grid of streets, 400 m apart, with nodes every 100 m.
    fn town() -> RoadGraph {
        let mut b = GraphBuilder::new();
        let step = 0.0009; // about 100 m
        let mut id = std::collections::HashMap::new();
        for i in 0..=12 {
            for j in 0..=12 {
                if i % 4 == 0 || j % 4 == 0 {
                    let n = b.add_node(13.0 + step * f64::from(i), 80.2 + step * f64::from(j));
                    id.insert((i, j), n);
                }
            }
        }
        let all = 0b11111;
        for i in (0..=12).step_by(4) {
            b.add_way((0..=12).map(|j| id[&(i, j)]).collect(), all, all);
            b.add_way((0..=12).map(|j| id[&(j, i)]).collect(), all, all);
        }
        b.build(Resolution::Ten)
    }

    fn flood(grid: &mut Grid, lat: f64, lon: f64, depth: f64) {
        grid.ingest(&Observation {
            lat,
            lon,
            depth_cm: depth,
            depth_sigma_cm: 3.0,
            velocity_mps: Some(0.2),
            velocity_sigma_mps: Some(0.1),
            observed_at: now(),
            trust: 1.0,
        })
        .unwrap();
    }

    fn grid() -> Grid {
        Grid::new(FusionParams::default(), Resolution::Ten)
    }

    const A: (f64, f64) = (13.0, 80.2); // south-west corner
    const B: (f64, f64) = (13.0, 80.2 + 0.0108); // south-east corner

    #[test]
    fn dry_map_routes_along_the_shortest_path() {
        let g = town();
        let water = grid().water_map(now());
        let r = plan_route(&g, &water, A, B, Vehicle::Car, &RoutePolicy::default()).unwrap();
        assert!(r.route.passable);
        assert_eq!(r.route.kind, RouteKind::WaterAware);
        assert!((r.route.distance_m - r.baseline.distance_m).abs() < 1e-6);
        assert!(
            (r.route.distance_m - 1170.0).abs() < 20.0,
            "{}",
            r.route.distance_m
        );
        assert_eq!(r.route.unknown_cells_crossed, r.route.cells_on_route);
        assert!(r.route.max_depth_cm_on_route.is_none());
        assert!(r.route.advisory);
        assert!(
            r.route.explanation.contains("conditions on it are unknown"),
            "{}",
            r.route.explanation
        );
    }

    #[test]
    fn route_bends_around_a_flooded_cell() {
        let g = town();
        let mut grid = grid();
        // 45 cm in the middle of the southern street.
        flood(&mut grid, 13.0, 80.2054, 45.0);
        let water = grid.water_map(now());
        let r = plan_route(&g, &water, A, B, Vehicle::Car, &RoutePolicy::default()).unwrap();
        assert_eq!(r.route.kind, RouteKind::WaterAware);
        assert!(r.route.passable);
        assert_eq!(r.route.impassable_cells_crossed, 0);
        assert!(r.baseline.impassable_cells_crossed > 0);
        assert!(r.route.cells_avoided > 0);
        assert!(r.route.distance_m > r.baseline.distance_m + 500.0);
        assert!(
            r.route.explanation.contains("avoids"),
            "{}",
            r.route.explanation
        );
        // A bus may cross 45 cm? No: its limit is 30 cm, so it also detours.
        let bus = plan_route(&g, &water, A, B, Vehicle::Bus, &RoutePolicy::default()).unwrap();
        assert!(bus.route.distance_m > bus.baseline.distance_m);
    }

    #[test]
    fn shallow_water_blocks_a_car_but_not_a_bus() {
        let g = town();
        let mut grid = grid();
        // 15 cm plus 1.5 x 3 cm is over the car limit. The neighbours carry
        // 8.5 cm of sigma, so their upper bound is about 28 cm: under the bus limit.
        flood(&mut grid, 13.0, 80.2054, 15.0);
        let water = grid.water_map(now());
        let p = RoutePolicy::default();
        let car = plan_route(&g, &water, A, B, Vehicle::Car, &p).unwrap();
        let bus = plan_route(&g, &water, A, B, Vehicle::Bus, &p).unwrap();
        assert!(car.route.distance_m > car.baseline.distance_m + 500.0);
        assert!((bus.route.distance_m - bus.baseline.distance_m).abs() < 1e-6);
        assert!(bus.route.max_depth_cm_on_route.unwrap() > 14.0);
        assert!(bus.route.passable);
    }

    #[test]
    fn falls_back_to_least_bad_route_when_everything_is_flooded() {
        let g = town();
        let mut grid = grid();
        // Flood the whole southern band of the town: every route must cross it.
        for j in 0..=12 {
            flood(&mut grid, 13.0009, 80.2 + 0.0009 * f64::from(j), 60.0);
        }
        flood(&mut grid, 13.0, 80.2108, 25.0);
        let water = grid.water_map(now());
        let to = (13.0108, 80.2108); // north-east corner
        let r = plan_route(&g, &water, A, to, Vehicle::Car, &RoutePolicy::default()).unwrap();
        assert_eq!(r.route.kind, RouteKind::LeastBad);
        assert!(!r.route.passable);
        assert!(r.route.impassable_cells_crossed > 0);
        assert!(r.route.explanation.contains("No route avoids"));
        assert!(r.route.advisory);
    }

    #[test]
    fn pedestrians_use_the_hazard_class() {
        let p = RoutePolicy::default();
        let view = |depth: f64, unknown: bool| CellView {
            cell: h3o::LatLng::new(13.0, 80.2)
                .unwrap()
                .to_cell(Resolution::Ten),
            depth: crate::fusion::Gaussian::new(depth, 2.0),
            velocity: Some(crate::fusion::Gaussian::new(0.0, 0.05)),
            unknown,
            n_readings: 1,
            n_neighbour: 0,
            updated_at: now(),
        };
        // 10 cm still water: HR about 0.07 + 0.5, low.
        assert_eq!(
            cell_passability(Some(&view(10.0, false)), Vehicle::Pedestrian, &p),
            Passability::Clear
        );
        // 30 cm: DF 1 makes HR >= 1.15, danger for some.
        assert!(matches!(
            cell_passability(Some(&view(30.0, false)), Vehicle::Pedestrian, &p),
            Passability::Blocked { .. }
        ));
        // A car is blocked at 14 cm once the 1.5 sigma margin is added.
        assert!(matches!(
            cell_passability(Some(&view(14.0, false)), Vehicle::Car, &p),
            Passability::Blocked { .. }
        ));
        // Unknown cells: passable unless the mean alone is too deep.
        assert_eq!(
            cell_passability(Some(&view(14.0, true)), Vehicle::Car, &p),
            Passability::Unknown
        );
        assert!(matches!(
            cell_passability(Some(&view(40.0, true)), Vehicle::Car, &p),
            Passability::Blocked { .. }
        ));
        assert_eq!(
            cell_passability(None, Vehicle::Car, &p),
            Passability::Unknown
        );
    }

    #[test]
    fn fast_shallow_water_blocks_by_depth_times_velocity() {
        let p = RoutePolicy::default();
        let v = CellView {
            cell: h3o::LatLng::new(13.0, 80.2)
                .unwrap()
                .to_cell(Resolution::Ten),
            depth: crate::fusion::Gaussian::new(10.0, 0.5),
            velocity: Some(crate::fusion::Gaussian::new(3.0, 0.1)),
            unknown: false,
            n_readings: 1,
            n_neighbour: 0,
            updated_at: now(),
        };
        // 0.1075 m x 3.15 m/s is about 0.34 m2/s, over the 0.3 car limit.
        assert!(matches!(
            cell_passability(Some(&v), Vehicle::Car, &p),
            Passability::Blocked { .. }
        ));
        assert_eq!(
            cell_passability(Some(&v), Vehicle::Bus, &p),
            Passability::Clear
        );
    }

    #[test]
    fn far_points_are_rejected() {
        let g = town();
        let water = grid().water_map(now());
        let e = plan_route(
            &g,
            &water,
            (13.2, 80.2),
            B,
            Vehicle::Car,
            &RoutePolicy::default(),
        )
        .unwrap_err();
        assert!(matches!(e, RouteError::StartNotNearRoad { .. }));
    }
}
