//! Control-room analytics: ranked hotspots, a city summary and the drain-rate
//! estimate behind the recession view.
//!
//! Nothing here changes routing. The hotspot score and the drain rate are
//! aids for people deciding where to send help or ask for a fresh photo; the
//! blocking rule in [`crate::routing`] stays the only thing that opens or
//! closes a road.
//!
//! # Hotspot score
//!
//! Each cell with evidence in force scores from 0 to 100, the sum of four
//! parts:
//!
//! ```text
//! depth     = 40 x min(depth_block / 150 cm, 1)
//! hazard    = 30 x min(HR / 3, 1)        HR: the Defra rating at depth_block
//! freshness = 20 x max(0, 1 - age_h / expire_h)       expire_h = 12
//! reports   = 10 x min(ln(1 + n) / ln(31), 1)          n: readings in force
//! ```
//!
//! - `depth_block` is the depth routing judges the cell at (the safety model's
//!   blocking estimate: the deeper side of any conflict, plus the measurement
//!   margin and growth with time). 150 cm is about chest deep on an adult,
//!   five times the bus limit. An earlier scale filled the depth part at
//!   60 cm, so every street closed to buses scored the same and a control
//!   room could not tell 65 cm from 100 cm.
//! - `HR` is the Defra hazard rating `d x (v + 0.5) + DF` at that depth and
//!   the blocking velocity (the 1 m/s default when not measured). 3 is one
//!   and a half times the "danger for all" boundary (2.0), so the part keeps
//!   rising inside the top class instead of stopping at its edge. The class
//!   itself is still reported.
//! - `age_h` is the time since the latest evidence in force; evidence stops
//!   counting at `expire_h`, so freshness falls to zero there.
//! - `n` (`independent_reports` in the API) is the number of direct readings
//!   in force plus the number of adjacent cells whose readings are in force.
//!   Many reports from one neighbour count once, as in the fusion model. The
//!   engine cannot tell who sent a reading, so `n` counts readings, not
//!   people. The part grows with the logarithm of `n`: the second reading adds
//!   more than the twentieth, and the part is full at 30.
//!
//! Depth and hazard carry 70 of the 100 points, so a fresh report of a dry
//! street (under 30 points) never outranks deep water a few hours old. Ties
//! are broken by the blocking depth, then by cell id, so the order is stable.
//! Each hotspot lists its parts and a plain-English reason for each.
//!
//! # Drain rate
//!
//! See [`drain_estimate`].

use crate::fusion::{CellView, FusionParams, HistoryPoint};
use crate::hazard::{assess, HazardClass};
use crate::routing::{blocking_estimate, cell_passability, Passability, RoutePolicy};
use crate::vehicle::Vehicle;
use chrono::{DateTime, Utc};
use serde::Serialize;

/// Blocking depth at which the depth part is full, cm.
pub const DEPTH_SATURATION_CM: f64 = 150.0;
/// Hazard rating at which the hazard part is full.
pub const HAZARD_SATURATION_HR: f64 = 3.0;
/// Points for depth.
pub const DEPTH_POINTS: f64 = 40.0;
/// Points for the worst hazard class.
pub const HAZARD_POINTS: f64 = 30.0;
/// Points for brand-new evidence.
pub const FRESHNESS_POINTS: f64 = 20.0;
/// Points for many independent reports.
pub const REPORT_POINTS: f64 = 10.0;
/// Readings in force at which the report part is full.
pub const REPORT_SATURATION: f64 = 30.0;

/// A last level above this, in a stale or expired cell, means the cell needs
/// a fresh report: it is the lowest vehicle depth limit.
pub const NEEDS_REPORT_DEPTH_CM: f64 = 15.0;

/// Hours of readings the drain-rate fit looks back over.
pub const DRAIN_WINDOW_H: f64 = 6.0;
/// Fewest readings the fit needs.
pub const DRAIN_MIN_POINTS: usize = 3;
/// Shortest span of readings the fit needs, hours.
pub const DRAIN_MIN_SPAN_H: f64 = 0.5;

fn hours_between(a: DateTime<Utc>, b: DateTime<Utc>) -> f64 {
    (b - a).num_milliseconds() as f64 / 3_600_000.0
}

fn round1(x: f64) -> f64 {
    (x * 10.0).round() / 10.0
}

/// The four parts of a hotspot score.
#[derive(Clone, Copy, Debug, PartialEq, Serialize)]
pub struct ScoreParts {
    /// Up to 40.
    pub depth: f64,
    /// Up to 30.
    pub hazard: f64,
    /// Up to 20.
    pub freshness: f64,
    /// Up to 10.
    pub reports: f64,
}

