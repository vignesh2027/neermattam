//! The road graph: a compact directed graph built from an OpenStreetMap PBF
//! extract of highway ways.
//!
//! # Layout
//!
//! Every OSM node used by a kept way becomes a graph node. Each pair of
//! consecutive way nodes is a *segment*, stored once with its length and the
//! H3 cells it passes through. Directed *edges* point at segments, so the two
//! directions of a two-way street share one segment. Out-edges are stored in
//! compressed sparse row form (`first_out`), so the whole graph is a handful of
//! flat vectors and no per-node allocation. Coordinates are `f32`, about 0.15 m
//! of precision at Chennai's latitude.
//!
//! # Access
//!
//! Each edge carries a bit mask of the [`Vehicle`]s allowed to use it in that
//! direction, from the way's `highway` class, its access tags and its
//! one-way rules:
//!
//! - Pedestrians use everything except motorways, including footways, paths,
//!   steps, pedestrian streets, tracks and cycleways, in both directions.
//! - Motor vehicles skip footways, paths, steps, pedestrian streets and
//!   cycleways. Two-wheelers and autos also skip motorways and motorway links,
//!   which Indian expressway rules close to them. Buses skip `service` roads
//!   and living streets. Only pedestrians and two-wheelers use `track`.
//!   Trucks (tankers and other heavy goods vehicles) use what cars use
//!   except living streets, and honour `hgv`.
//! - `access=no|private` closes a way unless a more specific tag reopens it.
//!   `foot`, `motor_vehicle`, `motorcar`, `motorcycle`, `psv` and `bus` are
//!   honoured when they say `no` (or `yes` and `designated` to reopen).
//! - `oneway=yes|true|1` and `oneway=-1|reverse` restrict motor vehicles.
//!   Motorways, motorway links and `junction=roundabout` are one-way unless
//!   tagged `oneway=no`. `oneway:bus=no` lets buses run against the flow.
//!   Pedestrians ignore one-way rules.
//!
//! Only the largest connected component per vehicle is offered for snapping,
//! so a route request never starts on a disconnected scrap of road.
//!
//! # Main roads
//!
//! A node on a `motorway`, `trunk`, `primary` or `secondary` way (or their
//! links) is marked as on a main road, in a spare bit of the node's access
//! mask. Critical-site access routes look for the nearest of these.
//!
//! # Road names
//!
//! Each segment keeps the name of its way (`name:en`, else `name`, else
//! `ref`, such as `NH 32`; none for a bridge or flyover, since water lies on
//! the road beneath it) as an index into one shared table of distinct
//! names, four bytes a segment. [`RoadGraph::nearest_road_name`] gives the
//! control room a place name for a map cell; routing never reads it.

use crate::geo::haversine_m;
use crate::vehicle::Vehicle;
use h3o::{LatLng, Resolution};
use serde::Serialize;
use std::collections::HashMap;
use std::ops::Range;
use std::path::Path;
use std::time::Instant;

const PED: u8 = Vehicle::Pedestrian.bit();
const TWO: u8 = Vehicle::TwoWheeler.bit();
const AUTO: u8 = Vehicle::Auto.bit();
const CAR: u8 = Vehicle::Car.bit();
const BUS: u8 = Vehicle::Bus.bit();
const TRUCK: u8 = Vehicle::Truck.bit();
const MOTOR: u8 = TWO | AUTO | CAR | BUS | TRUCK;
/// Spare bit of a node's access mask: the node is on a main road.
const MAIN_ROAD: u8 = 64;

/// Size of a spatial-index bucket, degrees.
const BUCKET_DEG: f64 = 0.004;

/// The tags of a way that matter for access, borrowed from the PBF block.
#[derive(Clone, Copy, Debug, Default)]
pub struct WayTags<'a> {
    /// `highway`
    pub highway: &'a str,
    /// `oneway`
    pub oneway: Option<&'a str>,
    /// `junction`
    pub junction: Option<&'a str>,
    /// `access`
    pub access: Option<&'a str>,
    /// `foot`
    pub foot: Option<&'a str>,
    /// `motor_vehicle`
    pub motor_vehicle: Option<&'a str>,
    /// `motorcar`
    pub motorcar: Option<&'a str>,
    /// `motorcycle`
    pub motorcycle: Option<&'a str>,
    /// `psv`
    pub psv: Option<&'a str>,
    /// `bus`
    pub bus: Option<&'a str>,
    /// `oneway:bus`
    pub oneway_bus: Option<&'a str>,
    /// `hgv`
    pub hgv: Option<&'a str>,
    /// `name`
    pub name: Option<&'a str>,
    /// `name:en`
    pub name_en: Option<&'a str>,
    /// `ref`, such as `NH 32`
    pub reference: Option<&'a str>,
    /// `bridge`
    pub bridge: Option<&'a str>,
}

impl<'a> WayTags<'a> {
    /// Collect the relevant tags. Returns `None` when there is no `highway` tag.
    pub fn from_tags<I: IntoIterator<Item = (&'a str, &'a str)>>(tags: I) -> Option<Self> {
        let mut t = WayTags::default();
        let mut has_highway = false;
        for (k, v) in tags {
            match k {
                "highway" => {
                    t.highway = v;
                    has_highway = true;
                }
                "oneway" => t.oneway = Some(v),
                "junction" => t.junction = Some(v),
                "access" => t.access = Some(v),
                "foot" => t.foot = Some(v),
                "motor_vehicle" => t.motor_vehicle = Some(v),
                "motorcar" => t.motorcar = Some(v),
                "motorcycle" => t.motorcycle = Some(v),
                "psv" => t.psv = Some(v),
                "bus" => t.bus = Some(v),
                "oneway:bus" => t.oneway_bus = Some(v),
                "hgv" => t.hgv = Some(v),
                "name" => t.name = Some(v),
                "name:en" => t.name_en = Some(v),
                "ref" => t.reference = Some(v),
                "bridge" => t.bridge = Some(v),
                _ => {}
            }
        }
        has_highway.then_some(t)
    }

