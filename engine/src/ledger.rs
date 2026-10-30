//! The risk ledger: a long-lived record, per area, of what direct readings
//! have shown, for choosing where to build.
//!
//! The live map forgets a cell 48 hours after its last evidence, and each
//! cell keeps only its latest readings. Siting needs the opposite: how often
//! an area has had water, how deep it got and how long it took to go down,
//! over a whole monsoon. The ledger keeps that, small and summarised, from
//! the moment it starts. It never reads or changes the map, and routing never
//! reads it.
//!
//! # What is recorded
//!
//! Every fused direct reading adds to the area (an H3 cell at
//! `ledger_resolution`, 8 by default, about 0.7 square kilometres) holding
//! its map cell:
//!
//! - `readings`, and `wet_readings` when the cell's fused direct level just
//!   after the reading is at or over [`WET_CM`] (15 cm, the lowest vehicle
//!   limit);
//! - the India Standard Time date of each wet reading (`days_with_water`);
//! - the peak fused level, with its sigma and time;
//! - wet spells per map cell: a spell starts at the first wet reading and
//!   ends at the first later reading below [`WET_CM`] in the same cell. Its
//!   length is a recession time. A spell whose cell stops being reported for
//!   [`UNRESOLVED_AFTER_H`] hours while still wet is counted as unresolved: the
//!   water may have gone down, or nobody went back to look.
//!
//! These are coarse. A spell's length is bounded by when people happened to
//! report, so it can overstate or understate the real recession, and an area
//! with few reports says little. An area with no reports is unknown, never
//! dry; the ledger only lists areas someone has reported in.
//!
//! # Buckets
//!
//! Readings that carry a `source` (`photo`, `video`, `voice`, `official`)
//! go to the `reported` bucket. Readings with none (the gateway's simulated
//! scenario sends none, and neither do most tests) go to `unattributed`, so
//! simulated water can never become part of an area's flood record.

use chrono::{DateTime, FixedOffset, Utc};
use h3o::{CellIndex, LatLng, Resolution};
use serde::{Deserialize, Serialize};
use std::collections::HashMap;

/// A fused level at or above this is wet, cm.
pub const WET_CM: f64 = 15.0;
/// Hours without a reading after which an open wet spell is unresolved.
pub const UNRESOLVED_AFTER_H: f64 = 48.0;
/// Recession times kept per area for the median (the newest are kept).
pub const EPISODES_KEPT: usize = 500;

/// Format of the ledger file.
pub const FORMAT: &str = "neermattam-engine-ledger";
/// Version of the ledger file.
pub const VERSION: u32 = 1;

fn ist() -> FixedOffset {
    FixedOffset::east_opt(5 * 3600 + 1800).expect("valid offset")
}

/// Days since 1970-01-01 of the India Standard Time date of `t`.
fn ist_day(t: DateTime<Utc>) -> i32 {
    let local = t.with_timezone(&ist()).date_naive();
    (local - chrono::NaiveDate::from_ymd_opt(1970, 1, 1).expect("epoch")).num_days() as i32
}

fn hours_between(a: DateTime<Utc>, b: DateTime<Utc>) -> f64 {
    (b - a).num_milliseconds() as f64 / 3_600_000.0
}

/// One area's record.
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
pub struct AreaRecord {
    /// Direct readings.
    pub readings: u32,
    /// Readings whose fused level was wet.
    pub wet_readings: u32,
    /// IST dates (days since 1970) with a wet reading, sorted.
    pub wet_days: Vec<i32>,
    /// Deepest fused level, cm.
    pub peak_cm: f64,
    /// Its sigma, cm.
    pub peak_sigma_cm: f64,
    /// When.
    pub peak_at: Option<DateTime<Utc>>,
    /// Wet spells seen to end.
    pub episodes: u32,
    /// The newest spell lengths, hours (at most [`EPISODES_KEPT`]).
    pub recession_h: Vec<f32>,
    /// Longest spell seen to end, hours.
    pub max_recession_h: f64,
    /// Spells whose end was never seen.
    pub unresolved: u32,
    /// First reading.
    pub first_at: Option<DateTime<Utc>>,
    /// Latest reading.
    pub last_at: Option<DateTime<Utc>>,
}