/// One ranked cell.
#[derive(Clone, Debug, PartialEq, Serialize)]
pub struct Hotspot {
    /// H3 index, hex.
    pub cell: String,
    /// Cell centre latitude.
    pub lat: f64,
    /// Cell centre longitude.
    pub lon: f64,
    /// 0 to 100.
    pub score: f64,
    /// How the score was made.
    pub parts: ScoreParts,
    /// The depth routing judges the cell at, cm.
    pub depth_block_cm: f64,
    /// Mean depth, cm.
    pub depth_cm: f64,
    /// Depth standard deviation, cm.
    pub depth_sigma_cm: f64,
    /// Defra class at the blocking depth.
    pub hazard_class_upper: HazardClass,
    /// Hours since the latest evidence in force.
    pub age_h: f64,
    /// Direct readings plus adjacent cells reporting.
    pub independent_reports: u32,
    /// True when the map shows the cell as unknown.
    pub unknown: bool,
    /// Vehicles the cell is closed to.
    pub blocks: Vec<Vehicle>,
    /// One plain-English sentence per part, plus notes.
    pub reasons: Vec<String>,
    /// The nearest named road in the city's road graph, when one is within
    /// [`PLACE_MAX_M`]; filled in by the API, which holds the graphs.
    pub place: Option<PlaceName>,
}

/// Furthest a named road may be from a cell centre to name it, metres.
pub const PLACE_MAX_M: f64 = 400.0;

/// A place name for a cell.
#[derive(Clone, Debug, PartialEq, Serialize)]
pub struct PlaceName {
    /// The road's OpenStreetMap name (`name:en`, else `name`, else `ref`).
    pub name: String,
    /// Distance from the cell centre to the road, metres.
    pub distance_m: f64,
    /// Where the name comes from: always `osm_road`.
    pub source: &'static str,
}

/// Points for a hazard rating: proportional to the rating, full at
/// [`HAZARD_SATURATION_HR`].
fn hazard_points(rating: f64) -> f64 {
    if rating.is_finite() {
        HAZARD_POINTS * (rating / HAZARD_SATURATION_HR).clamp(0.0, 1.0)
    } else {
        0.0
    }
}

/// Points for `n` readings in force: logarithmic, full at
/// [`REPORT_SATURATION`].
fn report_points(n: u32) -> f64 {
    REPORT_POINTS * ((1.0 + f64::from(n)).ln() / (1.0 + REPORT_SATURATION).ln()).min(1.0)
}

/// The numbers behind one cell's score.
struct Scored {
    parts: ScoreParts,
    score: f64,
    depth_block: f64,
    velocity: f64,
    assumed: bool,
    class: HazardClass,
    rating: f64,
    age_h: f64,
    direct: u32,
    independent: u32,
}

/// The score alone, without the explanation, or `None` when the cell has no
/// evidence in force.
fn score(
    v: &CellView,
    policy: &RoutePolicy,
    params: &FusionParams,
    now: DateTime<Utc>,
) -> Option<Scored> {
    if v.expired {
        return None;
    }
    let (depth_block, velocity, assumed) = blocking_estimate(v, policy);
    let hazard = assess(
        depth_block,
        (!assumed).then_some(velocity),
        velocity,
        policy.debris_rule,
    );
    let age_h = hours_between(v.updated_at, now).max(0.0);
    // Direct readings count only while the cell's own track is in force.
    let direct = if v.neighbour_only { 0 } else { v.n_readings };
    let independent = direct + v.neighbour_sources;
    let parts = ScoreParts {
        depth: DEPTH_POINTS * (depth_block / DEPTH_SATURATION_CM).clamp(0.0, 1.0),
        hazard: hazard_points(hazard.rating),
        freshness: FRESHNESS_POINTS * (1.0 - age_h / params.expire_after_h).clamp(0.0, 1.0),
        reports: report_points(independent),
    };
    Some(Scored {
        score: round1(parts.depth + parts.hazard + parts.freshness + parts.reports),
        parts,
        depth_block,
        velocity,
        assumed,
        class: hazard.class,
        rating: hazard.rating,
        age_h,
        direct,
        independent,
    })
}