    /// The name to show for the way: `name:en`, else `name`, else `ref`.
    /// A bridge or flyover has none: water lies on the road beneath it, so
    /// it would name the wrong street.
    #[must_use]
    pub fn display_name(&self) -> Option<&'a str> {
        if self.bridge.is_some_and(|b| b != "no") {
            return None;
        }
        [self.name_en, self.name, self.reference]
            .into_iter()
            .flatten()
            .map(str::trim)
            .find(|n| !n.is_empty())
    }
}

fn is_no(v: Option<&str>) -> bool {
    matches!(v, Some("no" | "private"))
}

fn is_yes(v: Option<&str>) -> bool {
    matches!(v, Some("yes" | "designated" | "permissive"))
}

/// Forward and backward access masks for a way, or `None` when nobody may
/// use it.
#[must_use]
pub fn way_access(t: &WayTags<'_>) -> Option<(u8, u8)> {
    let base = match t.highway {
        "motorway" | "motorway_link" => CAR | BUS | TRUCK,
        "trunk" | "trunk_link" | "primary" | "primary_link" | "secondary" | "secondary_link"
        | "tertiary" | "tertiary_link" | "unclassified" | "residential" | "road" => PED | MOTOR,
        "living_street" => PED | TWO | AUTO | CAR,
        // Service roads are the driveways and yards a tanker uses to reach a
        // site.
        "service" => PED | TWO | AUTO | CAR | TRUCK,
        "track" => PED | TWO,
        "pedestrian" | "footway" | "path" | "steps" | "cycleway" | "bridleway" | "corridor" => PED,
        "busway" | "bus_guideway" => BUS,
        _ => 0,
    };
    let mut mask = base;
    if is_no(t.access) {
        mask = 0;
    }
    // Specific tags override the general access tag.
    if is_no(t.foot) {
        mask &= !PED;
    } else if is_yes(t.foot) && !t.highway.starts_with("motorway") {
        mask |= PED;
    }
    if is_no(t.motor_vehicle) {
        mask &= !MOTOR;
    } else if is_yes(t.motor_vehicle) {
        mask |= base & MOTOR;
    }
    if is_no(t.motorcar) {
        mask &= !CAR;
    }
    if is_no(t.motorcycle) {
        mask &= !TWO;
    }
    if is_no(t.hgv) {
        mask &= !TRUCK;
    } else if is_yes(t.hgv) {
        mask |= base & TRUCK;
    }
    if is_no(t.psv) || is_no(t.bus) {
        mask &= !BUS;
    } else if is_yes(t.psv) || is_yes(t.bus) {
        mask |= base & BUS;
    }
    if mask == 0 {
        return None;
    }

    let implied_oneway = t.highway == "motorway"
        || t.highway == "motorway_link"
        || t.junction == Some("roundabout")
        || t.junction == Some("circular");
    let (mut fwd, mut bwd) = (mask, mask);
    match t.oneway {
        Some("yes" | "true" | "1") => bwd &= !MOTOR,
        Some("-1" | "reverse") => fwd &= !MOTOR,
        Some("no" | "false" | "0") => {}
        _ if implied_oneway => bwd &= !MOTOR,
        _ => {}
    }
    if t.oneway_bus == Some("no") {
        fwd |= mask & BUS;
        bwd |= mask & BUS;
    }
    Some((fwd, bwd))
}

/// True for the `highway` classes whose nodes count as a main road.
#[must_use]
pub fn is_main_highway(highway: &str) -> bool {
    matches!(
        highway,
        "motorway"
            | "motorway_link"
            | "trunk"
            | "trunk_link"
            | "primary"
            | "primary_link"
            | "secondary"
            | "secondary_link"
    )
}

/// A way handed to [`GraphBuilder`], with node positions as builder indices.
#[derive(Clone, Debug)]
pub struct WayInput {
    /// Builder node indices in way order.
    pub nodes: Vec<u32>,
    /// Vehicles allowed in way direction.
    pub forward: u8,
    /// Vehicles allowed against way direction.
    pub backward: u8,
    /// A main road (see the module docs).
    pub main: bool,
    /// Index of the way's name in the builder's name table.
    pub name: Option<u32>,
}

/// Value of a segment's name index when its way has no name.
const NO_NAME: u32 = u32::MAX;

/// Distinct road names, stored once each.
#[derive(Clone, Debug, Default)]
struct NameTable {
    text: String,
    start: Vec<u32>,
    ids: HashMap<Box<str>, u32>,
}

impl NameTable {
    /// The id of a name, adding it when new. Names are cut to 80 bytes at a
    /// character boundary.
    fn intern(&mut self, name: &str) -> u32 {
        let mut end = name.len().min(80);
        while !name.is_char_boundary(end) {
            end -= 1;
        }
        let name = &name[..end];
        if let Some(&id) = self.ids.get(name) {
            return id;
        }
        if self.start.is_empty() {
            self.start.push(0);
        }
        let id = u32::try_from(self.start.len() - 1).expect("too many road names");
        self.text.push_str(name);
        self.start
            .push(u32::try_from(self.text.len()).expect("road names over 4 GB"));
        self.ids.insert(name.into(), id);
        id
    }

    fn get(&self, id: u32) -> Option<&str> {
        let i = id as usize;
        let (a, b) = (*self.start.get(i)?, *self.start.get(i + 1)?);
        self.text.get(a as usize..b as usize)
    }

    /// Drop the lookup map once the graph is built.
    fn freeze(mut self) -> Self {
        self.ids = HashMap::new();
        self.text.shrink_to_fit();
        self.start.shrink_to_fit();
        self
    }

    fn memory_bytes(&self) -> usize {
        self.text.capacity() + self.start.capacity() * 4
    }
}

/// Collects nodes and ways, then builds a [`RoadGraph`].
#[derive(Clone, Debug, Default)]
pub struct GraphBuilder {
    lat: Vec<f32>,
    lon: Vec<f32>,
    ways: Vec<WayInput>,
    names: NameTable,
}

impl GraphBuilder {
    /// An empty builder.
    #[must_use]
    pub fn new() -> Self {
        Self::default()
    }

