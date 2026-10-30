//! Critical sites: data centres first, with hospitals, telephone exchanges,
//! substations and cable landing stations on the same model.
//!
//! A site has three questions in a flood, and each is answered from what the
//! engine already holds:
//!
//! 1. **Is water coming at it?** A watch zone of rings around the site (300 m
//!    and 1 km by default) over the live map: the deepest blocking depth in
//!    each ring, how much of the ring has recent evidence, how fresh it is,
//!    whether the level is rising or falling, and `unknown` where evidence is
//!    thin. Nothing here ever says "safe": `under` means the cells with
//!    recent reports are below the threshold, and the rest are unknown.
//! 2. **Can people and supplies reach it?** Water-aware routes, planned by
//!    [`crate::routing::plan_route`], from the site to the nearest main road
//!    and to the nearest fuel station, for a diesel tanker, a two-wheeler and
//!    a car.
//! 3. **Where should the next one go?** The risk ledger
//!    ([`crate::ledger`]) for the site's area and the areas around it.
//!
//! Sites come from OpenStreetMap (`scripts/extract_sites.py` writes
//! `engine/assets/sites-osm.json`, embedded here) or from an operator through
//! the gateway's developer API. None is typed by hand. An operator's site is
//! only ever shown to its owner.

use crate::analytics::{drain_estimate, Trend};
use crate::config::Config;
use crate::fusion::{CellView, Grid, HistoryPoint, WaterMap};
use crate::geo::haversine_m;
use crate::graph::RoadGraph;
use crate::ledger::Ledger;
use crate::routing::{blocking_estimate, plan_route, RouteError, RouteKind, RoutePolicy};
use crate::vehicle::Vehicle;
use chrono::{DateTime, Utc};
use h3o::{CellIndex, LatLng, Resolution};
use parking_lot::{Mutex, RwLock};
use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;
use std::fmt;
use std::hash::{BuildHasher, Hasher};
use std::path::{Path, PathBuf};
use std::str::FromStr;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};

/// The OpenStreetMap sites and fuel stations, as written by
/// `scripts/extract_sites.py`.
pub const EMBEDDED_OSM: &str = include_str!("../assets/sites-osm.json");

/// What kind of place a site is.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum SiteKind {
    /// A data centre.
    DataCentre,
    /// A hospital.
    Hospital,
    /// A telephone exchange.
    TelephoneExchange,
    /// An electricity substation.
    Substation,
    /// A subsea cable landing station.
    CableLandingStation,
}

impl SiteKind {
    /// Every kind.
    pub const ALL: [SiteKind; 5] = [
        SiteKind::DataCentre,
        SiteKind::Hospital,
        SiteKind::TelephoneExchange,
        SiteKind::Substation,
        SiteKind::CableLandingStation,
    ];

    /// The wire name.
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            SiteKind::DataCentre => "data_centre",
            SiteKind::Hospital => "hospital",
            SiteKind::TelephoneExchange => "telephone_exchange",
            SiteKind::Substation => "substation",
            SiteKind::CableLandingStation => "cable_landing_station",
        }
    }
}

impl fmt::Display for SiteKind {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

impl FromStr for SiteKind {
    type Err = String;
    fn from_str(s: &str) -> Result<Self, String> {
        SiteKind::ALL
            .into_iter()
            .find(|k| k.as_str() == s)
            .ok_or_else(|| {
                format!("unknown kind '{s}', expected data_centre, hospital, telephone_exchange, substation or cable_landing_station")
            })
    }
}

/// Where a site came from.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum SiteSource {
    /// OpenStreetMap.
    Osm,
    /// Added by an operator through the gateway.
    Operator,
}

/// One critical site.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct Site {
    /// `osm:way:123` or `op_<16 hex>`.
    pub id: String,
    /// What it is.
    pub kind: SiteKind,
    /// Its name, when known.
    pub name: Option<String>,
    /// Its operator, when known.
    pub operator: Option<String>,
    /// Latitude.
    pub lat: f64,
    /// Longitude.
    pub lon: f64,
    /// The covered city holding it.
    pub city: String,
    /// Where it came from.
    pub source: SiteSource,
    /// The OpenStreetMap object, such as `way/123`.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub osm: Option<String>,
    /// The API key id that added it.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub owner: Option<String>,
    /// When it was added.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub added_at: Option<DateTime<Utc>>,
}

/// A fuel station, a supply point for access routes.
#[derive(Clone, Debug, PartialEq, Serialize)]
pub struct FuelPoint {
    /// `osm:node:123`.
    pub id: String,
    /// Name or brand, when known.
    pub name: Option<String>,
    /// `fuel:diesel`, when tagged.
    pub diesel: Option<bool>,
    /// Latitude.
    pub lat: f64,
    /// Longitude.
    pub lon: f64,
}

#[derive(Deserialize)]
struct AssetFile {
    format: String,
    version: u32,
    attribution: String,
    #[serde(default)]
    extracts: BTreeMap<String, AssetExtract>,
    sites: Vec<AssetSite>,
    fuel: Vec<AssetFuel>,
}

#[derive(Deserialize)]
struct AssetExtract {
    osm_data_until: String,
}

#[derive(Deserialize)]
struct AssetSite {
    id: String,
    kind: SiteKind,
    lat: f64,
    lon: f64,
    name: Option<String>,
    operator: Option<String>,
    osm: Option<String>,
}

#[derive(Deserialize)]
struct AssetFuel {
    id: String,
    lat: f64,
    lon: f64,
    name: Option<String>,
    brand: Option<String>,
    diesel: Option<bool>,
}

/// Why an operator site was refused.
#[derive(Clone, Debug, PartialEq, thiserror::Error)]
pub enum SiteError {
    /// Invalid input; the message names the field.
    #[error("{0}")]
    Invalid(String),
    /// The owner, or the engine, holds its maximum.
    #[error("{0}")]
    Full(String),
    /// No such site for this owner.
    #[error("no such site")]
    NotFound,
    /// OpenStreetMap sites are not deleted here.
    #[error("OpenStreetMap sites cannot be deleted here; correct them in OpenStreetMap and rerun scripts/extract_sites.py")]
    NotOperator,
}

