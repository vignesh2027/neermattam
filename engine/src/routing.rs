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
//! Every cell that holds evidence in force, known or unknown, is judged by
//! one rule. For a cell with a direct reading in force the engine takes
//!
//! ```text
//! depth_block = max(mean, conflict_high_mean)
//!             + k x min(sigma_meas, sigma_cap)            k = 1.5, sigma_cap = 10 cm
//!             + min(q x sqrt(age_h), growth_cap)          q = 6 cm/sqrt(h), growth_cap = 5 cm
//! ```
//!
//! where `sigma_meas` is the uncertainty of the fused measurements alone,
//! `age_h` is the time since the latest evidence in force, and
//! `conflict_high_mean` is the deeper side of a conflict still in force
//! (see [`crate::fusion`]). The displayed sigma and the unknown flag keep the
//! full sigma, which grows without bound; blocking uses this bounded form so
//! that a shallow report does not grow into a closure. A cell whose only
//! evidence in force comes from adjacent cells blocks on the neighbour mean
//! alone, with no margin: deep water next door still closes the road, shallow
//! water does not.
//!
//! Velocity is `mean + k x min(sigma, 0.5 m/s)` when measured (the mean alone
//! for neighbour-only cells), and never less than the conservative default
//! once it is too uncertain to show.
//!
//! - Motor vehicles: impassable when `depth_block` is strictly greater than
//!   the vehicle depth limit, or `depth_block` (m) times velocity is strictly
//!   greater than its D x V limit (see [`crate::vehicle`]).
//! - Pedestrians: impassable when the Defra hazard rating at `depth_block`
//!   reaches `pedestrian_block_class` (danger for some by default, because a
//!   router cannot know whether the walker is a child or elderly). Class
//!   boundaries belong to the higher class, as in the source.
//!
//! The rule is monotonic: more measurement uncertainty or more age never
//! unblocks a cell before it expires, and neither does a conflicting report.
//! A cell that passes and is unknown costs `unknown_penalty` times its length.
//! An expired cell (every track older than the expiry window) and a cell
//! nobody has reported on are unknown and never block.
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
    /// `k` in `mean + k x min(sigma, cap)`.
    pub safety_sigmas: f64,
    /// Cap on the depth sigma in the blocking rule, cm.
    pub sigma_cap_cm: f64,
    /// Cap on the velocity sigma in the blocking rule, m/s.
    pub velocity_sigma_cap_mps: f64,
    /// Cap on the growth-with-time term in the blocking rule, cm.
    pub growth_cap_cm: f64,
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
            sigma_cap_cm: 10.0,
            velocity_sigma_cap_mps: 0.5,
            growth_cap_cm: 5.0,
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

/// The depth (cm) and velocity (m/s) a cell is judged at, and whether the
/// velocity is the assumed default. See the module docs.
#[must_use]
pub fn blocking_estimate(v: &CellView, policy: &RoutePolicy) -> (f64, f64, bool) {
    // Evidence only from adjacent cells counts at its mean, with no margin.
    let k = if v.neighbour_only {
        0.0
    } else {
        policy.safety_sigmas
    };
    let depth = if v.neighbour_only {
        v.depth.mean
    } else {
        let mean = v.depth.mean.max(v.conflict_high_mean_cm.unwrap_or(0.0));
        mean + k * v.sigma_meas_cm.min(policy.sigma_cap_cm) + v.growth_cm.min(policy.growth_cap_cm)
    }
    .max(0.0);
    let (velocity, assumed) = match v.velocity_any {
        None => (policy.default_velocity_mps, true),
        Some(g) => {
            let upper = g.upper_capped(k, policy.velocity_sigma_cap_mps).max(0.0);
            if v.velocity.is_some() {
                (upper, false)
            } else {
                // Too uncertain to show: never assume less than the default.
                (upper.max(policy.default_velocity_mps), true)
            }
        }
    };
    (depth, velocity, assumed)
}