    /// Add a node and return its index.
    pub fn add_node(&mut self, lat: f64, lon: f64) -> u32 {
        self.lat.push(lat as f32);
        self.lon.push(lon as f32);
        u32::try_from(self.lat.len() - 1).expect("more than u32::MAX nodes")
    }

    /// Add a way. Masks of zero are ignored.
    pub fn add_way(&mut self, nodes: Vec<u32>, forward: u8, backward: u8) {
        self.add_way_class(nodes, forward, backward, false);
    }

    /// Add a way, saying whether it is a main road. Masks of zero are
    /// ignored.
    pub fn add_way_class(&mut self, nodes: Vec<u32>, forward: u8, backward: u8, main: bool) {
        self.add_named_way(nodes, forward, backward, main, None);
    }

    /// Add a way with its class and name. Masks of zero are ignored.
    pub fn add_named_way(
        &mut self,
        nodes: Vec<u32>,
        forward: u8,
        backward: u8,
        main: bool,
        name: Option<&str>,
    ) {
        if nodes.len() >= 2 && (forward | backward) != 0 {
            let name = name.map(|n| self.names.intern(n));
            self.ways.push(WayInput {
                nodes,
                forward,
                backward,
                main,
                name,
            });
        }
    }

    /// Add a way whose name is already in the builder's table.
    fn push_way(
        &mut self,
        nodes: Vec<u32>,
        forward: u8,
        backward: u8,
        main: bool,
        name: Option<u32>,
    ) {
        if nodes.len() >= 2 && (forward | backward) != 0 {
            self.ways.push(WayInput {
                nodes,
                forward,
                backward,
                main,
                name,
            });
        }
    }

    /// Build the graph, computing segment cells at `resolution`.
    #[must_use]
    pub fn build(self, resolution: Resolution) -> RoadGraph {
        let started = Instant::now();
        let n = self.lat.len();
        let ways_used = self.ways.len();

        // Segments and directed edges.
        let mut seg_a: Vec<u32> = Vec::new();
        let mut seg_b: Vec<u32> = Vec::new();
        let mut seg_name: Vec<u32> = Vec::new();
        let mut raw_edges: Vec<(u32, u32, u32, u8)> = Vec::new(); // from, to, seg, mask
        let mut main_node = vec![false; n];
        for w in &self.ways {
            if w.main {
                for &v in &w.nodes {
                    main_node[v as usize] = true;
                }
            }
            for pair in w.nodes.windows(2) {
                let (a, b) = (pair[0], pair[1]);
                if a == b {
                    continue;
                }
                let seg = u32::try_from(seg_a.len()).expect("too many segments");
                seg_a.push(a);
                seg_b.push(b);
                seg_name.push(w.name.unwrap_or(NO_NAME));
                if w.forward != 0 {
                    raw_edges.push((a, b, seg, w.forward));
                }
                if w.backward != 0 {
                    raw_edges.push((b, a, seg, w.backward));
                }
            }
        }
        drop(self.ways);

        let seg_len_m: Vec<f32> = seg_a
            .iter()
            .zip(&seg_b)
            .map(|(&a, &b)| {
                let (a, b) = (a as usize, b as usize);
                haversine_m(
                    f64::from(self.lat[a]),
                    f64::from(self.lon[a]),
                    f64::from(self.lat[b]),
                    f64::from(self.lon[b]),
                ) as f32
            })
            .collect();

        let (seg_cells_start, cells) =
            segment_cells(&self.lat, &self.lon, &seg_a, &seg_b, resolution);

        // CSR by source node.
        raw_edges.sort_unstable_by_key(|e| e.0);
        let mut first_out = vec![0u32; n + 1];
        for e in &raw_edges {
            first_out[e.0 as usize + 1] += 1;
        }
        for i in 0..n {
            first_out[i + 1] += first_out[i];
        }
        let edge_to: Vec<u32> = raw_edges.iter().map(|e| e.1).collect();
        let edge_seg: Vec<u32> = raw_edges.iter().map(|e| e.2).collect();
        let edge_access: Vec<u8> = raw_edges.iter().map(|e| e.3).collect();
        drop(raw_edges);

        let mut node_access = main_components(n, &first_out, &edge_to, &edge_access);
        for (a, m) in node_access.iter_mut().zip(&main_node) {
            if *m {
                *a |= MAIN_ROAD;
            }
        }
        drop(main_node);
        let index = SpatialIndex::build(&self.lat, &self.lon);

        let (mut lat, mut lon) = (self.lat, self.lon);
        lat.shrink_to_fit();
        lon.shrink_to_fit();
        seg_name.shrink_to_fit();
        let names = self.names.freeze();
        let mut g = RoadGraph {
            resolution,
            lat,
            lon,
            node_access,
            first_out,
            edge_to,
            edge_seg,
            edge_access,
            seg_len_m,
            seg_cells_start,
            cells,
            seg_name,
            names,
            index,
            stats: GraphStats::default(),
        };
        g.stats = GraphStats {
            ways: ways_used,
            nodes: g.lat.len(),
            edges: g.edge_to.len(),
            segments: g.seg_len_m.len(),
            segment_cell_refs: g.cells.len(),
            memory_bytes: g.memory_bytes(),
            build_ms: started.elapsed().as_millis() as u64,
            parse_ms: 0,
        };
        g
    }
}