/// The OpenStreetMap file's provenance.
#[derive(Clone, Debug, Default, PartialEq, Serialize)]
pub struct OsmInfo {
    /// The attribution OpenStreetMap's licence asks for.
    pub attribution: String,
    /// The oldest "data until" time of the extracts used.
    pub data_until: Option<String>,
}

/// Every site the engine knows.
#[derive(Debug, Default)]
pub struct SiteRegistry {
    osm: Vec<Site>,
    /// Fuel stations, per city.
    fuel: BTreeMap<String, Vec<FuelPoint>>,
    operator: RwLock<Vec<Site>>,
    /// Provenance of the OpenStreetMap sites.
    pub info: OsmInfo,
    /// Why the OpenStreetMap file could not be read, if it could not.
    pub load_error: Option<String>,
}

fn printable(s: &str, max: usize) -> bool {
    !s.trim().is_empty() && s.chars().count() <= max && !s.chars().any(char::is_control)
}

/// True when `owner` is a usable owner id.
#[must_use]
pub fn valid_owner(owner: &str) -> bool {
    !owner.is_empty()
        && owner.len() <= 64
        && owner
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || c == '_' || c == '-')
}

static ID_COUNTER: AtomicU64 = AtomicU64::new(0);

fn new_operator_id(owner: &str, name: &str) -> String {
    // RandomState is seeded randomly per process; mixed with the time and a
    // counter it gives ids that do not repeat across restarts.
    let mut h = std::collections::hash_map::RandomState::new().build_hasher();
    h.write(owner.as_bytes());
    h.write(name.as_bytes());
    h.write_i64(Utc::now().timestamp_nanos_opt().unwrap_or_default());
    h.write_u64(ID_COUNTER.fetch_add(1, Ordering::Relaxed));
    format!("op_{:016x}", h.finish())
}

impl SiteRegistry {
    /// Load the OpenStreetMap sites (from `cfg.sites.osm_path`, else the
    /// embedded file) for the configured cities. A site or fuel station
    /// outside every city's box is left out. A file that cannot be read
    /// leaves the registry empty with `load_error` set.
    #[must_use]
    pub fn load(cfg: &Config) -> Self {
        let text = match &cfg.sites.osm_path {
            Some(p) => match std::fs::read_to_string(p) {
                Ok(t) => t,
                Err(e) => {
                    return SiteRegistry {
                        load_error: Some(format!("cannot read {}: {e}", p.display())),
                        ..SiteRegistry::default()
                    }
                }
            },
            None => EMBEDDED_OSM.to_string(),
        };
        match Self::from_json(&text, cfg) {
            Ok(r) => r,
            Err(e) => SiteRegistry {
                load_error: Some(e),
                ..SiteRegistry::default()
            },
        }
    }

    /// Parse an OpenStreetMap site file for the configured cities.
    pub fn from_json(text: &str, cfg: &Config) -> Result<Self, String> {
        let f: AssetFile = serde_json::from_str(text).map_err(|e| e.to_string())?;
        if f.format != "neermattam-sites-osm" || f.version != 1 {
            return Err(format!(
                "not a neermattam-sites-osm version 1 file (found {} {})",
                f.format, f.version
            ));
        }
        let mut reg = SiteRegistry {
            info: OsmInfo {
                attribution: f.attribution,
                data_until: f.extracts.values().map(|e| e.osm_data_until.clone()).min(),
            },
            ..SiteRegistry::default()
        };
        for s in f.sites {
            if !(s.lat.is_finite() && s.lon.is_finite()) {
                continue;
            }
            let Some(city) = cfg.city_at(s.lat, s.lon) else {
                continue;
            };
            reg.osm.push(Site {
                id: s.id,
                kind: s.kind,
                name: s.name,
                operator: s.operator,
                lat: s.lat,
                lon: s.lon,
                city: city.id.clone(),
                source: SiteSource::Osm,
                osm: s.osm,
                owner: None,
                added_at: None,
            });
        }
        for p in f.fuel {
            if let Some(city) = cfg.city_at(p.lat, p.lon) {
                reg.fuel
                    .entry(city.id.clone())
                    .or_default()
                    .push(FuelPoint {
                        id: p.id,
                        name: p.name.or(p.brand),
                        diesel: p.diesel,
                        lat: p.lat,
                        lon: p.lon,
                    });
            }
        }
        Ok(reg)
    }

    /// OpenStreetMap sites held.
    #[must_use]
    pub fn osm_len(&self) -> usize {
        self.osm.len()
    }

    /// Operator sites held.
    #[must_use]
    pub fn operator_len(&self) -> usize {
        self.operator.read().len()
    }

    /// Fuel stations in a city.
    #[must_use]
    pub fn fuel_in(&self, city: &str) -> &[FuelPoint] {
        self.fuel.get(city).map_or(&[], Vec::as_slice)
    }

    /// The sites in a city: OpenStreetMap ones and, with `owner`, that
    /// owner's ones. With `kind`, only that kind.
    #[must_use]
    pub fn list(&self, city: &str, kind: Option<SiteKind>, owner: Option<&str>) -> Vec<Site> {
        let keep = |s: &&Site| s.city == city && kind.is_none_or(|k| s.kind == k);
        let mut out: Vec<Site> = self.osm.iter().filter(keep).cloned().collect();
        if let Some(o) = owner {
            out.extend(
                self.operator
                    .read()
                    .iter()
                    .filter(|s| s.owner.as_deref() == Some(o))
                    .filter(keep)
                    .cloned(),
            );
        }
        out
    }