/// An open wet spell in one map cell.
#[derive(Clone, Copy, Debug, PartialEq, Serialize, Deserialize)]
struct WetSpell {
    since: DateTime<Utc>,
    last: DateTime<Utc>,
    area: u64,
}

/// One bucket of the ledger.
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
pub struct Bucket {
    areas: HashMap<u64, AreaRecord>,
    wet: HashMap<u64, WetSpell>,
}

impl Bucket {
    /// Areas with a record.
    #[must_use]
    pub fn len(&self) -> usize {
        self.areas.len()
    }

    /// True when nothing is recorded.
    #[must_use]
    pub fn is_empty(&self) -> bool {
        self.areas.is_empty()
    }

    /// The record of one area.
    #[must_use]
    pub fn area(&self, area: u64) -> Option<&AreaRecord> {
        self.areas.get(&area)
    }

    /// Open spells per area.
    #[must_use]
    pub fn open_by_area(&self) -> HashMap<u64, u32> {
        let mut out = HashMap::new();
        for s in self.wet.values() {
            *out.entry(s.area).or_insert(0) += 1;
        }
        out
    }

    /// Every area and its record.
    pub fn areas(&self) -> impl Iterator<Item = (&u64, &AreaRecord)> {
        self.areas.iter()
    }

    fn record(&mut self, cell: u64, area: u64, at: DateTime<Utc>, fused_cm: f64, sigma_cm: f64) {
        let r = self.areas.entry(area).or_default();
        r.readings += 1;
        r.first_at = Some(r.first_at.map_or(at, |f| f.min(at)));
        r.last_at = Some(r.last_at.map_or(at, |l| l.max(at)));
        let wet = fused_cm >= WET_CM;
        if wet {
            r.wet_readings += 1;
            let d = ist_day(at);
            if let Err(i) = r.wet_days.binary_search(&d) {
                r.wet_days.insert(i, d);
            }
        }
        if r.peak_at.is_none() || fused_cm > r.peak_cm {
            r.peak_cm = fused_cm;
            r.peak_sigma_cm = sigma_cm;
            r.peak_at = Some(at);
        }
        match (wet, self.wet.get_mut(&cell)) {
            (true, Some(s)) => {
                s.since = s.since.min(at);
                s.last = s.last.max(at);
            }
            (true, None) => {
                self.wet.insert(
                    cell,
                    WetSpell {
                        since: at,
                        last: at,
                        area,
                    },
                );
            }
            (false, Some(s)) if at > s.since => {
                // The first dry reading after the spell began ends it. A dry
                // reading dated before the spell began (it arrived late) says
                // nothing about its end.
                let h = hours_between(s.since, at);
                let area = s.area;
                self.wet.remove(&cell);
                let r = self.areas.entry(area).or_default();
                r.episodes += 1;
                r.max_recession_h = r.max_recession_h.max(h);
                r.recession_h.push(h as f32);
                if r.recession_h.len() > EPISODES_KEPT {
                    r.recession_h.remove(0);
                }
            }
            _ => {}
        }
    }

    fn maintain(&mut self, now: DateTime<Utc>) -> usize {
        let stale: Vec<u64> = self
            .wet
            .iter()
            .filter(|(_, s)| hours_between(s.last, now) > UNRESOLVED_AFTER_H)
            .map(|(c, _)| *c)
            .collect();
        for c in &stale {
            if let Some(s) = self.wet.remove(c) {
                self.areas.entry(s.area).or_default().unresolved += 1;
            }
        }
        stale.len()
    }
}