/// Cells crossed by each segment, sampled every quarter of a hex edge.
fn segment_cells(
    lat: &[f32],
    lon: &[f32],
    seg_a: &[u32],
    seg_b: &[u32],
    res: Resolution,
) -> (Vec<u32>, Vec<u64>) {
    let step_m = res.edge_length_m() / 4.0;
    let threads = std::thread::available_parallelism()
        .map(std::num::NonZeroUsize::get)
        .unwrap_or(1)
        .clamp(1, 8);
    let chunk = seg_a.len().div_ceil(threads).max(1);
    let parts: Vec<(Vec<u32>, Vec<u64>)> = std::thread::scope(|s| {
        let handles: Vec<_> = seg_a
            .chunks(chunk)
            .zip(seg_b.chunks(chunk))
            .map(|(ca, cb)| {
                s.spawn(move || {
                    let mut counts = Vec::with_capacity(ca.len());
                    let mut cells = Vec::with_capacity(ca.len() * 2);
                    let mut local: Vec<u64> = Vec::with_capacity(8);
                    for (&a, &b) in ca.iter().zip(cb) {
                        let (a, b) = (a as usize, b as usize);
                        let (la, oa) = (f64::from(lat[a]), f64::from(lon[a]));
                        let (lb, ob) = (f64::from(lat[b]), f64::from(lon[b]));
                        let len = haversine_m(la, oa, lb, ob);
                        let steps = ((len / step_m).ceil() as usize).max(1);
                        local.clear();
                        for i in 0..=steps {
                            let f = i as f64 / steps as f64;
                            let c = LatLng::new(la + (lb - la) * f, oa + (ob - oa) * f)
                                .map(|ll| u64::from(ll.to_cell(res)));
                            if let Ok(c) = c {
                                if !local.contains(&c) {
                                    local.push(c);
                                }
                            }
                        }
                        // Every cell is kept: dropping any would hide its water
                        // from routing.
                        counts.push(u32::try_from(local.len()).expect("too many cells"));
                        cells.extend(&local);
                    }
                    (counts, cells)
                })
            })
            .collect();
        handles
            .into_iter()
            .map(|h| h.join().expect("segment cell worker panicked"))
            .collect()
    });
    let mut start = Vec::with_capacity(seg_a.len() + 1);
    let mut cells = Vec::with_capacity(parts.iter().map(|p| p.1.len()).sum());
    start.push(0u32);
    for (counts, part_cells) in parts {
        let mut acc = *start.last().unwrap_or(&0);
        for c in counts {
            acc += c;
            start.push(acc);
        }
        cells.extend(part_cells);
    }
    cells.shrink_to_fit();
    (start, cells)
}

/// Per node, the vehicles for which it lies in that vehicle's largest
/// connected component (treating edges as undirected).
fn main_components(n: usize, first_out: &[u32], edge_to: &[u32], edge_access: &[u8]) -> Vec<u8> {
    let mut out = vec![0u8; n];
    let mut parent: Vec<u32> = Vec::with_capacity(n);
    for v in Vehicle::ALL {
        let bit = v.bit();
        parent.clear();
        parent.extend(0..n as u32);
        let mut touched = vec![false; n];
        for u in 0..n {
            for e in first_out[u] as usize..first_out[u + 1] as usize {
                if edge_access[e] & bit != 0 {
                    let w = edge_to[e] as usize;
                    touched[u] = true;
                    touched[w] = true;
                    let (ru, rw) = (find(&mut parent, u as u32), find(&mut parent, w as u32));
                    if ru != rw {
                        parent[ru as usize] = rw;
                    }
                }
            }
        }
        let mut size: HashMap<u32, u32> = HashMap::new();
        for (u, _) in touched.iter().enumerate().filter(|(_, t)| **t) {
            *size.entry(find(&mut parent, u as u32)).or_default() += 1;
        }
        if let Some((&root, _)) = size
            .iter()
            .max_by_key(|(r, s)| (**s, std::cmp::Reverse(**r)))
        {
            for (u, slot) in out.iter_mut().enumerate() {
                if touched[u] && find(&mut parent, u as u32) == root {
                    *slot |= bit;
                }
            }
        }
    }
    out
}

fn find(parent: &mut [u32], mut x: u32) -> u32 {
    while parent[x as usize] != x {
        let p = parent[x as usize];
        parent[x as usize] = parent[p as usize];
        x = p;
    }
    x
}

/// A way as read from the PBF: OSM node ids, forward and backward access,
/// main-road flag and name id.
type ParsedWay = (Vec<i64>, u8, u8, bool, Option<u32>);

/// Distance in metres from the origin to the segment `a`-`b`, all in local
/// planar metres.
fn point_segment_m(a: (f64, f64), b: (f64, f64)) -> f64 {
    let (dx, dy) = (b.0 - a.0, b.1 - a.1);
    let len2 = dx * dx + dy * dy;
    let t = if len2 > 0.0 {
        (-(a.0 * dx + a.1 * dy) / len2).clamp(0.0, 1.0)
    } else {
        0.0
    };
    let (px, py) = (a.0 + t * dx, a.1 + t * dy);
    (px * px + py * py).sqrt()
}

/// A uniform lat/lon bucket grid for nearest-node queries.
#[derive(Clone, Debug, Default)]
struct SpatialIndex {
    min_lat: f64,
    min_lon: f64,
    nx: usize,
    ny: usize,
    start: Vec<u32>,
    nodes: Vec<u32>,
}

impl SpatialIndex {
    fn build(lat: &[f32], lon: &[f32]) -> Self {
        if lat.is_empty() {
            return SpatialIndex::default();
        }
        let fold = |v: &[f32]| {
            v.iter().fold((f64::MAX, f64::MIN), |(lo, hi), &x| {
                (lo.min(f64::from(x)), hi.max(f64::from(x)))
            })
        };
        let (min_lat, max_lat) = fold(lat);
        let (min_lon, max_lon) = fold(lon);
        let nx = (((max_lon - min_lon) / BUCKET_DEG) as usize) + 1;
        let ny = (((max_lat - min_lat) / BUCKET_DEG) as usize) + 1;
        let mut idx = SpatialIndex {
            min_lat,
            min_lon,
            nx,
            ny,
            start: vec![0; nx * ny + 1],
            nodes: vec![0; lat.len()],
        };
        let bucket_of: Vec<usize> = (0..lat.len())
            .map(|i| idx.bucket(f64::from(lat[i]), f64::from(lon[i])))
            .collect();
        for &b in &bucket_of {
            idx.start[b + 1] += 1;
        }
        for i in 0..nx * ny {
            idx.start[i + 1] += idx.start[i];
        }
        let mut fill = idx.start.clone();
        for (i, &b) in bucket_of.iter().enumerate() {
            idx.nodes[fill[b] as usize] = i as u32;
            fill[b] += 1;
        }
        idx
    }

    fn coords(&self, lat: f64, lon: f64) -> (i64, i64) {
        (
            ((lon - self.min_lon) / BUCKET_DEG).floor() as i64,
            ((lat - self.min_lat) / BUCKET_DEG).floor() as i64,
        )
    }