/// Score one cell, or `None` when it has no evidence in force.
#[must_use]
pub fn hotspot(
    v: &CellView,
    policy: &RoutePolicy,
    params: &FusionParams,
    now: DateTime<Utc>,
) -> Option<Hotspot> {
    let Scored {
        parts,
        score,
        depth_block,
        velocity,
        assumed,
        class,
        rating,
        age_h,
        direct,
        independent,
    } = score(v, policy, params, now)?;
    let blocks: Vec<Vehicle> = Vehicle::ALL
        .into_iter()
        .filter(|veh| {
            matches!(
                cell_passability(Some(v), *veh, policy),
                Passability::Blocked { .. }
            )
        })
        .collect();

    let mut reasons = vec![
        format!(
            "Water may be as deep as {:.0} cm here (mean {:.0} cm plus the safety margin): {:.1} of 40 points.",
            depth_block, v.depth.mean, parts.depth
        ),
        format!(
            "At that depth the Defra hazard rating is {:.2}, {}: {:.1} of 30 points.",
            rating,
            class.describe(),
            parts.hazard
        ),
        format!(
            "The latest evidence is {:.1} hours old: {:.1} of 20 points.",
            age_h, parts.freshness
        ),
        format!(
            "{} reading{} in force ({} in the cell, {} from adjacent cells; readings, not necessarily different people): {:.1} of 10 points.",
            independent,
            if independent == 1 { "" } else { "s" },
            direct,
            v.neighbour_sources,
            parts.reports
        ),
    ];
    if let Some(high) = v.conflict_high_mean_cm {
        reasons.push(format!(
            "Reports here disagree ({} conflict{}); the deeper side, {:.0} cm, is used.",
            v.conflicts,
            if v.conflicts == 1 { "" } else { "s" },
            high
        ));
    }
    if v.neighbour_only {
        reasons.push(
            "No report from inside this cell is in force; the depth comes from adjacent cells."
                .into(),
        );
    }
    if v.unknown {
        reasons
            .push("The evidence is thin or ageing, so the map shows this cell as unknown.".into());
    }
    if assumed {
        reasons.push(format!(
            "Flow speed was not measured, so {:.1} m/s is assumed.",
            velocity
        ));
    }
    if blocks.len() == Vehicle::ALL.len() {
        reasons.push("Closed to every vehicle and to people on foot.".into());
    } else if !blocks.is_empty() {
        let names: Vec<&str> = blocks.iter().map(|b| b.as_str()).collect();
        reasons.push(format!("Closed to {}.", names.join(", ")));
    }

    let centre = h3o::LatLng::from(v.cell);
    Some(Hotspot {
        cell: v.cell.to_string(),
        lat: (centre.lat() * 1e6).round() / 1e6,
        lon: (centre.lng() * 1e6).round() / 1e6,
        score,
        parts: ScoreParts {
            depth: round1(parts.depth),
            hazard: round1(parts.hazard),
            freshness: round1(parts.freshness),
            reports: round1(parts.reports),
        },
        depth_block_cm: round1(depth_block),
        depth_cm: round1(v.depth.mean),
        depth_sigma_cm: round1(v.depth.sigma()),
        hazard_class_upper: class,
        age_h: (age_h * 100.0).round() / 100.0,
        independent_reports: independent,
        unknown: v.unknown,
        blocks,
        reasons,
        place: None,
    })
}

/// Rank cells by hotspot score, highest first, keeping at most `limit`.
///
/// Every cell is scored, but only the `limit` kept are explained: building
/// the reasons is most of the cost, and a full city has tens of thousands of
/// cells.
#[must_use]
pub fn hotspots(
    views: impl IntoIterator<Item = CellView>,
    policy: &RoutePolicy,
    params: &FusionParams,
    now: DateTime<Utc>,
    limit: usize,
) -> Vec<Hotspot> {
    // Order: score, then blocking depth as shown (to 0.1 cm), then cell id.
    let mut ranked: Vec<(f64, f64, u64, CellView)> = views
        .into_iter()
        .filter_map(|v| {
            score(&v, policy, params, now)
                .map(|s| (s.score, round1(s.depth_block), u64::from(v.cell), v))
        })
        .collect();
    let order = |a: &(f64, f64, u64, CellView), b: &(f64, f64, u64, CellView)| {
        b.0.total_cmp(&a.0)
            .then(b.1.total_cmp(&a.1))
            .then(a.2.cmp(&b.2))
    };
    if limit == 0 {
        return Vec::new();
    }
    if ranked.len() > limit {
        ranked.select_nth_unstable_by(limit - 1, order);
        ranked.truncate(limit);
    }
    ranked.sort_by(order);
    ranked
        .iter()
        .filter_map(|r| hotspot(&r.3, policy, params, now))
        .collect()
}

/// Order cells most important first: hotspot score descending, then
/// blocking depth descending, then cell index; cells with no evidence in force
/// (no score) go last, by cell index. The export uses it so that a row cap
/// always keeps the deepest and most dangerous cells.
pub fn sort_by_score(
    views: Vec<CellView>,
    policy: &RoutePolicy,
    params: &FusionParams,
    now: DateTime<Utc>,
) -> Vec<CellView> {
    let mut keyed: Vec<(f64, f64, u64, CellView)> = views
        .into_iter()
        .map(|v| match score(&v, policy, params, now) {
            Some(s) => (s.score, round1(s.depth_block), u64::from(v.cell), v),
            None => (-1.0, -1.0, u64::from(v.cell), v),
        })
        .collect();
    keyed.sort_by(|a, b| {
        b.0.total_cmp(&a.0)
            .then(b.1.total_cmp(&a.1))
            .then(a.2.cmp(&b.2))
    });
    keyed.into_iter().map(|k| k.3).collect()
}

/// Cell counts by hazard class (the class shown on the map).
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Serialize)]
pub struct ClassCounts {
    /// Low hazard.
    pub low: usize,
    /// Danger for some.
    pub danger_some: usize,
    /// Danger for most.
    pub danger_most: usize,
    /// Danger for all.
    pub danger_all: usize,
    /// Unknown, including stale and expired cells.
    pub unknown: usize,
}

/// A cell that was deep when last seen and has had no report since.
#[derive(Clone, Debug, PartialEq, Serialize)]
pub struct NeedsReport {
    /// H3 index, hex.
    pub cell: String,
    /// Cell centre latitude.
    pub lat: f64,
    /// Cell centre longitude.
    pub lon: f64,
    /// The direct level after the latest reading, cm.
    pub last_depth_cm: f64,
    /// Hours since that reading.
    pub last_reading_age_h: f64,
    /// True when every piece of evidence has expired.
    pub expired: bool,
}