/// The ledger: two buckets and when it began.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct Ledger {
    /// File format name.
    pub format: String,
    /// File format version.
    pub version: u32,
    /// When the ledger began recording.
    pub since: DateTime<Utc>,
    /// H3 resolution of the areas.
    pub resolution: u8,
    /// Readings with a source.
    pub reported: Bucket,
    /// Readings without one (simulated or test input).
    pub unattributed: Bucket,
}

impl Ledger {
    /// An empty ledger beginning now.
    #[must_use]
    pub fn new(resolution: Resolution, now: DateTime<Utc>) -> Self {
        Ledger {
            format: FORMAT.into(),
            version: VERSION,
            since: now,
            resolution: u8::from(resolution),
            reported: Bucket::default(),
            unattributed: Bucket::default(),
        }
    }

    /// The H3 resolution of the areas.
    #[must_use]
    pub fn area_resolution(&self) -> Resolution {
        Resolution::try_from(self.resolution).unwrap_or(Resolution::Eight)
    }

    /// The area holding a map cell.
    #[must_use]
    pub fn area_of(&self, cell: CellIndex) -> Option<CellIndex> {
        cell.parent(self.area_resolution())
    }

    /// Record one fused direct reading: the map cell, when the water was
    /// seen, the cell's fused direct level just after it and that level's
    /// sigma, and whether the reading carried a source.
    pub fn record(
        &mut self,
        cell: CellIndex,
        at: DateTime<Utc>,
        fused_cm: f64,
        sigma_cm: f64,
        attributed: bool,
    ) {
        let Some(area) = self.area_of(cell) else {
            return;
        };
        if !(fused_cm.is_finite() && sigma_cm.is_finite()) {
            return;
        }
        let b = if attributed {
            &mut self.reported
        } else {
            &mut self.unattributed
        };
        b.record(u64::from(cell), u64::from(area), at, fused_cm, sigma_cm);
    }

    /// Close spells whose cells went quiet while wet. Returns how many.
    pub fn maintain(&mut self, now: DateTime<Utc>) -> usize {
        self.reported.maintain(now) + self.unattributed.maintain(now)
    }

    /// One bucket by name.
    #[must_use]
    pub fn bucket(&self, reported: bool) -> &Bucket {
        if reported {
            &self.reported
        } else {
            &self.unattributed
        }
    }

    /// The ledger as JSON.
    #[must_use]
    pub fn encode(&self) -> Vec<u8> {
        serde_json::to_vec(self).expect("ledger serialises")
    }

    /// Read a ledger written by [`Ledger::encode`]. It must have the same
    /// area resolution.
    pub fn decode(bytes: &[u8], resolution: Resolution) -> Result<Self, String> {
        let l: Ledger = serde_json::from_slice(bytes).map_err(|e| e.to_string())?;
        if l.format != FORMAT || l.version != VERSION {
            return Err(format!(
                "not a {FORMAT} version {VERSION} file (found {} {})",
                l.format, l.version
            ));
        }
        if l.resolution != u8::from(resolution) {
            return Err(format!(
                "areas are at H3 resolution {}, the engine uses {}",
                l.resolution,
                u8::from(resolution)
            ));
        }
        Ok(l)
    }
}

/// One area as `/v1/siting` returns it.
#[derive(Clone, Debug, PartialEq, Serialize)]
pub struct AreaOut {
    /// H3 index of the area.
    pub area: String,
    /// Centre latitude.
    pub lat: f64,
    /// Centre longitude.
    pub lon: f64,
    /// Outline, `[lon, lat]`, closed.
    pub boundary: Vec<[f64; 2]>,
    /// Direct readings.
    pub readings: u32,
    /// Wet readings.
    pub wet_readings: u32,
    /// IST dates with a wet reading.
    pub days_with_water: usize,
    /// Deepest fused level, cm.
    pub peak_cm: f64,
    /// Its sigma, cm.
    pub peak_sigma_cm: f64,
    /// When.
    pub peak_at: Option<DateTime<Utc>>,
    /// Wet spells seen to end.
    pub episodes: u32,
    /// Median of the kept spell lengths, hours.
    pub median_recession_h: Option<f64>,
    /// Longest spell seen to end, hours.
    pub max_recession_h: Option<f64>,
    /// Wet now.
    pub open_episodes: u32,
    /// Reports stopped while wet.
    pub unresolved_episodes: u32,
    /// First reading.
    pub first_at: Option<DateTime<Utc>>,
    /// Latest reading.
    pub last_at: Option<DateTime<Utc>>,
}