    fn bucket(&self, lat: f64, lon: f64) -> usize {
        let (x, y) = self.coords(lat, lon);
        let x = x.clamp(0, self.nx as i64 - 1) as usize;
        let y = y.clamp(0, self.ny as i64 - 1) as usize;
        y * self.nx + x
    }

    fn memory_bytes(&self) -> usize {
        (self.start.capacity() + self.nodes.capacity()) * 4
    }
}

/// Load and size figures for a graph.
#[derive(Clone, Copy, Debug, Default, Serialize)]
pub struct GraphStats {
    /// Ways kept.
    pub ways: usize,
    /// Graph nodes.
    pub nodes: usize,
    /// Directed edges.
    pub edges: usize,
    /// Undirected segments.
    pub segments: usize,
    /// Total segment-to-cell references.
    pub segment_cell_refs: usize,
    /// Heap bytes held by the graph's vectors.
    pub memory_bytes: usize,
    /// Milliseconds spent decoding the PBF.
    pub parse_ms: u64,
    /// Milliseconds spent building the graph.
    pub build_ms: u64,
}

/// Errors loading a graph.
#[derive(Debug, thiserror::Error)]
pub enum GraphError {
    /// The PBF could not be read.
    #[error("cannot read OSM extract {path}: {source}")]
    Read {
        /// File path.
        path: String,
        /// Underlying error.
        source: osmpbf::Error,
    },
    /// The extract held no usable road.
    #[error("OSM extract {0} contains no usable highway ways")]
    Empty(String),
}

/// The compact directed road graph.
#[derive(Clone, Debug)]
pub struct RoadGraph {
    resolution: Resolution,
    lat: Vec<f32>,
    lon: Vec<f32>,
    node_access: Vec<u8>,
    first_out: Vec<u32>,
    edge_to: Vec<u32>,
    edge_seg: Vec<u32>,
    edge_access: Vec<u8>,
    seg_len_m: Vec<f32>,
    seg_cells_start: Vec<u32>,
    cells: Vec<u64>,
    seg_name: Vec<u32>,
    names: NameTable,
    index: SpatialIndex,
    stats: GraphStats,
}

impl RoadGraph {
    /// Load an OSM PBF extract of highway ways.
    pub fn load_pbf(path: &Path, resolution: Resolution) -> Result<Self, GraphError> {
        let started = Instant::now();
        let display = path.display().to_string();
        let mut coords: HashMap<i64, (f64, f64)> = HashMap::new();
        let mut ways: Vec<ParsedWay> = Vec::new();
        let mut names = NameTable::default();
        let reader = osmpbf::ElementReader::from_path(path).map_err(|source| GraphError::Read {
            path: display.clone(),
            source,
        })?;
        reader
            .for_each(|el| match el {
                osmpbf::Element::DenseNode(n) => {
                    coords.insert(n.id(), (n.lat(), n.lon()));
                }
                osmpbf::Element::Node(n) => {
                    coords.insert(n.id(), (n.lat(), n.lon()));
                }
                osmpbf::Element::Way(w) => {
                    if let Some(tags) = WayTags::from_tags(w.tags()) {
                        if let Some((f, b)) = way_access(&tags) {
                            let name = tags.display_name().map(|n| names.intern(n));
                            ways.push((
                                w.refs().collect(),
                                f,
                                b,
                                is_main_highway(tags.highway),
                                name,
                            ));
                        }
                    }
                }
                osmpbf::Element::Relation(_) => {}
            })
            .map_err(|source| GraphError::Read {
                path: display.clone(),
                source,
            })?;
        let parse_ms = started.elapsed().as_millis() as u64;

        let mut builder = GraphBuilder {
            names,
            ..GraphBuilder::default()
        };
        let mut ids: HashMap<i64, u32> = HashMap::with_capacity(coords.len());
        for (refs, f, b, main, name) in ways {
            let mut nodes = Vec::with_capacity(refs.len());
            for r in refs {
                let Some(&(lat, lon)) = coords.get(&r) else {
                    // A way clipped at the extract boundary: keep what is inside.
                    if nodes.len() >= 2 {
                        builder.push_way(std::mem::take(&mut nodes), f, b, main, name);
                    }
                    nodes.clear();
                    continue;
                };
                let idx = *ids.entry(r).or_insert_with(|| builder.add_node(lat, lon));
                nodes.push(idx);
            }
            builder.push_way(nodes, f, b, main, name);
        }
        drop(coords);
        drop(ids);
        if builder.ways.is_empty() {
            return Err(GraphError::Empty(display));
        }
        let mut g = builder.build(resolution);
        g.stats.parse_ms = parse_ms;
        Ok(g)
    }

    /// The H3 resolution of the segment cells.
    #[must_use]
    pub fn resolution(&self) -> Resolution {
        self.resolution
    }

    /// Size and timing figures.
    #[must_use]
    pub fn stats(&self) -> GraphStats {
        self.stats
    }

    /// Number of nodes.
    #[must_use]
    pub fn node_count(&self) -> usize {
        self.lat.len()
    }

    /// Number of directed edges.
    #[must_use]
    pub fn edge_count(&self) -> usize {
        self.edge_to.len()
    }

    /// Latitude and longitude of a node.
    #[must_use]
    pub fn coord(&self, node: u32) -> (f64, f64) {
        let i = node as usize;
        (f64::from(self.lat[i]), f64::from(self.lon[i]))
    }

    /// Out-edge ids of a node.
    #[must_use]
    pub fn out_edges(&self, node: u32) -> Range<usize> {
        let i = node as usize;
        self.first_out[i] as usize..self.first_out[i + 1] as usize
    }

    /// Head node of an edge.
    #[must_use]
    pub fn edge_to(&self, edge: usize) -> u32 {
        self.edge_to[edge]
    }

    /// Vehicles allowed on an edge.
    #[must_use]
    pub fn edge_access(&self, edge: usize) -> u8 {
        self.edge_access[edge]
    }