    /// Counts per kind in a city: OpenStreetMap sites, and the owner's.
    #[must_use]
    pub fn counts(&self, city: &str, owner: Option<&str>) -> BTreeMap<SiteKind, (usize, usize)> {
        let mut out: BTreeMap<SiteKind, (usize, usize)> = BTreeMap::new();
        for k in SiteKind::ALL {
            out.insert(k, (0, 0));
        }
        for s in self.osm.iter().filter(|s| s.city == city) {
            out.entry(s.kind).or_default().0 += 1;
        }
        if let Some(o) = owner {
            for s in self
                .operator
                .read()
                .iter()
                .filter(|s| s.city == city && s.owner.as_deref() == Some(o))
            {
                out.entry(s.kind).or_default().1 += 1;
            }
        }
        out
    }

    /// One site. An operator site is found only with its owner.
    #[must_use]
    pub fn get(&self, id: &str, owner: Option<&str>) -> Option<Site> {
        if id.starts_with("osm:") {
            return self.osm.iter().find(|s| s.id == id).cloned();
        }
        let o = owner?;
        self.operator
            .read()
            .iter()
            .find(|s| s.id == id && s.owner.as_deref() == Some(o))
            .cloned()
    }

    /// Add an operator site.
    #[allow(clippy::too_many_arguments)]
    pub fn add(
        &self,
        cfg: &Config,
        owner: &str,
        kind: SiteKind,
        name: &str,
        operator: Option<&str>,
        lat: f64,
        lon: f64,
        now: DateTime<Utc>,
    ) -> Result<Site, SiteError> {
        if !valid_owner(owner) {
            return Err(SiteError::Invalid(
                "owner must be 1 to 64 characters of A-Z, a-z, 0-9, _ and -".into(),
            ));
        }
        if !printable(name, 120) {
            return Err(SiteError::Invalid(
                "name must be 1 to 120 printable characters".into(),
            ));
        }
        if let Some(op) = operator {
            if !printable(op, 120) {
                return Err(SiteError::Invalid(
                    "operator must be 1 to 120 printable characters".into(),
                ));
            }
        }
        if !(lat.is_finite() && lon.is_finite()) {
            return Err(SiteError::Invalid("lat and lon must be numbers".into()));
        }
        let Some(city) = cfg.city_at(lat, lon) else {
            return Err(SiteError::Invalid(format!(
                "lat/lon ({lat}, {lon}) is outside every covered city"
            )));
        };
        let mut ops = self.operator.write();
        if ops.len() >= cfg.sites.max_total {
            return Err(SiteError::Full(format!(
                "the engine holds its maximum of {} operator sites",
                cfg.sites.max_total
            )));
        }
        let mine = ops
            .iter()
            .filter(|s| s.owner.as_deref() == Some(owner))
            .count();
        if mine >= cfg.sites.per_owner {
            return Err(SiteError::Full(format!(
                "this owner already has the maximum of {} sites",
                cfg.sites.per_owner
            )));
        }
        let mut id = new_operator_id(owner, name);
        while ops.iter().any(|s| s.id == id) {
            id = new_operator_id(owner, name);
        }
        let site = Site {
            id,
            kind,
            name: Some(name.trim().to_string()),
            operator: operator.map(|o| o.trim().to_string()),
            lat,
            lon,
            city: city.id.clone(),
            source: SiteSource::Operator,
            osm: None,
            owner: Some(owner.to_string()),
            added_at: Some(now),
        };
        ops.push(site.clone());
        Ok(site)
    }

    /// Remove an operator site of this owner.
    pub fn remove(&self, id: &str, owner: &str) -> Result<(), SiteError> {
        if id.starts_with("osm:") {
            return Err(SiteError::NotOperator);
        }
        let mut ops = self.operator.write();
        let before = ops.len();
        ops.retain(|s| !(s.id == id && s.owner.as_deref() == Some(owner)));
        if ops.len() == before {
            Err(SiteError::NotFound)
        } else {
            Ok(())
        }
    }

    /// The operator sites as JSON, for the sites file.
    #[must_use]
    pub fn encode_operator(&self) -> Vec<u8> {
        serde_json::to_vec(&serde_json::json!({
            "format": "neermattam-engine-sites",
            "version": 1,
            "sites": &*self.operator.read(),
        }))
        .expect("sites serialise")
    }

    /// Replace the operator sites from a sites file. Sites outside every
    /// configured city are dropped. Returns how many were kept.
    pub fn decode_operator(&self, bytes: &[u8], cfg: &Config) -> Result<usize, String> {
        #[derive(Deserialize)]
        struct File {
            format: String,
            version: u32,
            sites: Vec<Site>,
        }
        let f: File = serde_json::from_slice(bytes).map_err(|e| e.to_string())?;
        if f.format != "neermattam-engine-sites" || f.version != 1 {
            return Err("not a neermattam-engine-sites version 1 file".into());
        }
        let kept: Vec<Site> = f
            .sites
            .into_iter()
            .filter(|s| {
                s.source == SiteSource::Operator
                    && s.owner.as_deref().is_some_and(valid_owner)
                    && s.id.starts_with("op_")
            })
            .filter_map(|mut s| {
                cfg.city_at(s.lat, s.lon).map(|c| {
                    s.city = c.id.clone();
                    s
                })
            })
            .collect();
        let n = kept.len();
        *self.operator.write() = kept;
        Ok(n)
    }
}

/// Live state for sites: the registry and the risk ledger.
#[derive(Debug)]
pub struct SitesState {
    /// Every known site.
    pub registry: SiteRegistry,
    /// The risk ledger.
    pub ledger: Mutex<Ledger>,
    /// The ledger changed since it was saved.
    pub ledger_dirty: AtomicBool,
}

impl SitesState {
    /// Load the OpenStreetMap sites and start an empty ledger.
    #[must_use]
    pub fn new(cfg: &Config) -> Self {
        SitesState {
            registry: SiteRegistry::load(cfg),
            ledger: Mutex::new(Ledger::new(cfg.sites.ledger_resolution, Utc::now())),
            ledger_dirty: AtomicBool::new(false),
        }
    }
}