/// Cells closed to each vehicle.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Serialize)]
pub struct BlockedCounts {
    /// Closed to walkers.
    pub pedestrian: usize,
    /// Closed to two-wheelers.
    pub two_wheeler: usize,
    /// Closed to autos.
    pub auto: usize,
    /// Closed to cars.
    pub car: usize,
    /// Closed to buses.
    pub bus: usize,
    /// Closed to diesel tankers and trucks.
    pub truck: usize,
}

impl BlockedCounts {
    fn add(&mut self, v: Vehicle) {
        match v {
            Vehicle::Pedestrian => self.pedestrian += 1,
            Vehicle::TwoWheeler => self.two_wheeler += 1,
            Vehicle::Auto => self.auto += 1,
            Vehicle::Car => self.car += 1,
            Vehicle::Bus => self.bus += 1,
            Vehicle::Truck => self.truck += 1,
        }
    }
}

/// A summary of every cell with state in an area.
#[derive(Clone, Debug, PartialEq, Serialize)]
pub struct Summary {
    /// Cells with any state.
    pub cells: usize,
    /// Counts by the class shown on the map.
    pub by_class: ClassCounts,
    /// Cells shown as unknown (thin, stale or expired evidence).
    pub unknown: usize,
    /// Cells with no evidence for `stale_after_h` hours that have not expired.
    pub stale: usize,
    /// Cells whose evidence has all expired.
    pub expired: usize,
    /// Cells with a conflict in force.
    pub cells_in_conflict: usize,
    /// Conflicting direct readings counted across all cells.
    pub conflicting_readings: u64,
    /// Cells closed to each vehicle.
    pub blocked: BlockedCounts,
    /// Cells that are stale or expired but were last seen deeper than
    /// `NEEDS_REPORT_DEPTH_CM`.
    pub needs_fresh_report: usize,
    /// The deepest of those, at most 50, deepest first.
    pub needs_fresh_report_cells: Vec<NeedsReport>,
    /// Direct readings fused, over all cells.
    pub readings: u64,
    /// The newest direct reading.
    pub newest_reading_at: Option<DateTime<Utc>>,
}

/// Most cells listed in `needs_fresh_report_cells`.
pub const NEEDS_REPORT_LIST_MAX: usize = 50;

/// Summarise cells as they stand at `now`.
#[must_use]
pub fn summarise(
    views: impl IntoIterator<Item = CellView>,
    policy: &RoutePolicy,
    params: &FusionParams,
    now: DateTime<Utc>,
) -> Summary {
    let mut s = Summary {
        cells: 0,
        by_class: ClassCounts::default(),
        unknown: 0,
        stale: 0,
        expired: 0,
        cells_in_conflict: 0,
        conflicting_readings: 0,
        blocked: BlockedCounts::default(),
        needs_fresh_report: 0,
        needs_fresh_report_cells: Vec::new(),
        readings: 0,
        newest_reading_at: None,
    };
    for v in views {
        s.cells += 1;
        s.readings += u64::from(v.n_readings);
        s.conflicting_readings += u64::from(v.conflicts);
        if v.conflict_high_mean_cm.is_some() {
            s.cells_in_conflict += 1;
        }
        if let Some(at) = v.last_direct_at {
            s.newest_reading_at = Some(s.newest_reading_at.map_or(at, |n| n.max(at)));
        }
        let stale = !v.expired && hours_between(v.updated_at, now) > params.stale_after_h;
        if stale {
            s.stale += 1;
        }
        if v.expired {
            s.expired += 1;
        }
        if v.unknown {
            s.unknown += 1;
            s.by_class.unknown += 1;
        } else {
            let mean = assess(
                v.depth.mean,
                v.velocity.map(|g| g.mean),
                policy.default_velocity_mps,
                policy.debris_rule,
            );
            match mean.class {
                HazardClass::Low => s.by_class.low += 1,
                HazardClass::DangerSome => s.by_class.danger_some += 1,
                HazardClass::DangerMost => s.by_class.danger_most += 1,
                HazardClass::DangerAll => s.by_class.danger_all += 1,
                HazardClass::Unknown => s.by_class.unknown += 1,
            }
        }
        for veh in Vehicle::ALL {
            if matches!(
                cell_passability(Some(&v), veh, policy),
                Passability::Blocked { .. }
            ) {
                s.blocked.add(veh);
            }
        }
        if let (true, Some(d), Some(age)) =
            (stale || v.expired, v.last_depth_cm, v.last_reading_age_h)
        {
            if d > NEEDS_REPORT_DEPTH_CM {
                s.needs_fresh_report += 1;
                let c = h3o::LatLng::from(v.cell);
                s.needs_fresh_report_cells.push(NeedsReport {
                    cell: v.cell.to_string(),
                    lat: (c.lat() * 1e6).round() / 1e6,
                    lon: (c.lng() * 1e6).round() / 1e6,
                    last_depth_cm: round1(d),
                    last_reading_age_h: (age * 100.0).round() / 100.0,
                    expired: v.expired,
                });
            }
        }
    }
    s.needs_fresh_report_cells.sort_by(|a, b| {
        b.last_depth_cm
            .total_cmp(&a.last_depth_cm)
            .then_with(|| a.cell.cmp(&b.cell))
    });
    s.needs_fresh_report_cells.truncate(NEEDS_REPORT_LIST_MAX);
    s
}