    /// Segment id of an edge (shared by both directions).
    #[must_use]
    pub fn edge_segment(&self, edge: usize) -> u32 {
        self.edge_seg[edge]
    }

    /// Length of a segment in metres.
    #[must_use]
    pub fn segment_len_m(&self, seg: u32) -> f64 {
        f64::from(self.seg_len_m[seg as usize])
    }

    /// H3 cells (raw u64) that a segment passes through.
    #[must_use]
    pub fn segment_cells(&self, seg: u32) -> &[u64] {
        let s = seg as usize;
        &self.cells[self.seg_cells_start[s] as usize..self.seg_cells_start[s + 1] as usize]
    }

    /// Number of segments.
    #[must_use]
    pub fn segment_count(&self) -> usize {
        self.seg_len_m.len()
    }

    /// The nearest node, within `max_m` metres, that `vehicle` can use and that
    /// lies in its main connected component. Returns the node and its distance.
    #[must_use]
    pub fn nearest_node(
        &self,
        lat: f64,
        lon: f64,
        vehicle: Vehicle,
        max_m: f64,
    ) -> Option<(u32, f64)> {
        let idx = &self.index;
        if idx.nodes.is_empty() {
            return None;
        }
        let bit = vehicle.bit();
        let (qx, qy) = idx.coords(lat, lon);
        let cos = lat.to_radians().cos().abs().max(0.1);
        let bucket_m = BUCKET_DEG * 111_195.0 * cos;
        let max_r = (max_m / bucket_m).ceil() as i64 + 1;
        let mut best: Option<(u32, f64)> = None;
        for r in 0..=max_r {
            for y in (qy - r)..=(qy + r) {
                if y < 0 || y >= idx.ny as i64 {
                    continue;
                }
                let edge_row = y == qy - r || y == qy + r;
                let mut x = qx - r;
                while x <= qx + r {
                    if x >= 0 && x < idx.nx as i64 {
                        let b = y as usize * idx.nx + x as usize;
                        for &node in &idx.nodes[idx.start[b] as usize..idx.start[b + 1] as usize] {
                            if self.node_access[node as usize] & bit == 0 {
                                continue;
                            }
                            let (nl, no) = self.coord(node);
                            let d = haversine_m(lat, lon, nl, no);
                            if d <= max_m && best.is_none_or(|(_, bd)| d < bd) {
                                best = Some((node, d));
                            }
                        }
                    }
                    // Interior rows only need their two end buckets.
                    x += if edge_row || r == 0 { 1 } else { 2 * r };
                }
            }
            if let Some((_, d)) = best {
                if d <= r as f64 * bucket_m {
                    break;
                }
            }
        }
        best
    }

    /// True when the node lies on a main road (see the module docs).
    #[must_use]
    pub fn is_main_road(&self, node: u32) -> bool {
        self.node_access[node as usize] & MAIN_ROAD != 0
    }

    /// Up to `k` main-road nodes that `vehicle` can use from its main
    /// component, nearest first, within `max_m` metres of the point and at
    /// least `min_sep_m` apart, so the candidates are different roads or
    /// different stretches of one. Returns each node with its distance.
    #[must_use]
    pub fn nearest_main_roads(
        &self,
        lat: f64,
        lon: f64,
        vehicle: Vehicle,
        max_m: f64,
        k: usize,
        min_sep_m: f64,
    ) -> Vec<(u32, f64)> {
        let idx = &self.index;
        if idx.nodes.is_empty() || k == 0 {
            return Vec::new();
        }
        let want = vehicle.bit() | MAIN_ROAD;
        let (qx, qy) = idx.coords(lat, lon);
        let cos = lat.to_radians().cos().abs().max(0.1);
        let bucket_m = BUCKET_DEG * 111_195.0 * cos;
        let r = (max_m / bucket_m).ceil() as i64 + 1;
        let mut found: Vec<(u32, f64)> = Vec::new();
        for y in (qy - r).max(0)..=(qy + r).min(idx.ny as i64 - 1) {
            for x in (qx - r).max(0)..=(qx + r).min(idx.nx as i64 - 1) {
                let b = y as usize * idx.nx + x as usize;
                for &node in &idx.nodes[idx.start[b] as usize..idx.start[b + 1] as usize] {
                    if self.node_access[node as usize] & want != want {
                        continue;
                    }
                    let (nl, no) = self.coord(node);
                    let d = haversine_m(lat, lon, nl, no);
                    if d <= max_m {
                        found.push((node, d));
                    }
                }
            }
        }
        found.sort_by(|a, b| a.1.total_cmp(&b.1).then(a.0.cmp(&b.0)));
        let mut out: Vec<(u32, f64)> = Vec::with_capacity(k);
        for (node, d) in found {
            let (nl, no) = self.coord(node);
            if out.iter().all(|(o, _)| {
                let (ol, oo) = self.coord(*o);
                haversine_m(nl, no, ol, oo) >= min_sep_m
            }) {
                out.push((node, d));
                if out.len() == k {
                    break;
                }
            }
        }
        out
    }