/// The file next to the cell snapshot with `.<what>.json` in place of
/// `.json`, such as `cells.ledger.json`.
#[must_use]
pub fn sibling(snapshot: &Path, what: &str) -> PathBuf {
    let stem = snapshot
        .file_stem()
        .map_or_else(|| "cells".into(), |s| s.to_string_lossy().into_owned());
    snapshot.with_file_name(format!("{stem}.{what}.json"))
}

/// What the watch zones are judged against.
#[derive(Clone, Debug, PartialEq)]
pub struct ZoneParams {
    /// Ring radii, metres, smallest first.
    pub rings_m: Vec<f64>,
    /// Blocking depth over which a ring is `over`, cm.
    pub threshold_cm: f64,
    /// Share of a ring's cells that must be known for `under`.
    pub min_coverage: f64,
}

impl Default for ZoneParams {
    fn default() -> Self {
        ZoneParams {
            rings_m: vec![300.0, 1000.0],
            threshold_cm: 15.0,
            min_coverage: 0.5,
        }
    }
}

/// A watch zone's status.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ZoneStatus {
    /// Some cell with evidence in force is over the threshold.
    Over,
    /// Enough of the zone is known, and none of it is over the threshold.
    Under,
    /// Too little recent evidence to say.
    Unknown,
}

/// A site's status over its zones.
#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum SiteStatus {
    /// The smallest zone is over.
    Over,
    /// Only a larger zone is over.
    Nearby,
    /// The smallest zone is under, and no zone is over.
    Under,
    /// Too little recent evidence.
    Unknown,
}

/// The deepest cell of a zone.
#[derive(Clone, Debug, PartialEq, Serialize)]
pub struct Deepest {
    /// H3 index.
    pub cell: String,
    /// The depth routing judges it at, cm.
    pub depth_block_cm: f64,
    /// Mean, cm.
    pub depth_cm: f64,
    /// Sigma, cm.
    pub depth_sigma_cm: f64,
    /// Shown as unknown on the map.
    pub unknown: bool,
    /// Hours since its latest evidence.
    pub age_h: f64,
}

/// Cells by fitted trend.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Serialize)]
pub struct TrendCells {
    /// Rising.
    pub rising: u32,
    /// Falling.
    pub falling: u32,
    /// Steady.
    pub steady: u32,
}

/// One ring around a site.
#[derive(Clone, Debug, PartialEq, Serialize)]
pub struct Zone {
    /// Radius, metres.
    pub radius_m: f64,
    /// Map cells whose centre is within the radius.
    pub cells: usize,
    /// Of those, cells with evidence in force.
    pub cells_reported: usize,
    /// Of those, cells not shown as unknown.
    pub cells_known: usize,
    /// `cells_known / cells`.
    pub coverage: f64,
    /// Over, under or unknown.
    pub status: ZoneStatus,
    /// The deepest cell with evidence in force.
    pub deepest: Option<Deepest>,
    /// Expired cells whose last level was over the threshold.
    pub old_deep_cells: usize,
    /// The deepest of their last levels, cm.
    pub old_deep_max_cm: Option<f64>,
    /// How old that report is, hours.
    pub old_deep_age_h: Option<f64>,
    /// Hours since the newest evidence in the zone.
    pub newest_evidence_age_h: Option<f64>,
    /// Combined trend of the zone's cells.
    pub trend: Option<Trend>,
    /// Cells by trend.
    pub trend_cells: TrendCells,
}

fn r1(x: f64) -> f64 {
    (x * 10.0).round() / 10.0
}

fn r2(x: f64) -> f64 {
    (x * 100.0).round() / 100.0
}

fn hours_between(a: DateTime<Utc>, b: DateTime<Utc>) -> f64 {
    (b - a).num_milliseconds() as f64 / 3_600_000.0
}

/// The map cells whose centre lies within `radius_m` of the point, always
/// including the cell holding it.
#[must_use]
pub fn zone_cells(lat: f64, lon: f64, radius_m: f64, res: Resolution) -> Vec<CellIndex> {
    let Ok(ll) = LatLng::new(lat, lon) else {
        return Vec::new();
    };
    let home = ll.to_cell(res);
    let spacing = res.edge_length_m() * 3f64.sqrt();
    let k = (radius_m / spacing).ceil() as u32 + 1;
    home.grid_disk::<Vec<_>>(k)
        .into_iter()
        .filter(|c| {
            if *c == home {
                return true;
            }
            let p = LatLng::from(*c);
            haversine_m(lat, lon, p.lat(), p.lng()) <= radius_m
        })
        .collect()
}