/// True when water this deep, at the default velocity, is too much for the
/// vehicle. Used to describe old reports.
fn too_deep(depth_cm: f64, vehicle: Vehicle, policy: &RoutePolicy) -> bool {
    excess(depth_cm, policy.default_velocity_mps, vehicle, policy).is_some()
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
    if v.expired {
        return Passability::Unknown;
    }
    let (depth, velocity, _) = blocking_estimate(v, policy);
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
    /// Deepest mean depth among known cells on the route, cm. Also returned
    /// as `max_known_depth_cm`.
    pub max_depth_cm_on_route: Option<f64>,
    /// Standard deviation of that depth, cm.
    pub max_depth_sigma_cm: Option<f64>,
    /// Deepest last direct level among unknown cells on the route that still
    /// carry one, cm.
    pub max_unknown_depth_cm: Option<f64>,
    /// Hours since that level was reported.
    pub max_unknown_depth_age_h: Option<f64>,
    /// Unknown cells on the route whose last report was too deep for the
    /// vehicle.
    pub old_deep_cells_crossed: usize,
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
    max_unknown: Option<(f64, f64)>,
    old_deep: usize,
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
    // The start node's own cell, so a route whose ends snap to one node still
    // reports the water the traveller is standing in.
    if let Ok(ll) = h3o::LatLng::new(la, lo) {
        let c = u64::from(ll.to_cell(g.resolution()));
        seen.insert(c);
        cells.push(c);
    }
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
    let mut max_unknown: Option<(f64, f64)> = None;
    let mut old_deep = 0;
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
        if let Some(v) = view.filter(|v| v.unknown) {
            if let (Some(d), Some(age)) = (v.last_depth_cm, v.last_reading_age_h) {
                if max_unknown.is_none_or(|(m, _)| d > m) {
                    max_unknown = Some((d, age));
                }
                if too_deep(d, vehicle, policy) {
                    old_deep += 1;
                }
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
        max_unknown,
        old_deep,
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

/// A sentence about unknown cells whose last report was too deep, if any.
fn old_report_sentence(s: &Stats, vehicle: Vehicle) -> Option<String> {
    let (depth, age) = s.max_unknown?;
    (s.old_deep > 0).then(|| {
        format!(
            "{} of the unknown cells had reports of water too deep for {} that are now out of date, the deepest {:.0} cm reported {:.0} hours ago; treat {} as flooded until you can see otherwise.",
            s.old_deep,
            vehicle.noun(),
            depth,
            age,
            if s.old_deep == 1 { "it" } else { "them" }
        )
    })
}

/// What is known about the water along a route, in one to three sentences.
fn evidence_sentences(s: &Stats, vehicle: Vehicle) -> String {
    let base = evidence_base(s);
    match old_report_sentence(s, vehicle) {
        Some(old) => format!("{base} {old}"),
        None => base,
    }
}

fn evidence_base(s: &Stats) -> String {
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
        max_unknown_depth_cm: s.max_unknown.map(|m| m.0),
        max_unknown_depth_age_h: s.max_unknown.map(|m| m.1),
        old_deep_cells_crossed: s.old_deep,
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
        _ if path.is_empty() => format!(
            "Advisory only. The start and the destination snap to the same point on the road network, so there is no route to plan.{} {} Check conditions before moving.",
            if main.blocked.is_empty() {
                String::new()
            } else {
                format!(" That point is in {limit}.")
            },
            evidence_sentences(&main, vehicle),
        ),
        RouteKind::LeastBad => format!(
            "Advisory only. No route avoids {limit}. This is the least risky route found, and it still crosses {} map cell{} where the water may be too deep. {} Do not attempt it unless you can see the water is shallow; waiting for it to recede is safer.",
            main.blocked.len(),
            if main.blocked.len() == 1 { "" } else { "s" },
            evidence_sentences(&main, vehicle),
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
                evidence_sentences(&main, vehicle),
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
    use crate::fusion::{FusionParams, Gaussian, Grid, Observation};
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

    fn cv(depth: Gaussian, velocity: Option<Gaussian>, unknown: bool) -> CellView {
        CellView {
            cell: h3o::LatLng::new(13.0, 80.2)
                .unwrap()
                .to_cell(Resolution::Ten),
            depth,
            velocity,
            velocity_any: velocity,
            unknown,
            expired: false,
            sigma_meas_cm: depth.sigma(),
            growth_cm: 0.0,
            neighbour_only: false,
            conflict_high_mean_cm: None,
            n_readings: 1,
            n_neighbour: 0,
            conflicts: 0,
            updated_at: now(),
            last_depth_cm: Some(depth.mean),
            last_reading_age_h: Some(0.0),
            last_direct_at: Some(now()),
            neighbour_sources: 0,
        }
    }

    fn blocked(v: &CellView, vehicle: Vehicle) -> bool {
        matches!(
            cell_passability(Some(v), vehicle, &RoutePolicy::default()),
            Passability::Blocked { .. }
        )
    }

    #[test]
    fn pedestrians_use_the_hazard_class() {
        let p = RoutePolicy::default();
        let still = Some(Gaussian::new(0.0, 0.05));
        let view = |depth: f64, unknown: bool| cv(Gaussian::new(depth, 2.0), still, unknown);
        // 10 cm still water: HR about 0.07 + 0.5, low.
        assert_eq!(
            cell_passability(Some(&view(10.0, false)), Vehicle::Pedestrian, &p),
            Passability::Clear
        );
        // 30 cm: DF 1 makes HR >= 1.15, danger for some.
        assert!(blocked(&view(30.0, false), Vehicle::Pedestrian));
        // A car is blocked at 14 cm once the 1.5 sigma margin is added.
        assert!(blocked(&view(14.0, false), Vehicle::Car));
        // Unknown cells with evidence are judged by the same rule.
        assert!(blocked(&view(14.0, true), Vehicle::Car));
        assert!(blocked(&view(40.0, true), Vehicle::Car));
        assert_eq!(
            cell_passability(None, Vehicle::Car, &p),
            Passability::Unknown
        );
    }

    #[test]
    fn more_uncertainty_never_unblocks() {
        // Regression: above 10 cm of sigma the rule switched to the mean
        // alone, so a 14 cm report at trust 0.08 let a car through while the
        // same report at trust 0.1 blocked it.
        // Measurement sigma, growth with time, the unknown flag and the full
        // sigma are swept together; once blocked, a cell stays blocked.
        for vehicle in Vehicle::ALL {
            for mean in [0.0, 3.0, 5.0, 10.0, 14.0, 25.0, 40.0] {
                for growth in [0.0, 1.0, 3.0, 5.0, 8.0, 30.0] {
                    let mut was_blocked = false;
                    for sigma in [0.5, 2.0, 5.0, 9.9, 10.0, 10.1, 15.0, 40.0, 1000.0] {
                        for unknown in [false, true] {
                            let mut v = cv(Gaussian::new(mean, sigma + growth), None, unknown);
                            v.sigma_meas_cm = sigma;
                            v.growth_cm = growth;
                            let b = blocked(&v, vehicle);
                            assert!(
                                b || !was_blocked,
                                "{vehicle} at {mean} cm unblocked at sigma {sigma}, growth {growth}"
                            );
                            was_blocked |= b;
                        }
                    }
                }
                let mut was_blocked = false;
                for growth in [0.0, 0.5, 1.0, 2.0, 5.0, 6.0, 50.0] {
                    let mut v = cv(Gaussian::new(mean, 3.0), None, false);
                    v.growth_cm = growth;
                    let b = blocked(&v, vehicle);
                    assert!(
                        b || !was_blocked,
                        "{vehicle} at {mean} cm unblocked with age"
                    );
                    was_blocked |= b;
                }
            }
        }
        // Velocity: an uncertain fast flow never drops to the slower default.
        let fast = |s: f64, shown: bool| {
            let g = Gaussian::new(2.0, s);
            let mut v = cv(Gaussian::new(12.0, 1.0), Some(g), false);
            if !shown {
                v.velocity = None;
            }
            v
        };
        // 13.5 cm is under the depth limit; 0.135 m x 2.6 m/s is over 0.3.
        assert!(blocked(&fast(0.4, true), Vehicle::Car));
        // Too uncertain to show, it still counts at 2 + 1.5 x 0.5 m/s.
        assert!(blocked(&fast(0.6, false), Vehicle::Car));
        assert!(blocked(&fast(5.0, false), Vehicle::Car));
        // A vague report of a dry street does not block a car: 0 + 15 is
        // not greater than 15.
        for sigma in [10.0, 20.0, 1000.0] {
            let v = cv(Gaussian::new(0.0, sigma), None, true);
            assert_eq!(
                cell_passability(Some(&v), Vehicle::Car, &RoutePolicy::default()),
                Passability::Unknown
            );
        }
    }

    #[test]
    fn a_14_cm_report_blocks_a_car_at_every_trust() {
        for trust in [1.0, 0.5, 0.1, 0.08, 0.01, 1e-6] {
            let mut g = grid();
            let r = g
                .ingest(&Observation {
                    lat: 13.0,
                    lon: 80.2054,
                    depth_cm: 14.0,
                    depth_sigma_cm: 3.0,
                    velocity_mps: None,
                    velocity_sigma_mps: None,
                    observed_at: now(),
                    trust,
                })
                .unwrap();
            let v = g.view(r.cell, now()).unwrap();
            assert!(blocked(&v, Vehicle::Car), "trust {trust}: {v:?}");
        }
    }

    #[test]
    fn an_old_deep_report_blocks_until_it_expires() {
        let mut g = grid();
        flood(&mut g, 13.0, 80.2054, 40.0);
        let cell = g.cell_at(13.0, 80.2054).unwrap();
        let at = |h: i64| g.view(cell, now() + chrono::Duration::minutes(h)).unwrap();
        for minutes in [0, 60, 180, 270, 360, 600, 715] {
            let v = at(minutes);
            assert!(blocked(&v, Vehicle::Car), "car at {minutes} min");
            assert!(blocked(&v, Vehicle::Bus), "bus at {minutes} min");
        }
        let v = at(13 * 60);
        assert!(v.expired);
        assert_eq!(
            cell_passability(Some(&v), Vehicle::Car, &RoutePolicy::default()),
            Passability::Unknown
        );
    }

    #[test]
    fn ten_dry_reports_do_not_clear_a_fresh_deep_one() {
        let mut g = grid();
        let base = |lat: f64, lon: f64, depth: f64, minutes: i64| Observation {
            lat,
            lon,
            depth_cm: depth,
            depth_sigma_cm: 3.0,
            velocity_mps: None,
            velocity_sigma_mps: None,
            observed_at: now() + chrono::Duration::minutes(minutes),
            trust: 1.0,
        };
        let r = g.ingest(&base(13.0, 80.2054, 40.0, 0)).unwrap();
        for i in 1..=10 {
            // All within the first 20 minutes, before any can count toward
            // clearing the conflict.
            g.ingest(&base(13.0, 80.2054, 0.0, 2 * i)).unwrap();
        }
        let v = g
            .view(r.cell, now() + chrono::Duration::minutes(59))
            .unwrap();
        assert!(blocked(&v, Vehicle::Car), "{v:?}");
        assert!(
            v.unknown && v.depth.sigma() >= 10.0 && v.conflicts >= 1,
            "{v:?}"
        );
    }

    fn reading(depth: f64, minutes: i64) -> Observation {
        Observation {
            lat: 13.0,
            lon: 80.2054,
            depth_cm: depth,
            depth_sigma_cm: 3.0,
            velocity_mps: None,
            velocity_sigma_mps: None,
            observed_at: now() + chrono::Duration::minutes(minutes),
            trust: 1.0,
        }
    }

    fn car_blocked_at(g: &Grid, cell: h3o::CellIndex, minutes: i64) -> bool {
        let v = g
            .view(cell, now() + chrono::Duration::minutes(minutes))
            .unwrap();
        blocked(&v, Vehicle::Car)
    }

    const AGES_MIN: [i64; 9] = [1, 10, 60, 90, 180, 360, 600, 700, 719];

    #[test]
    fn a_3_cm_report_never_blocks_a_car_before_expiry() {
        // 3 + 1.5 x 3 + min(growth, 5) is at most 12.5 cm.
        let mut g = grid();
        let cell = g.ingest(&reading(3.0, 0)).unwrap().cell;
        for m in [0].into_iter().chain(AGES_MIN) {
            assert!(!car_blocked_at(&g, cell, m), "blocked at {m} min");
        }
    }

    #[test]
    fn a_10_cm_report_blocks_a_car() {
        // 10 + 1.5 x 3 = 14.5 at the instant of the report; growth passes
        // 0.5 cm within the first minute and the cell blocks from then until
        // it expires.
        let mut g = grid();
        let cell = g.ingest(&reading(10.0, 0)).unwrap().cell;
        for m in AGES_MIN {
            assert!(car_blocked_at(&g, cell, m), "not blocked at {m} min");
        }
        assert!(!car_blocked_at(&g, cell, 13 * 60), "expired at 13 h");
        // With a sigma of 4 cm it blocks at once: 10 + 6 = 16.
        let mut g = grid();
        let mut r = reading(10.0, 0);
        r.depth_sigma_cm = 4.0;
        let cell = g.ingest(&r).unwrap().cell;
        assert!(car_blocked_at(&g, cell, 0));
    }

    #[test]
    fn neighbours_block_on_the_neighbour_mean_alone() {
        let ring_of = |g: &mut Grid, depth: f64| {
            let r = g.ingest(&reading(depth, 0)).unwrap();
            r.touched[1..].to_vec()
        };
        // 2.2 cm next door does not close the adjacent roads, at any age.
        let mut g = grid();
        for n in ring_of(&mut g, 2.2) {
            for m in [0].into_iter().chain(AGES_MIN) {
                let v = g.view(n, now() + chrono::Duration::minutes(m)).unwrap();
                assert!(v.neighbour_only);
                assert!(!blocked(&v, Vehicle::Car), "{n} at {m} min");
            }
        }
        // 30 cm next door closes them for cars but not buses; 14 cm does not.
        let mut g = grid();
        for n in ring_of(&mut g, 30.0) {
            for m in [0].into_iter().chain(AGES_MIN) {
                let v = g.view(n, now() + chrono::Duration::minutes(m)).unwrap();
                assert!(blocked(&v, Vehicle::Car), "{n} at {m} min");
                assert!(!blocked(&v, Vehicle::Bus), "bus at {m} min");
            }
        }
        let mut g = grid();
        for n in ring_of(&mut g, 14.0) {
            assert!(!car_blocked_at(&g, n, 60));
        }
    }

    #[test]
    fn a_conflict_never_unblocks() {
        // One dry report ten minutes after a 40 cm report: the cell blocks on
        // the deeper side of the disagreement.
        let mut g = grid();
        let cell = g.ingest(&reading(40.0, 0)).unwrap().cell;
        g.ingest(&reading(0.0, 10)).unwrap();
        let v = g.view(cell, now() + chrono::Duration::minutes(10)).unwrap();
        assert_eq!(v.conflicts, 1);
        assert_eq!(v.conflict_high_mean_cm, Some(40.0));
        assert!(blocked(&v, Vehicle::Car) && blocked(&v, Vehicle::Bus));
        for m in [60, 360, 700] {
            assert!(car_blocked_at(&g, cell, m), "unblocked at {m} min");
        }
    }

    #[test]
    fn five_agreeing_reports_after_half_an_hour_clear_a_conflict() {
        let mut g = grid();
        let cell = g.ingest(&reading(40.0, 0)).unwrap().cell;
        // Ten dry reports in the first ten minutes: still blocked.
        for m in 1..=10 {
            g.ingest(&reading(0.0, m)).unwrap();
        }
        assert!(car_blocked_at(&g, cell, 10));
        // Four agreeing reports after the 30 minute mark: still blocked.
        for m in 31..=34 {
            g.ingest(&reading(0.0, m)).unwrap();
        }
        assert!(car_blocked_at(&g, cell, 34));
        // A disagreeing report in between restarts the count.
        let mut g2 = g.clone();
        g2.ingest(&reading(25.0, 35)).unwrap();
        g2.ingest(&reading(0.0, 36)).unwrap();
        assert!(car_blocked_at(&g2, cell, 36));
        // The fifth agreeing report clears the conflict and the cell.
        g.ingest(&reading(0.0, 35)).unwrap();
        let v = g.view(cell, now() + chrono::Duration::minutes(35)).unwrap();
        assert_eq!(v.conflict_high_mean_cm, None);
        assert!(!v.unknown, "{v:?}");
        assert_eq!(
            cell_passability(Some(&v), Vehicle::Car, &RoutePolicy::default()),
            Passability::Clear
        );
    }

    #[test]
    fn route_text_names_old_deep_reports_on_unknown_cells() {
        let g = town();
        let mut grid = grid();
        flood(&mut grid, 13.0, 80.2054, 45.0);
        // Thirteen hours later the report has expired and no longer blocks.
        let water = grid.water_map(now() + chrono::Duration::hours(13));
        let r = plan_route(&g, &water, A, B, Vehicle::Car, &RoutePolicy::default()).unwrap();
        assert!(r.route.passable);
        assert_eq!(r.route.max_depth_cm_on_route, None);
        assert_eq!(r.route.max_unknown_depth_cm, Some(45.0));
        assert!((r.route.max_unknown_depth_age_h.unwrap() - 13.0).abs() < 1e-6);
        assert_eq!(r.route.old_deep_cells_crossed, 1);
        assert!(
            r.route.explanation.contains("out of date")
                && r.route.explanation.contains("45 cm reported 13 hours ago"),
            "{}",
            r.route.explanation
        );
        // A bus is told too: 45 cm is over its 30 cm limit.
        let bus = plan_route(&g, &water, A, B, Vehicle::Bus, &RoutePolicy::default()).unwrap();
        assert_eq!(bus.route.old_deep_cells_crossed, 1);
    }

    #[test]
    fn fast_shallow_water_blocks_by_depth_times_velocity() {
        let p = RoutePolicy::default();
        let v = cv(
            Gaussian::new(10.0, 0.5),
            Some(Gaussian::new(3.0, 0.1)),
            false,
        );
        // 0.1075 m x 3.15 m/s is about 0.34 m2/s, over the 0.3 car limit.
        assert!(blocked(&v, Vehicle::Car));
        assert_eq!(
            cell_passability(Some(&v), Vehicle::Bus, &p),
            Passability::Clear
        );
    }

    #[test]
    fn same_start_and_end_in_deep_water_is_not_passable() {
        // Regression: when both ends snapped to one node the route had no
        // cells, so a car standing in 80 cm of water was told it was passable.
        let g = town();
        let mut grid = grid();
        flood(&mut grid, A.0, A.1, 80.0);
        let water = grid.water_map(now());
        let r = plan_route(&g, &water, A, A, Vehicle::Car, &RoutePolicy::default()).unwrap();
        assert!(!r.route.passable);
        assert_eq!(r.route.cells_on_route, 1);
        assert_eq!(r.route.impassable_cells_crossed, 1);
        assert!(r.route.advisory);
        assert!(
            r.route.explanation.contains("same point") && r.route.explanation.contains("80 cm"),
            "{}",
            r.route.explanation
        );
        // On a dry map the same request is passable and says so plainly.
        let dry = grid_dry_route(&g);
        assert!(dry.route.passable && dry.route.unknown_cells_crossed == 1);
    }

    fn grid_dry_route(g: &RoadGraph) -> RouteResult {
        let water = grid().water_map(now());
        plan_route(g, &water, A, A, Vehicle::Car, &RoutePolicy::default()).unwrap()
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