fn r1(x: f64) -> f64 {
    (x * 10.0).round() / 10.0
}

fn r6(x: f64) -> f64 {
    (x * 1e6).round() / 1e6
}

/// Describe one area. `open` is its number of open spells.
#[must_use]
pub fn area_out(area: CellIndex, r: &AreaRecord, open: u32) -> AreaOut {
    let c = LatLng::from(area);
    let mut boundary: Vec<[f64; 2]> = area
        .boundary()
        .iter()
        .map(|ll| [r6(ll.lng()), r6(ll.lat())])
        .collect();
    if let Some(f) = boundary.first().copied() {
        boundary.push(f);
    }
    let median = if r.recession_h.is_empty() {
        None
    } else {
        let mut v: Vec<f64> = r.recession_h.iter().map(|x| f64::from(*x)).collect();
        v.sort_by(f64::total_cmp);
        let n = v.len();
        Some(if n % 2 == 1 {
            v[n / 2]
        } else {
            (v[n / 2 - 1] + v[n / 2]) / 2.0
        })
    };
    AreaOut {
        area: area.to_string(),
        lat: r6(c.lat()),
        lon: r6(c.lng()),
        boundary,
        readings: r.readings,
        wet_readings: r.wet_readings,
        days_with_water: r.wet_days.len(),
        peak_cm: r1(r.peak_cm),
        peak_sigma_cm: r1(r.peak_sigma_cm),
        peak_at: r.peak_at,
        episodes: r.episodes,
        median_recession_h: median.map(r1),
        max_recession_h: (r.episodes > 0).then(|| r1(r.max_recession_h)),
        open_episodes: open,
        unresolved_episodes: r.unresolved,
        first_at: r.first_at,
        last_at: r.last_at,
    }
}

/// Order areas for siting: more days with water first, then deeper peaks,
/// then more wet readings, then area id.
pub fn rank(areas: &mut [AreaOut]) {
    areas.sort_by(|a, b| {
        b.days_with_water
            .cmp(&a.days_with_water)
            .then(b.peak_cm.total_cmp(&a.peak_cm))
            .then(b.wet_readings.cmp(&a.wet_readings))
            .then(a.area.cmp(&b.area))
    });
}

#[cfg(test)]
mod tests {
    use super::*;
    use chrono::TimeZone;

    fn t(h: f64) -> DateTime<Utc> {
        Utc.with_ymd_and_hms(2026, 11, 2, 0, 0, 0).unwrap()
            + chrono::Duration::milliseconds((h * 3_600_000.0) as i64)
    }

    fn cell(lat: f64, lon: f64) -> CellIndex {
        LatLng::new(lat, lon).unwrap().to_cell(Resolution::Ten)
    }

    #[test]
    fn a_wet_spell_ends_at_the_first_dry_reading_and_is_counted_once() {
        let mut l = Ledger::new(Resolution::Eight, t(0.0));
        let c = cell(13.0, 80.2);
        let area = u64::from(l.area_of(c).unwrap());
        l.record(c, t(0.0), 40.0, 3.0, true);
        l.record(c, t(2.0), 30.0, 3.0, true);
        l.record(c, t(5.0), 20.0, 3.0, true);
        l.record(c, t(9.0), 5.0, 3.0, true);
        l.record(c, t(10.0), 2.0, 3.0, true);
        let r = l.reported.area(area).unwrap();
        assert_eq!(r.readings, 5);
        assert_eq!(r.wet_readings, 3);
        assert_eq!(r.episodes, 1);
        assert!((r.max_recession_h - 9.0).abs() < 1e-9);
        assert_eq!(r.peak_cm, 40.0);
        assert!(l.unattributed.is_empty(), "attributed readings stay apart");
        let out = area_out(l.area_of(c).unwrap(), r, 0);
        assert_eq!(out.median_recession_h, Some(9.0));
        assert_eq!(out.days_with_water, 1);
        assert_eq!(out.boundary.first(), out.boundary.last());
    }