    /// The name of the named road nearest to a point, within `max_m`
    /// metres, with its distance in metres. Distances are to the road's
    /// segments, not only its nodes. Only used to label places for people;
    /// routing never reads names.
    #[must_use]
    pub fn nearest_road_name(&self, lat: f64, lon: f64, max_m: f64) -> Option<(&str, f64)> {
        let idx = &self.index;
        if idx.nodes.is_empty() {
            return None;
        }
        let (qx, qy) = idx.coords(lat, lon);
        let cos = lat.to_radians().cos().abs().max(0.1);
        let bucket_m = BUCKET_DEG * 111_195.0 * cos;
        let max_r = (max_m / bucket_m).ceil() as i64 + 1;
        // Local planar metres around the query point.
        let xy = |la: f64, lo: f64| ((lo - lon) * 111_195.0 * cos, (la - lat) * 111_195.0);
        let mut best: Option<(u32, f64)> = None;
        let mut settle_at: Option<i64> = None;
        for r in 0..=max_r {
            for y in (qy - r)..=(qy + r) {
                if y < 0 || y >= idx.ny as i64 {
                    continue;
                }
                let edge_row = y == qy - r || y == qy + r;
                let mut x = qx - r;
                while x <= qx + r {
                    if x >= 0 && x < idx.nx as i64 {
                        let b = y as usize * idx.nx + x as usize;
                        for &node in &idx.nodes[idx.start[b] as usize..idx.start[b + 1] as usize] {
                            for e in self.out_edges(node) {
                                let seg = self.edge_seg[e] as usize;
                                let name = self.seg_name[seg];
                                if name == NO_NAME {
                                    continue;
                                }
                                let (al, ao) = self.coord(node);
                                let (bl, bo) = self.coord(self.edge_to[e]);
                                let d = point_segment_m(xy(al, ao), xy(bl, bo));
                                if d <= max_m && best.is_none_or(|(_, bd)| d < bd) {
                                    best = Some((name, d));
                                }
                            }
                        }
                    }
                    x += if edge_row || r == 0 { 1 } else { 2 * r };
                }
            }
            // A segment is found from its tail node, which can lie one ring
            // further out than the nearest point of the segment, so look one
            // ring past the first ring that could hold the best.
            if let Some((_, d)) = best {
                if settle_at.is_none() && d <= r as f64 * bucket_m {
                    settle_at = Some(r + 1);
                }
            }
            if settle_at.is_some_and(|s| r >= s) {
                break;
            }
        }
        best.and_then(|(id, d)| self.names.get(id).map(|n| (n, d)))
    }