/// Judge one ring around a point against the live map.
#[must_use]
pub fn zone(
    grid: &Grid,
    lat: f64,
    lon: f64,
    radius_m: f64,
    now: DateTime<Utc>,
    policy: &RoutePolicy,
    zp: &ZoneParams,
) -> Zone {
    let cells = zone_cells(lat, lon, radius_m, grid.resolution());
    let mut z = Zone {
        radius_m,
        cells: cells.len(),
        cells_reported: 0,
        cells_known: 0,
        coverage: 0.0,
        status: ZoneStatus::Unknown,
        deepest: None,
        old_deep_cells: 0,
        old_deep_max_cm: None,
        old_deep_age_h: None,
        newest_evidence_age_h: None,
        trend: None,
        trend_cells: TrendCells::default(),
    };
    let mut over = false;
    for c in &cells {
        let Some(v) = grid.view(*c, now) else {
            continue;
        };
        if v.expired {
            if let (Some(d), Some(age)) = (v.last_depth_cm, v.last_reading_age_h) {
                if d > zp.threshold_cm {
                    z.old_deep_cells += 1;
                    if z.old_deep_max_cm.is_none_or(|m| d > m) {
                        z.old_deep_max_cm = Some(r1(d));
                        z.old_deep_age_h = Some(r1(age));
                    }
                }
            }
            continue;
        }
        z.cells_reported += 1;
        if !v.unknown {
            z.cells_known += 1;
        }
        let age = hours_between(v.updated_at, now).max(0.0);
        if z.newest_evidence_age_h.is_none_or(|a| age < a) {
            z.newest_evidence_age_h = Some(age);
        }
        let (block, _, _) = blocking_estimate(&v, policy);
        if block > zp.threshold_cm {
            over = true;
        }
        if z.deepest.as_ref().is_none_or(|d| block > d.depth_block_cm) {
            z.deepest = Some(deepest(&v, block, age));
        }
        if v.n_readings >= 3 && !v.neighbour_only {
            if let Some(s) = grid.state(*c) {
                let h: Vec<HistoryPoint> = s.history.iter().copied().collect();
                if let Ok(d) = drain_estimate(&h) {
                    match d.trend {
                        Trend::Rising => z.trend_cells.rising += 1,
                        Trend::Falling => z.trend_cells.falling += 1,
                        Trend::Steady => z.trend_cells.steady += 1,
                    }
                }
            }
        }
    }
    let t = z.trend_cells;
    z.trend = if t.rising > 0 {
        Some(Trend::Rising)
    } else if t.falling > 0 && t.steady == 0 {
        Some(Trend::Falling)
    } else if t.steady > 0 || t.falling > 0 {
        Some(Trend::Steady)
    } else {
        None
    };
    z.coverage = if z.cells == 0 {
        0.0
    } else {
        r2(z.cells_known as f64 / z.cells as f64)
    };
    z.newest_evidence_age_h = z.newest_evidence_age_h.map(r2);
    z.status = if over {
        ZoneStatus::Over
    } else if z.cells > 0 && (z.cells_known as f64 / z.cells as f64) >= zp.min_coverage {
        ZoneStatus::Under
    } else {
        ZoneStatus::Unknown
    };
    z
}

fn deepest(v: &CellView, block: f64, age: f64) -> Deepest {
    Deepest {
        cell: v.cell.to_string(),
        depth_block_cm: r1(block),
        depth_cm: r1(v.depth.mean),
        depth_sigma_cm: r1(v.depth.sigma()),
        unknown: v.unknown,
        age_h: r2(age),
    }
}

/// A site's status from its zones, smallest first.
#[must_use]
pub fn site_status(zones: &[Zone]) -> SiteStatus {
    match zones.first() {
        None => SiteStatus::Unknown,
        Some(z) if z.status == ZoneStatus::Over => SiteStatus::Over,
        Some(_) if zones.iter().any(|z| z.status == ZoneStatus::Over) => SiteStatus::Nearby,
        Some(z) if z.status == ZoneStatus::Under => SiteStatus::Under,
        Some(_) => SiteStatus::Unknown,
    }
}

/// A site with its zones, as the API returns it.
#[derive(Clone, Debug, PartialEq, Serialize)]
pub struct SiteView {
    /// The site.
    #[serde(flatten)]
    pub site: Site,
    /// The map cell holding it.
    pub cell: String,
    /// Status over its zones.
    pub status: SiteStatus,
    /// One per ring, smallest first.
    pub zones: Vec<Zone>,
}

/// Judge a site's rings.
#[must_use]
pub fn site_view(
    grid: &Grid,
    site: Site,
    now: DateTime<Utc>,
    policy: &RoutePolicy,
    zp: &ZoneParams,
) -> SiteView {
    let zones: Vec<Zone> = zp
        .rings_m
        .iter()
        .map(|r| zone(grid, site.lat, site.lon, *r, now, policy, zp))
        .collect();
    let cell = LatLng::new(site.lat, site.lon)
        .map(|ll| ll.to_cell(grid.resolution()).to_string())
        .unwrap_or_default();
    SiteView {
        status: site_status(&zones),
        site,
        cell,
        zones,
    }
}

/// Order site views: over, nearby, under, unknown; then the deepest smallest
/// ring; then name; then id.
pub fn sort_views(v: &mut [SiteView]) {
    let depth = |s: &SiteView| {
        s.zones
            .iter()
            .filter_map(|z| z.deepest.as_ref().map(|d| d.depth_block_cm))
            .fold(f64::NEG_INFINITY, f64::max)
    };
    v.sort_by(|a, b| {
        a.status
            .cmp(&b.status)
            .then(depth(b).total_cmp(&depth(a)))
            .then(a.site.name.cmp(&b.site.name))
            .then(a.site.id.cmp(&b.site.id))
    });
}

/// Where an access route goes.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Target {
    /// The nearest main road.
    MainRoad,
    /// The nearest fuel station.
    Fuel,
}

/// An access route's outcome.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum AccessStatus {
    /// A route avoiding water too deep for the vehicle exists.
    Open,
    /// Every route crosses water too deep for the vehicle.
    NoSafeRoute,
    /// No route could be planned (see `reason`).
    Unavailable,
}

/// The place an access route ends.
#[derive(Clone, Debug, PartialEq, Serialize)]
pub struct Destination {
    /// The fuel station's id.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub id: Option<String>,
    /// Its name, when known.
    pub name: Option<String>,
    /// Latitude.
    pub lat: f64,
    /// Longitude.
    pub lon: f64,
}

/// One access route.
#[derive(Clone, Debug, PartialEq, Serialize)]
pub struct AccessRoute {
    /// Main road or fuel.
    pub target: Target,
    /// For whom.
    pub vehicle: Vehicle,
    /// Open, no safe route, or unavailable.
    pub status: AccessStatus,
    /// Why it is unavailable.
    pub reason: Option<&'static str>,
    /// Where it ends.
    pub destination: Option<Destination>,
    /// Length, metres.
    pub distance_m: Option<f64>,
    /// Shortest route to the same place, ignoring water, metres.
    pub baseline_distance_m: Option<f64>,
    /// `distance_m - baseline_distance_m`.
    pub detour_m: Option<f64>,
    /// Minutes at the vehicle's urban speed.
    pub eta_min: Option<f64>,
    /// Cells on the route.
    pub cells_on_route: Option<usize>,
    /// Of those, unknown.
    pub unknown_cells_crossed: Option<usize>,
    /// Of those, too deep for the vehicle.
    pub impassable_cells_crossed: Option<usize>,
    /// Deepest mean among known cells on it, cm.
    pub max_known_depth_cm: Option<f64>,
    /// The engine's route text.
    pub explanation: Option<String>,
    /// `[lon, lat]` pairs.
    pub coordinates: Vec<[f64; 2]>,
}