    #[test]
    fn days_are_india_dates_and_quiet_wet_cells_become_unresolved() {
        let mut l = Ledger::new(Resolution::Eight, t(0.0));
        let c = cell(13.0, 80.2);
        let area = u64::from(l.area_of(c).unwrap());
        // 17:00 and 19:00 UTC on 2 November are 22:30 on 2 November and
        // 00:30 on 3 November in India.
        l.record(c, t(17.0), 20.0, 3.0, true);
        l.record(c, t(19.0), 22.0, 3.0, true);
        assert_eq!(l.reported.area(area).unwrap().wet_days.len(), 2);
        assert_eq!(l.reported.open_by_area().get(&area), Some(&1));
        assert_eq!(l.maintain(t(19.0 + UNRESOLVED_AFTER_H - 1.0)), 0);
        assert_eq!(l.maintain(t(19.0 + UNRESOLVED_AFTER_H + 1.0)), 1);
        let r = l.reported.area(area).unwrap();
        assert_eq!((r.unresolved, r.episodes), (1, 0));
        assert!(l.reported.open_by_area().is_empty());
    }

    #[test]
    fn unattributed_readings_never_reach_the_reported_record() {
        let mut l = Ledger::new(Resolution::Eight, t(0.0));
        let c = cell(13.0, 80.2);
        l.record(c, t(0.0), 60.0, 3.0, false);
        assert!(l.reported.is_empty());
        assert_eq!(l.unattributed.len(), 1);
    }

    #[test]
    fn a_late_dry_reading_does_not_end_a_spell_it_predates() {
        let mut l = Ledger::new(Resolution::Eight, t(0.0));
        let c = cell(13.0, 80.2);
        l.record(c, t(3.0), 30.0, 3.0, true);
        l.record(c, t(1.0), 0.0, 3.0, true);
        assert_eq!(l.reported.open_by_area().len(), 1);
        let area = u64::from(l.area_of(c).unwrap());
        assert_eq!(l.reported.area(area).unwrap().episodes, 0);
    }

    #[test]
    fn round_trips_and_refuses_another_resolution() {
        let mut l = Ledger::new(Resolution::Eight, t(0.0));
        l.record(cell(13.0, 80.2), t(1.0), 25.0, 2.0, true);
        let back = Ledger::decode(&l.encode(), Resolution::Eight).unwrap();
        assert_eq!(back, l);
        assert!(Ledger::decode(&l.encode(), Resolution::Seven).is_err());
        assert!(Ledger::decode(b"{}", Resolution::Eight).is_err());
    }

    #[test]
    fn ranking_puts_frequent_deep_areas_first() {
        let mk = |id: &str, days: usize, peak: f64| AreaOut {
            area: id.into(),
            lat: 0.0,
            lon: 0.0,
            boundary: vec![],
            readings: 1,
            wet_readings: 1,
            days_with_water: days,
            peak_cm: peak,
            peak_sigma_cm: 1.0,
            peak_at: None,
            episodes: 0,
            median_recession_h: None,
            max_recession_h: None,
            open_episodes: 0,
            unresolved_episodes: 0,
            first_at: None,
            last_at: None,
        };
        let mut v = vec![mk("a", 1, 80.0), mk("b", 3, 20.0), mk("c", 3, 40.0)];
        rank(&mut v);
        let ids: Vec<&str> = v.iter().map(|a| a.area.as_str()).collect();
        assert_eq!(ids, ["c", "b", "a"]);
    }
}