/// Direction of the fitted level.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Trend {
    /// Falling by more than twice its standard error.
    Falling,
    /// Rising by more than twice its standard error.
    Rising,
    /// Neither: steady, or too noisy to say.
    Steady,
}

/// A straight-line fit to a cell's recent direct readings.
#[derive(Clone, Copy, Debug, PartialEq, Serialize)]
pub struct DrainEstimate {
    /// Fall of the fitted level per hour, cm/h. Negative when rising.
    pub rate_cm_per_h: f64,
    /// Standard error of that rate, cm/h.
    pub rate_sigma_cm_per_h: f64,
    /// Readings used.
    pub points_used: usize,
    /// First reading used.
    pub from: DateTime<Utc>,
    /// Last reading used.
    pub to: DateTime<Utc>,
    /// Falling, rising or steady.
    pub trend: Trend,
    /// When falling: hours from the last reading until the fitted line
    /// reaches `NEEDS_REPORT_DEPTH_CM` (15 cm, the lowest vehicle limit).
    pub hours_to_15_cm: Option<f64>,
}

/// Fit the drain rate of a cell from its history.
///
/// The fit is weighted least squares of reading depth against time, with
/// weights `1 / sigma^2`, over the direct readings of the last
/// `DRAIN_WINDOW_H` (6) hours before the newest one. It needs at least
/// `DRAIN_MIN_POINTS` (3) readings spanning at least `DRAIN_MIN_SPAN_H`
/// (half an hour); points migrated from an old snapshot are skipped. The
/// standard error is the textbook one for known variances, scaled up by the
/// square root of the reduced chi-squared when the readings scatter more than
/// their sigmas say.
///
/// Honest limits: it is a straight line through a handful of reports that may
/// come from different spots in a 66 m cell and from different people;
/// recession is rarely linear; and the rate and `hours_to_15_cm` are
/// descriptions of the recent past, not forecasts. The engine never uses them
/// to open a road (rule 6 of the safety model: no optimistic recession).
///
/// Returns the reason when there are not enough readings.
pub fn drain_estimate(points: &[HistoryPoint]) -> Result<DrainEstimate, String> {
    let usable: Vec<&HistoryPoint> = points.iter().filter(|p| !p.migrated).collect();
    let Some(last) = usable.last() else {
        return Err("no direct readings are recorded for this cell".into());
    };
    let window: Vec<&HistoryPoint> = usable
        .iter()
        .copied()
        .filter(|p| hours_between(p.at, last.at) <= DRAIN_WINDOW_H)
        .collect();
    if window.len() < DRAIN_MIN_POINTS {
        return Err(format!(
            "a drain rate needs at least {DRAIN_MIN_POINTS} readings within {DRAIN_WINDOW_H} hours; this cell has {}",
            window.len()
        ));
    }
    let t0 = window[0].at;
    let span = hours_between(t0, last.at);
    if span < DRAIN_MIN_SPAN_H {
        return Err(format!(
            "the readings span {:.0} minutes; a drain rate needs at least {:.0}",
            span * 60.0,
            DRAIN_MIN_SPAN_H * 60.0
        ));
    }
    let xs: Vec<(f64, f64, f64)> = window
        .iter()
        .map(|p| {
            (
                hours_between(t0, p.at),
                p.reading_cm,
                1.0 / (p.reading_sigma_cm * p.reading_sigma_cm),
            )
        })
        .collect();
    let sw: f64 = xs.iter().map(|x| x.2).sum();
    let tbar = xs.iter().map(|x| x.2 * x.0).sum::<f64>() / sw;
    let ybar = xs.iter().map(|x| x.2 * x.1).sum::<f64>() / sw;
    let stt: f64 = xs.iter().map(|x| x.2 * (x.0 - tbar).powi(2)).sum();
    let sty: f64 = xs.iter().map(|x| x.2 * (x.0 - tbar) * (x.1 - ybar)).sum();
    let slope = sty / stt;
    let intercept = ybar - slope * tbar;
    let chi2: f64 = xs
        .iter()
        .map(|x| x.2 * (x.1 - intercept - slope * x.0).powi(2))
        .sum();
    let dof = (xs.len() - 2) as f64;
    let scale = if dof > 0.0 {
        (chi2 / dof).max(1.0).sqrt()
    } else {
        1.0
    };
    let se = (1.0 / stt).sqrt() * scale;
    let rate = -slope;
    let trend = if rate > 2.0 * se {
        Trend::Falling
    } else if -rate > 2.0 * se {
        Trend::Rising
    } else {
        Trend::Steady
    };
    let level_now = intercept + slope * span;
    let hours_to_15_cm = (trend == Trend::Falling && level_now > NEEDS_REPORT_DEPTH_CM)
        .then(|| (level_now - NEEDS_REPORT_DEPTH_CM) / rate);
    Ok(DrainEstimate {
        rate_cm_per_h: rate,
        rate_sigma_cm_per_h: se,
        points_used: xs.len(),
        from: t0,
        to: last.at,
        trend,
        hours_to_15_cm,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::fusion::{Grid, Observation};
    use chrono::TimeZone;
    use h3o::{CellIndex, Resolution};

    fn t(h: f64) -> DateTime<Utc> {
        Utc.with_ymd_and_hms(2026, 10, 28, 10, 0, 0).unwrap()
            + chrono::Duration::milliseconds((h * 3_600_000.0) as i64)
    }

    fn obs(lat: f64, lon: f64, depth: f64, at: DateTime<Utc>) -> Observation {
        Observation {
            lat,
            lon,
            depth_cm: depth,
            depth_sigma_cm: 3.0,
            velocity_mps: None,
            velocity_sigma_mps: None,
            observed_at: at,
            trust: 1.0,
        }
    }

    fn grid() -> Grid {
        Grid::new(FusionParams::default(), Resolution::Ten)
    }

    /// Four separate places about 1 km apart, so their rings do not touch.
    const PLACES: [(f64, f64); 5] = [
        (13.00, 80.20),
        (13.01, 80.20),
        (13.02, 80.20),
        (13.03, 80.20),
        (13.04, 80.20),
    ];

    fn ranked(g: &Grid, now: DateTime<Utc>) -> Vec<Hotspot> {
        hotspots(
            g.views(now),
            &RoutePolicy::default(),
            &FusionParams::default(),
            now,
            100,
        )
    }

    fn cell_of(g: &Grid, i: usize) -> String {
        g.cell_at(PLACES[i].0, PLACES[i].1).unwrap().to_string()
    }

    #[test]
    fn score_orders_deep_and_fresh_first() {
        let mut g = grid();
        // 0: deep and fresh. 1: deep, 5 hours old. 2: shallow and fresh.
        // 3: dry and fresh. 4: deep, fresh, three independent reports.
        g.ingest(&obs(PLACES[1].0, PLACES[1].1, 45.0, t(0.0)))
            .unwrap();
        g.ingest(&obs(PLACES[0].0, PLACES[0].1, 45.0, t(5.0)))
            .unwrap();
        g.ingest(&obs(PLACES[2].0, PLACES[2].1, 12.0, t(5.0)))
            .unwrap();
        g.ingest(&obs(PLACES[3].0, PLACES[3].1, 0.0, t(5.0)))
            .unwrap();
        for k in 0..3 {
            g.ingest(&obs(
                PLACES[4].0,
                PLACES[4].1,
                45.0,
                t(4.9 + 0.05 * f64::from(k)),
            ))
            .unwrap();
        }
        let now = t(5.0);
        let r = ranked(&g, now);
        let pos = |i: usize| r.iter().position(|h| h.cell == cell_of(&g, i)).unwrap();
        assert!(pos(4) < pos(0), "more reports rank higher");
        assert!(pos(0) < pos(1), "fresher ranks higher");
        assert!(
            pos(1) < pos(2),
            "deep water a few hours old outranks fresh shallow water"
        );
        assert!(pos(2) < pos(3));
        // Neighbour-only cells around the deep reports also appear, below them.
        let top = &r[0];
        assert!(top.score <= 100.0 && top.score > 50.0, "{}", top.score);
        let p = top.parts;
        assert!((top.score - (p.depth + p.hazard + p.freshness + p.reports)).abs() < 0.2);
        assert!(top.reasons.len() >= 4);
        assert!(top.reasons[0].contains("as deep as"));
        assert!(top.blocks.contains(&Vehicle::Car) && top.blocks.contains(&Vehicle::Bus));
        assert!(r.windows(2).all(|w| w[0].score >= w[1].score));
        // The dry cell scores freshness, a little depth margin, one report.
        let dry = &r[pos(3)];
        assert!(dry.score < 30.0 && dry.blocks.is_empty(), "{}", dry.score);
        for text in r.iter().flat_map(|h| h.reasons.iter()) {
            assert!(!text.contains('\u{2014}') && !text.contains("->"), "{text}");
        }
    }

    #[test]
    fn deep_cells_with_many_readings_are_told_apart() {
        // Regression: the depth part filled at 60 cm, the hazard part at the
        // top class and the report part at five readings, so every deep cell
        // of the demonstration scored 99.7 to 100.
        let mut g = grid();
        for k in 0..40 {
            let at = t(5.0 - 0.01 * f64::from(k));
            g.ingest(&obs(PLACES[0].0, PLACES[0].1, 70.0, at)).unwrap();
            g.ingest(&obs(PLACES[1].0, PLACES[1].1, 100.0, at)).unwrap();
            g.ingest(&obs(PLACES[2].0, PLACES[2].1, 85.0, at)).unwrap();
        }
        let r = ranked(&g, t(5.0));
        let find = |i: usize| r.iter().find(|h| h.cell == cell_of(&g, i)).unwrap();
        let (shallow, mid, deep) = (find(0), find(2), find(1));
        assert!(deep.score > mid.score && mid.score > shallow.score);
        assert!(
            deep.score - shallow.score > 8.0,
            "{} {}",
            deep.score,
            shallow.score
        );
        assert!(deep.score < 95.0, "{}", deep.score);
        assert!(deep.parts.depth > shallow.parts.depth + 7.0);
        assert!(deep.parts.hazard > shallow.parts.hazard);
        // The report part grows slowly: one reading is worth a fifth of it.
        assert!((report_points(1) - 2.0).abs() < 0.05);
        assert!(report_points(5) < 6.0 && report_points(30) == 10.0);
        assert!(deep.reasons[3].contains("not necessarily different people"));
    }

    #[test]
    fn a_limited_ranking_is_the_head_of_the_full_one() {
        // The fast path explains only the kept cells; it must keep the same
        // cells in the same order as ranking everything.
        let mut g = grid();
        for (i, (lat, lon)) in PLACES.iter().enumerate() {
            for k in 0..=i {
                g.ingest(&obs(
                    *lat,
                    *lon,
                    10.0 * (i as f64) + 3.0,
                    t(5.0 - 0.7 * (k as f64)),
                ))
                .unwrap();
            }
        }
        let now = t(5.0);
        let all = ranked(&g, now);
        assert!(all.len() > 20);
        for limit in [1, 3, 7, 20, all.len(), all.len() + 5] {
            let head = hotspots(
                g.views(now),
                &RoutePolicy::default(),
                &FusionParams::default(),
                now,
                limit,
            );
            assert_eq!(head, all[..limit.min(all.len())].to_vec(), "limit {limit}");
        }
    }

    #[test]
    fn expired_direct_readings_are_not_counted_as_reports() {
        // Regression: a cell whose own readings had expired still counted
        // them as independent reports while a neighbour kept it alive.
        let mut g = grid();
        let c = g.cell_at(PLACES[0].0, PLACES[0].1).unwrap();
        for k in 0..5 {
            g.ingest(&obs(PLACES[0].0, PLACES[0].1, 40.0, t(0.1 * f64::from(k))))
                .unwrap();
        }
        let ring: Vec<CellIndex> = c.grid_disk(1);
        let n = h3o::LatLng::from(ring.into_iter().find(|x| *x != c).unwrap());
        g.ingest(&obs(n.lat(), n.lng(), 40.0, t(10.0))).unwrap();
        let h = hotspot(
            &g.view(c, t(13.0)).unwrap(),
            &RoutePolicy::default(),
            &FusionParams::default(),
            t(13.0),
        )
        .unwrap();
        assert_eq!(h.independent_reports, 1);
        assert!(h.reasons[3].contains("(0 in the cell, 1 from adjacent cells;"));
    }

    #[test]
    fn expired_cells_are_not_hotspots_and_limit_applies() {
        let mut g = grid();
        g.ingest(&obs(PLACES[0].0, PLACES[0].1, 45.0, t(0.0)))
            .unwrap();
        assert_eq!(ranked(&g, t(1.0)).len(), 7);
        assert!(ranked(&g, t(13.0)).is_empty());
        let few = hotspots(
            g.views(t(1.0)),
            &RoutePolicy::default(),
            &FusionParams::default(),
            t(1.0),
            2,
        );
        assert_eq!(few.len(), 2);
        assert_eq!(few[0].cell, cell_of(&g, 0));
    }

    #[test]
    fn conflicts_are_scored_on_the_deep_side_and_explained() {
        let mut g = grid();
        g.ingest(&obs(PLACES[0].0, PLACES[0].1, 40.0, t(0.0)))
            .unwrap();
        g.ingest(&obs(PLACES[0].0, PLACES[0].1, 0.0, t(0.1)))
            .unwrap();
        let h = ranked(&g, t(0.2))
            .into_iter()
            .find(|h| h.cell == cell_of(&g, 0))
            .unwrap();
        assert!(h.depth_block_cm >= 40.0);
        assert!(h.reasons.iter().any(|r| r.contains("disagree")));
    }

    #[test]
    fn summary_counts() {
        let mut g = grid();
        // Deep fresh cell, dry fresh cell, a conflict, and an old deep report.
        g.ingest(&obs(PLACES[0].0, PLACES[0].1, 45.0, t(10.0)))
            .unwrap();
        g.ingest(&obs(PLACES[1].0, PLACES[1].1, 0.0, t(10.0)))
            .unwrap();
        g.ingest(&obs(PLACES[2].0, PLACES[2].1, 40.0, t(9.9)))
            .unwrap();
        g.ingest(&obs(PLACES[2].0, PLACES[2].1, 0.0, t(10.0)))
            .unwrap();
        g.ingest(&obs(PLACES[3].0, PLACES[3].1, 50.0, t(0.0)))
            .unwrap(); // 10 h old: stale
        g.ingest(&obs(PLACES[4].0, PLACES[4].1, 30.0, t(-3.0)))
            .unwrap(); // 13 h old: expired
        let now = t(10.0);
        let s = summarise(
            g.views(now),
            &RoutePolicy::default(),
            &FusionParams::default(),
            now,
        );
        assert_eq!(s.cells, 35);
        assert_eq!(s.readings, 6);
        assert_eq!(s.expired, 7);
        assert_eq!(s.stale, 7);
        assert_eq!(s.cells_in_conflict, 1);
        assert_eq!(s.conflicting_readings, 1);
        let c = s.by_class;
        assert_eq!(
            c.low + c.danger_some + c.danger_most + c.danger_all + c.unknown,
            35
        );
        assert_eq!(s.unknown, c.unknown);
        // The deep fresh cell is danger_most; the dry one is low.
        assert!(c.danger_most >= 1 && c.low >= 1);
        // Stale 50 cm and expired 30 cm need fresh reports; their neighbours
        // carry no direct level of their own.
        assert_eq!(s.needs_fresh_report, 2);
        assert_eq!(s.needs_fresh_report_cells[0].last_depth_cm, 50.0);
        assert!(!s.needs_fresh_report_cells[0].expired);
        assert!(s.needs_fresh_report_cells[1].expired);
        assert_eq!(s.newest_reading_at, Some(t(10.0)));
        assert!(s.blocked.car >= 2 && s.blocked.bus >= 1);
        assert!(s.blocked.car >= s.blocked.bus);
    }

    fn point(h: f64, depth: f64, sigma: f64) -> HistoryPoint {
        HistoryPoint {
            at: t(h),
            reading_cm: depth,
            reading_sigma_cm: sigma,
            fused_cm: depth,
            fused_sigma_cm: sigma,
            migrated: false,
        }
    }

    #[test]
    fn drain_rate_on_a_clean_line() {
        // 60 cm falling 5 cm an hour, read every half hour for 3 hours.
        let pts: Vec<HistoryPoint> = (0..7)
            .map(|i| point(0.5 * f64::from(i), 60.0 - 2.5 * f64::from(i), 3.0))
            .collect();
        let d = drain_estimate(&pts).unwrap();
        assert!((d.rate_cm_per_h - 5.0).abs() < 1e-9);
        assert_eq!(d.points_used, 7);
        assert_eq!(d.trend, Trend::Falling);
        // Level now 45 cm, 30 cm above the limit, at 5 cm/h: 6 hours.
        assert!((d.hours_to_15_cm.unwrap() - 6.0).abs() < 1e-9);
        // Known-variance standard error: sigma / sqrt(sum (t - tbar)^2).
        let stt: f64 = (0..7).map(|i| (0.5 * f64::from(i) - 1.5).powi(2)).sum();
        assert!((d.rate_sigma_cm_per_h - 3.0 / stt.sqrt()).abs() < 1e-9);
    }

    #[test]
    fn drain_rate_refuses_thin_data_and_reports_noise() {
        assert!(drain_estimate(&[])
            .unwrap_err()
            .contains("no direct readings"));
        let two = [point(0.0, 50.0, 3.0), point(1.0, 40.0, 3.0)];
        assert!(drain_estimate(&two).unwrap_err().contains("at least 3"));
        let quick = [
            point(0.0, 50.0, 3.0),
            point(0.1, 45.0, 3.0),
            point(0.2, 40.0, 3.0),
        ];
        assert!(drain_estimate(&quick).unwrap_err().contains("minutes"));
        // Old readings outside the 6 hour window are ignored.
        let mut old = vec![point(-10.0, 90.0, 3.0), point(-9.0, 80.0, 3.0)];
        old.extend([
            point(0.0, 30.0, 3.0),
            point(1.0, 30.0, 3.0),
            point(2.0, 30.0, 3.0),
        ]);
        let d = drain_estimate(&old).unwrap();
        assert_eq!(d.points_used, 3);
        assert_eq!(d.trend, Trend::Steady);
        assert!(d.hours_to_15_cm.is_none());
        // Rising water.
        let up = [
            point(0.0, 10.0, 2.0),
            point(1.0, 20.0, 2.0),
            point(2.0, 30.0, 2.0),
        ];
        assert_eq!(drain_estimate(&up).unwrap().trend, Trend::Rising);
        // Scatter well beyond the stated sigmas widens the error and hides the trend.
        let noisy = [
            point(0.0, 40.0, 1.0),
            point(0.5, 10.0, 1.0),
            point(1.0, 45.0, 1.0),
            point(1.5, 12.0, 1.0),
            point(2.0, 38.0, 1.0),
        ];
        let d = drain_estimate(&noisy).unwrap();
        assert!(d.rate_sigma_cm_per_h > 5.0, "{}", d.rate_sigma_cm_per_h);
        assert_eq!(d.trend, Trend::Steady);
        // Migrated points are skipped.
        let mut m = point(-0.5, 99.0, 3.0);
        m.migrated = true;
        let mut with_migrated = vec![m];
        with_migrated.extend(up);
        assert_eq!(drain_estimate(&with_migrated).unwrap().points_used, 3);
    }

    #[test]
    fn drain_rate_from_a_real_grid_history() {
        let mut g = grid();
        let mut cell: Option<CellIndex> = None;
        for i in 0..5 {
            let h = f64::from(i) * 0.75;
            cell = Some(
                g.ingest(&obs(PLACES[0].0, PLACES[0].1, 50.0 - 4.0 * h, t(h)))
                    .unwrap()
                    .cell,
            );
        }
        let h = &g.state(cell.unwrap()).unwrap().history;
        let pts: Vec<HistoryPoint> = h.iter().copied().collect();
        let d = drain_estimate(&pts).unwrap();
        assert!((d.rate_cm_per_h - 4.0).abs() < 1e-6, "{}", d.rate_cm_per_h);
        assert_eq!(d.trend, Trend::Falling);
    }
}