impl AccessRoute {
    fn unavailable(target: Target, vehicle: Vehicle, reason: &'static str) -> Self {
        AccessRoute {
            target,
            vehicle,
            status: AccessStatus::Unavailable,
            reason: Some(reason),
            destination: None,
            distance_m: None,
            baseline_distance_m: None,
            detour_m: None,
            eta_min: None,
            cells_on_route: None,
            unknown_cells_crossed: None,
            impassable_cells_crossed: None,
            max_known_depth_cm: None,
            explanation: None,
            coordinates: Vec::new(),
        }
    }
}

/// The vehicles access routes are planned for, in order.
pub const ACCESS_VEHICLES: [Vehicle; 3] = [Vehicle::Truck, Vehicle::TwoWheeler, Vehicle::Car];
/// How far a main road may be, metres.
pub const MAIN_ROAD_MAX_M: f64 = 3000.0;
/// How far a fuel station may be, metres.
pub const FUEL_MAX_M: f64 = 8000.0;
/// Destinations tried per route.
pub const CANDIDATES: usize = 3;
/// Least distance between two destinations tried, metres.
pub const CANDIDATE_SEP_M: f64 = 300.0;

/// The nearest fuel stations a tanker could use, nearest first, at most `k`,
/// at least `min_sep_m` apart. Stations tagged `fuel:diesel=no` are skipped.
#[must_use]
pub fn nearest_fuel(
    fuel: &[FuelPoint],
    lat: f64,
    lon: f64,
    max_m: f64,
    k: usize,
    min_sep_m: f64,
) -> Vec<&FuelPoint> {
    let mut d: Vec<(f64, &FuelPoint)> = fuel
        .iter()
        .filter(|f| f.diesel != Some(false))
        .map(|f| (haversine_m(lat, lon, f.lat, f.lon), f))
        .filter(|(m, _)| *m <= max_m)
        .collect();
    d.sort_by(|a, b| a.0.total_cmp(&b.0).then(a.1.id.cmp(&b.1.id)));
    let mut out: Vec<&FuelPoint> = Vec::new();
    for (_, f) in d {
        if out
            .iter()
            .all(|o| haversine_m(o.lat, o.lon, f.lat, f.lon) >= min_sep_m)
        {
            out.push(f);
            if out.len() == k {
                break;
            }
        }
    }
    out
}

/// Plan one access route: try each destination and keep the shortest open
/// route, else the least-bad one with the fewest impassable cells.
fn best_route(
    graph: &RoadGraph,
    water: &WaterMap,
    site: &Site,
    target: Target,
    vehicle: Vehicle,
    dests: &[Destination],
    policy: &RoutePolicy,
) -> AccessRoute {
    if dests.is_empty() {
        return AccessRoute::unavailable(target, vehicle, "no_target_in_range");
    }
    let mut best: Option<(bool, usize, f64, AccessRoute)> = None;
    for d in dests {
        let r = match plan_route(
            graph,
            water,
            (site.lat, site.lon),
            (d.lat, d.lon),
            vehicle,
            policy,
        ) {
            Ok(r) => r,
            Err(RouteError::StartNotNearRoad { .. }) => {
                return AccessRoute::unavailable(target, vehicle, "no_road_near_site")
            }
            Err(_) => continue,
        };
        let open = r.route.kind == RouteKind::WaterAware && r.route.passable;
        let m = &r.route;
        let ar = AccessRoute {
            target,
            vehicle,
            status: if open {
                AccessStatus::Open
            } else {
                AccessStatus::NoSafeRoute
            },
            reason: None,
            destination: Some(d.clone()),
            distance_m: Some(m.distance_m.round()),
            baseline_distance_m: Some(r.baseline.distance_m.round()),
            detour_m: Some((m.distance_m - r.baseline.distance_m).max(0.0).round()),
            eta_min: Some(r1(m.eta_min)),
            cells_on_route: Some(m.cells_on_route),
            unknown_cells_crossed: Some(m.unknown_cells_crossed),
            impassable_cells_crossed: Some(m.impassable_cells_crossed),
            max_known_depth_cm: m.max_depth_cm_on_route.map(r1),
            explanation: Some(m.explanation.clone()),
            coordinates: m
                .coordinates
                .iter()
                .map(|c| [(c[0] * 1e6).round() / 1e6, (c[1] * 1e6).round() / 1e6])
                .collect(),
        };
        let key = (open, m.impassable_cells_crossed, m.distance_m);
        let better = match &best {
            None => true,
            Some((bo, bi, bd, _)) => {
                (key.0 && !bo) || (key.0 == *bo && (key.1, key.2) < (*bi, *bd))
            }
        };
        if better {
            best = Some((key.0, key.1, key.2, ar));
        }
    }
    best.map_or_else(
        || AccessRoute::unavailable(target, vehicle, "no_path"),
        |b| b.3,
    )
}