    /// Heap bytes held by the graph's vectors.
    #[must_use]
    pub fn memory_bytes(&self) -> usize {
        self.lat.capacity() * 4
            + self.lon.capacity() * 4
            + self.node_access.capacity()
            + self.first_out.capacity() * 4
            + self.edge_to.capacity() * 4
            + self.edge_seg.capacity() * 4
            + self.edge_access.capacity()
            + self.seg_len_m.capacity() * 4
            + self.seg_cells_start.capacity() * 4
            + self.cells.capacity() * 8
            + self.seg_name.capacity() * 4
            + self.names.memory_bytes()
            + self.index.memory_bytes()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tags<'a>(pairs: &[(&'a str, &'a str)]) -> WayTags<'a> {
        WayTags::from_tags(pairs.iter().copied()).unwrap()
    }

    #[test]
    fn pedestrians_avoid_motorways_and_vehicles_avoid_footways() {
        let (f, _) = way_access(&tags(&[("highway", "motorway")])).unwrap();
        assert_eq!(f & PED, 0);
        assert_ne!(f & CAR, 0);
        assert_eq!(f & TWO, 0);
        let (f, b) = way_access(&tags(&[("highway", "footway")])).unwrap();
        assert_eq!((f, b), (PED, PED));
        let (f, _) = way_access(&tags(&[("highway", "residential")])).unwrap();
        assert_eq!(f, PED | MOTOR);
        assert!(way_access(&tags(&[("highway", "construction")])).is_none());
        assert!(WayTags::from_tags([("name", "x")]).is_none());
    }

    #[test]
    fn oneway_rules() {
        let (f, b) = way_access(&tags(&[("highway", "primary"), ("oneway", "yes")])).unwrap();
        assert_eq!(f, PED | MOTOR);
        assert_eq!(b, PED);
        let (f, b) = way_access(&tags(&[("highway", "primary"), ("oneway", "-1")])).unwrap();
        assert_eq!((f, b), (PED, PED | MOTOR));
        let (_, b) =
            way_access(&tags(&[("highway", "primary"), ("junction", "roundabout")])).unwrap();
        assert_eq!(b, PED);
        let (_, b) = way_access(&tags(&[("highway", "motorway"), ("oneway", "no")])).unwrap();
        assert_eq!(b, CAR | BUS | TRUCK);
        let (_, b) = way_access(&tags(&[
            ("highway", "primary"),
            ("oneway", "yes"),
            ("oneway:bus", "no"),
        ]))
        .unwrap();
        assert_eq!(b, PED | BUS);
    }

    #[test]
    fn access_tags() {
        assert!(way_access(&tags(&[("highway", "service"), ("access", "private")])).is_none());
        let (f, _) = way_access(&tags(&[
            ("highway", "service"),
            ("access", "no"),
            ("foot", "yes"),
        ]))
        .unwrap();
        assert_eq!(f, PED);
        let (f, _) = way_access(&tags(&[
            ("highway", "residential"),
            ("motor_vehicle", "no"),
        ]))
        .unwrap();
        assert_eq!(f, PED);
        let (f, _) = way_access(&tags(&[("highway", "primary"), ("foot", "no")])).unwrap();
        assert_eq!(f & PED, 0);
        let (f, _) = way_access(&tags(&[("highway", "primary"), ("motorcycle", "no")])).unwrap();
        assert_eq!(f & TWO, 0);
    }

    #[test]
    fn trucks_use_service_roads_but_not_living_streets_and_honour_hgv() {
        let (f, _) = way_access(&tags(&[("highway", "service")])).unwrap();
        assert_ne!(f & TRUCK, 0);
        let (f, _) = way_access(&tags(&[("highway", "living_street")])).unwrap();
        assert_eq!(f & TRUCK, 0);
        let (f, _) = way_access(&tags(&[("highway", "motorway")])).unwrap();
        assert_ne!(f & TRUCK, 0);
        let (f, _) = way_access(&tags(&[("highway", "residential"), ("hgv", "no")])).unwrap();
        assert_eq!(f & TRUCK, 0);
        assert_ne!(f & CAR, 0);
        assert!(is_main_highway("primary") && is_main_highway("trunk_link"));
        assert!(!is_main_highway("residential") && !is_main_highway("service"));
    }

    #[test]
    fn main_road_candidates_are_nearest_first_and_spread_out() {
        // A residential street with a primary road 400 m north of it.
        let mut b = GraphBuilder::new();
        let res: Vec<u32> = (0..11)
            .map(|i| b.add_node(13.0, 80.20 + 0.001 * f64::from(i)))
            .collect();
        let main: Vec<u32> = (0..11)
            .map(|i| b.add_node(13.0036, 80.20 + 0.001 * f64::from(i)))
            .collect();
        let all = PED | MOTOR;
        b.add_way(res.clone(), all, all);
        b.add_way_class(main.clone(), all, all, true);
        b.add_way(vec![res[0], main[0]], all, all);
        b.add_way(vec![res[10], main[10]], all, all);
        let g = b.build(Resolution::Ten);
        assert!(g.is_main_road(main[5]) && !g.is_main_road(res[5]));
        let c = g.nearest_main_roads(13.0, 80.205, Vehicle::Truck, 3000.0, 3, 300.0);
        assert_eq!(c.len(), 3);
        assert_eq!(c[0].0, main[5]);
        assert!((c[0].1 - 400.0).abs() < 10.0, "{}", c[0].1);
        for w in c.windows(2) {
            assert!(w[0].1 <= w[1].1);
        }
        assert!(g
            .nearest_main_roads(13.0, 80.205, Vehicle::Truck, 300.0, 3, 300.0)
            .is_empty());
    }

    fn ladder() -> RoadGraph {
        // Two parallel east-west streets joined at both ends, about 1 km long.
        let mut b = GraphBuilder::new();
        let s: Vec<u32> = (0..11)
            .map(|i| b.add_node(13.0, 80.20 + 0.001 * f64::from(i)))
            .collect();
        let n: Vec<u32> = (0..11)
            .map(|i| b.add_node(13.003, 80.20 + 0.001 * f64::from(i)))
            .collect();
        let all = PED | MOTOR;
        b.add_way(s.clone(), all, all);
        b.add_way(n.clone(), all, all);
        b.add_way(vec![s[0], n[0]], all, all);
        b.add_way(vec![s[10], n[10]], all, all);
        // An island that must never be snapped to.
        let i1 = b.add_node(13.0015, 80.205);
        let i2 = b.add_node(13.0016, 80.205);
        b.add_way(vec![i1, i2], all, all);
        b.build(Resolution::Ten)
    }

    #[test]
    fn builds_csr_and_cells() {
        let g = ladder();
        assert_eq!(g.node_count(), 24);
        assert_eq!(g.segment_count(), 23);
        assert_eq!(g.edge_count(), 46);
        for e in g.out_edges(0) {
            assert!(g.segment_len_m(g.edge_segment(e)) > 50.0);
            assert!(!g.segment_cells(g.edge_segment(e)).is_empty());
        }
        assert!(g.memory_bytes() > 0);
    }

    #[test]
    fn long_segments_keep_every_cell() {
        // Regression: cell counts were stored in a u8, so a segment crossing
        // more than 255 cells (5 km at resolution 12) silently lost the rest,
        // and water in them was invisible to routing.
        let mut b = GraphBuilder::new();
        let a = b.add_node(13.0, 80.20);
        let z = b.add_node(13.0, 80.25);
        b.add_way(vec![a, z], PED | MOTOR, PED | MOTOR);
        let g = b.build(Resolution::Twelve);
        let cells = g.segment_cells(0);
        assert!(cells.len() > 255, "{}", cells.len());
        let end = u64::from(
            LatLng::new(13.0, 80.25)
                .unwrap()
                .to_cell(Resolution::Twelve),
        );
        assert!(cells.contains(&end));
    }

    #[test]
    fn nearest_road_name_measures_to_the_segment_and_skips_unnamed_ways() {
        let all = PED | MOTOR;
        let mut b = GraphBuilder::new();
        // A named street running east, 1 km long, with nodes only at its ends.
        let w = b.add_node(13.0, 80.20);
        let e = b.add_node(13.0, 80.21);
        b.add_named_way(vec![w, e], all, all, false, Some("Velachery Main Road"));
        // An unnamed lane closer to the query point.
        let l1 = b.add_node(13.0004, 80.2049);
        let l2 = b.add_node(13.0004, 80.2051);
        b.add_named_way(vec![l1, l2], all, all, false, None);
        // A one-way named road further north.
        let n1 = b.add_node(13.003, 80.20);
        let n2 = b.add_node(13.003, 80.21);
        b.add_named_way(vec![n1, n2], CAR, 0, true, Some("Taramani Link Road"));
        let g = b.build(Resolution::Ten);
        // 0.0005 degrees north of the middle of the first street: about 56 m,
        // though both of its nodes are over 500 m away.
        let (name, d) = g.nearest_road_name(13.0005, 80.205, 300.0).unwrap();
        assert_eq!(name, "Velachery Main Road");
        assert!((d - 55.6).abs() < 1.0, "{d}");
        let (name, _) = g.nearest_road_name(13.0029, 80.205, 300.0).unwrap();
        assert_eq!(name, "Taramani Link Road");
        assert!(g.nearest_road_name(13.02, 80.205, 300.0).is_none());
        assert!(g.memory_bytes() > 0);
    }

    #[test]
    fn display_name_prefers_english_then_name_then_ref() {
        let t = tags(&[
            ("highway", "primary"),
            ("name", "வேளச்சேரி"),
            ("name:en", "Velachery Road"),
        ]);
        assert_eq!(t.display_name(), Some("Velachery Road"));
        let t = tags(&[("highway", "trunk"), ("ref", "NH 32"), ("name", " ")]);
        assert_eq!(t.display_name(), Some("NH 32"));
        assert_eq!(tags(&[("highway", "service")]).display_name(), None);
        let t = tags(&[
            ("highway", "trunk"),
            ("name", "Kathipara Flyover"),
            ("bridge", "yes"),
        ]);
        assert_eq!(t.display_name(), None);
        let t = tags(&[
            ("highway", "primary"),
            ("name", "Madley Road"),
            ("tunnel", "yes"),
        ]);
        assert_eq!(t.display_name(), Some("Madley Road"));
    }

    #[test]
    fn snapping_skips_disconnected_islands() {
        let g = ladder();
        let (node, d) = g
            .nearest_node(13.0015, 80.205, Vehicle::Car, 1000.0)
            .unwrap();
        assert!(node < 22, "snapped to island node {node}");
        assert!(d > 100.0);
        assert!(g.nearest_node(13.5, 80.205, Vehicle::Car, 1000.0).is_none());
    }
}