/// Access routes from a site to the nearest main road and fuel station, for
/// a tanker, a two-wheeler and a car. `graph` is `Err(reason)` while the
/// city's road graph is loading (`routing_loading`) or has failed
/// (`routing_failed`).
#[must_use]
pub fn access_routes(
    graph: Result<&RoadGraph, &'static str>,
    water: &WaterMap,
    site: &Site,
    fuel: &[FuelPoint],
    policy: &RoutePolicy,
) -> Vec<AccessRoute> {
    let mut out = Vec::with_capacity(6);
    for target in [Target::MainRoad, Target::Fuel] {
        for vehicle in ACCESS_VEHICLES {
            let g = match graph {
                Ok(g) => g,
                Err(reason) => {
                    out.push(AccessRoute::unavailable(target, vehicle, reason));
                    continue;
                }
            };
            let dests: Vec<Destination> = match target {
                Target::MainRoad => g
                    .nearest_main_roads(
                        site.lat,
                        site.lon,
                        vehicle,
                        MAIN_ROAD_MAX_M,
                        CANDIDATES,
                        CANDIDATE_SEP_M,
                    )
                    .into_iter()
                    .map(|(n, _)| {
                        let (lat, lon) = g.coord(n);
                        Destination {
                            id: None,
                            name: None,
                            lat,
                            lon,
                        }
                    })
                    .collect(),
                Target::Fuel => nearest_fuel(
                    fuel,
                    site.lat,
                    site.lon,
                    FUEL_MAX_M,
                    CANDIDATES,
                    CANDIDATE_SEP_M,
                )
                .into_iter()
                .map(|f| Destination {
                    id: Some(f.id.clone()),
                    name: f.name.clone(),
                    lat: f.lat,
                    lon: f.lon,
                })
                .collect(),
            };
            out.push(best_route(g, water, site, target, vehicle, &dests, policy));
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::fusion::{FusionParams, Observation};
    use chrono::TimeZone;

    fn now() -> DateTime<Utc> {
        Utc.with_ymd_and_hms(2026, 11, 2, 10, 0, 0).unwrap()
    }

    fn grid() -> Grid {
        Grid::new(FusionParams::default(), Resolution::Ten)
    }

    fn report(g: &mut Grid, lat: f64, lon: f64, depth: f64, sigma: f64, at: DateTime<Utc>) {
        g.ingest(&Observation {
            lat,
            lon,
            depth_cm: depth,
            depth_sigma_cm: sigma,
            velocity_mps: None,
            velocity_sigma_mps: None,
            observed_at: at,
            trust: 1.0,
        })
        .unwrap();
    }

    const SITE: (f64, f64) = (13.0, 80.2);

    #[test]
    fn zone_cells_cover_the_ring() {
        let c300 = zone_cells(SITE.0, SITE.1, 300.0, Resolution::Ten);
        let c1k = zone_cells(SITE.0, SITE.1, 1000.0, Resolution::Ten);
        // About pi r^2 / cell area (about 0.0151 km2 at resolution 10).
        assert!((15..=26).contains(&c300.len()), "{}", c300.len());
        assert!((180..=240).contains(&c1k.len()), "{}", c1k.len());
        let home = LatLng::new(SITE.0, SITE.1)
            .unwrap()
            .to_cell(Resolution::Ten);
        assert!(c300.contains(&home));
        assert!(zone_cells(SITE.0, SITE.1, 1.0, Resolution::Ten) == vec![home]);
    }

    #[test]
    fn an_unreported_zone_is_unknown_never_under() {
        let g = grid();
        let z = zone(
            &g,
            SITE.0,
            SITE.1,
            300.0,
            now(),
            &RoutePolicy::default(),
            &ZoneParams::default(),
        );
        assert_eq!(z.status, ZoneStatus::Unknown);
        assert_eq!((z.cells_reported, z.cells_known), (0, 0));
        assert!(z.deepest.is_none() && z.trend.is_none());
        assert_eq!(z.coverage, 0.0);
    }

    #[test]
    fn one_shallow_report_leaves_the_zone_unknown_until_enough_is_known() {
        let mut g = grid();
        report(&mut g, SITE.0, SITE.1, 3.0, 2.0, now());
        let zp = ZoneParams::default();
        let p = RoutePolicy::default();
        let z = zone(&g, SITE.0, SITE.1, 300.0, now(), &p, &zp);
        // Seven cells have evidence (the cell and its ring), a third of the
        // zone: the rest has no report, so the zone is unknown, not under.
        assert_eq!(z.cells_reported, 7);
        assert_eq!(z.cells_known, 7);
        assert!(z.coverage < 0.5, "{}", z.coverage);
        assert_eq!(z.status, ZoneStatus::Unknown);
        // Shallow reports across the whole ring make it known and under.
        for c in zone_cells(SITE.0, SITE.1, 300.0, Resolution::Ten) {
            let ll = LatLng::from(c);
            report(&mut g, ll.lat(), ll.lng(), 2.0, 2.0, now());
        }
        let z = zone(&g, SITE.0, SITE.1, 300.0, now(), &p, &zp);
        assert_eq!(z.status, ZoneStatus::Under, "{z:?}");
        assert!(z.coverage >= 0.5);
        assert!(z.deepest.unwrap().depth_block_cm <= 15.0);
    }

    #[test]
    fn deep_water_in_the_outer_ring_is_nearby_and_inner_is_over() {
        let mut g = grid();
        // About 700 m north of the site: in the 1 km ring, not the 300 m one.
        report(&mut g, SITE.0 + 0.0063, SITE.1, 40.0, 3.0, now());
        let p = RoutePolicy::default();
        let zp = ZoneParams::default();
        let site = Site {
            id: "osm:way:1".into(),
            kind: SiteKind::DataCentre,
            name: Some("Test DC".into()),
            operator: None,
            lat: SITE.0,
            lon: SITE.1,
            city: "chennai".into(),
            source: SiteSource::Osm,
            osm: Some("way/1".into()),
            owner: None,
            added_at: None,
        };
        let v = site_view(&g, site.clone(), now(), &p, &zp);
        assert_eq!(v.zones[0].status, ZoneStatus::Unknown);
        assert_eq!(v.zones[1].status, ZoneStatus::Over);
        assert_eq!(v.status, SiteStatus::Nearby);
        let d = v.zones[1].deepest.clone().unwrap();
        assert!(d.depth_block_cm > 40.0 && d.depth_cm == 40.0, "{d:?}");
        report(&mut g, SITE.0, SITE.1, 25.0, 3.0, now());
        let v = site_view(&g, site, now(), &p, &zp);
        assert_eq!(v.status, SiteStatus::Over);
        // Thirteen hours later the evidence has expired: unknown, and the old
        // deep level is still named.
        let later = now() + chrono::Duration::hours(13);
        let z = zone(&g, SITE.0, SITE.1, 1000.0, later, &p, &zp);
        assert_eq!(z.status, ZoneStatus::Unknown);
        assert_eq!(z.old_deep_cells, 2);
        assert_eq!(z.old_deep_max_cm, Some(40.0));
        assert!(z.deepest.is_none());
    }

    #[test]
    fn rising_water_is_reported_as_rising() {
        let mut g = grid();
        for (i, d) in [20.0, 30.0, 40.0, 50.0].iter().enumerate() {
            report(
                &mut g,
                SITE.0,
                SITE.1,
                *d,
                2.0,
                now() + chrono::Duration::minutes(30 * i as i64),
            );
        }
        let z = zone(
            &g,
            SITE.0,
            SITE.1,
            300.0,
            now() + chrono::Duration::minutes(91),
            &RoutePolicy::default(),
            &ZoneParams::default(),
        );
        assert_eq!(z.trend, Some(Trend::Rising));
        assert_eq!(z.trend_cells.rising, 1);
        assert_eq!(z.status, ZoneStatus::Over);
    }

    #[test]
    fn statuses_order_and_combine() {
        let mk = |s: ZoneStatus| Zone {
            radius_m: 1.0,
            cells: 1,
            cells_reported: 0,
            cells_known: 0,
            coverage: 0.0,
            status: s,
            deepest: None,
            old_deep_cells: 0,
            old_deep_max_cm: None,
            old_deep_age_h: None,
            newest_evidence_age_h: None,
            trend: None,
            trend_cells: TrendCells::default(),
        };
        use ZoneStatus::{Over, Under, Unknown};
        assert_eq!(site_status(&[mk(Over), mk(Under)]), SiteStatus::Over);
        assert_eq!(site_status(&[mk(Under), mk(Over)]), SiteStatus::Nearby);
        assert_eq!(site_status(&[mk(Unknown), mk(Over)]), SiteStatus::Nearby);
        assert_eq!(site_status(&[mk(Under), mk(Unknown)]), SiteStatus::Under);
        assert_eq!(site_status(&[mk(Unknown), mk(Under)]), SiteStatus::Unknown);
        assert_eq!(site_status(&[]), SiteStatus::Unknown);
        assert!(SiteStatus::Over < SiteStatus::Nearby && SiteStatus::Under < SiteStatus::Unknown);
    }

    #[test]
    fn fuel_candidates_skip_stations_without_diesel_and_spread_out() {
        let f = |id: &str, lat: f64, diesel: Option<bool>| FuelPoint {
            id: id.into(),
            name: None,
            diesel,
            lat,
            lon: 80.2,
        };
        let fuel = vec![
            f("a", 13.001, Some(false)),
            f("b", 13.002, None),
            f("c", 13.0021, Some(true)),
            f("d", 13.010, Some(true)),
            f("e", 13.2, Some(true)),
        ];
        let got: Vec<&str> = nearest_fuel(&fuel, 13.0, 80.2, 8000.0, 3, 300.0)
            .into_iter()
            .map(|p| p.id.as_str())
            .collect();
        assert_eq!(got, ["b", "d"]);
    }

    #[test]
    fn the_embedded_osm_file_loads_for_the_default_cities() {
        let cfg = Config::default();
        let reg = SiteRegistry::load(&cfg);
        assert!(reg.load_error.is_none(), "{:?}", reg.load_error);
        assert!(reg.osm_len() > 0);
        assert!(reg.info.attribution.contains("OpenStreetMap"));
        let dcs = reg.list("chennai", Some(SiteKind::DataCentre), None);
        assert!(!dcs.is_empty());
        for s in &dcs {
            assert!(cfg.city("chennai").unwrap().contains(s.lat, s.lon));
            assert!(s.id.starts_with("osm:") && s.osm.is_some());
        }
        assert!(!reg.fuel_in("chennai").is_empty());
    }

    #[test]
    fn operator_sites_are_private_to_their_owner_and_capped() {
        let mut cfg = Config::default();
        cfg.sites.per_owner = 2;
        let reg = SiteRegistry::default();
        let add = |owner: &str, name: &str| {
            reg.add(
                &cfg,
                owner,
                SiteKind::DataCentre,
                name,
                Some("Acme"),
                13.05,
                80.2,
                now(),
            )
        };
        let a = add("key1", "DC A").unwrap();
        assert!(a.id.starts_with("op_") && a.id.len() == 19);
        assert_eq!(a.city, "chennai");
        add("key1", "DC B").unwrap();
        assert!(matches!(add("key1", "DC C"), Err(SiteError::Full(_))));
        add("key2", "Other").unwrap();
        assert!(reg.get(&a.id, Some("key1")).is_some());
        assert!(reg.get(&a.id, Some("key2")).is_none());
        assert!(reg.get(&a.id, None).is_none());
        assert_eq!(reg.list("chennai", None, Some("key1")).len(), 2);
        assert!(reg.list("chennai", None, None).is_empty());
        assert!(matches!(
            reg.add(
                &cfg,
                "key3",
                SiteKind::Hospital,
                "x",
                None,
                14.5,
                80.2,
                now()
            ),
            Err(SiteError::Invalid(_))
        ));
        assert!(matches!(
            reg.add(
                &cfg,
                "bad key",
                SiteKind::Hospital,
                "x",
                None,
                13.05,
                80.2,
                now()
            ),
            Err(SiteError::Invalid(_))
        ));
        assert_eq!(reg.remove(&a.id, "key2"), Err(SiteError::NotFound));
        assert_eq!(reg.remove("osm:way:1", "key1"), Err(SiteError::NotOperator));
        reg.remove(&a.id, "key1").unwrap();
        // The sites file round-trips.
        let bytes = reg.encode_operator();
        let back = SiteRegistry::default();
        assert_eq!(back.decode_operator(&bytes, &cfg).unwrap(), 2);
        assert_eq!(back.list("chennai", None, Some("key2")).len(), 1);
    }
}
