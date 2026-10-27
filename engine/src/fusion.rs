//! Fusion of flood depth and velocity readings on an H3 hex grid.
//!
//! # Model
//!
//! Each cell carries scalar Gaussian estimates of water depth `x ~ N(mu, P)`
//! in centimetres and, once anyone has measured it, flow velocity
//! `u ~ N(mu_u, P_u)` in metres per second. Both follow the same linear
//! model, which is a one-dimensional Kalman filter.
//!
//! **Prediction (holding the level).** Between time `t` and `t + dt` (hours):
//!
//! ```text
//! mu'    = mu                            the last level is held
//! P'     = P + q^2 x dt                  uncertainty grows as a random walk
//! ```
//!
//! `q` is the process noise in cm per square-root hour (6 by default): a cell
//! that nobody has looked at for an hour gains 6 cm of standard deviation,
//! for four hours 12 cm. The engine does not assume water recedes: Chennai
//! streets can stay flooded for days, and assuming recession is what turned
//! old reports of deep water into green lights. Recession toward dry is
//! available as an opt-in (`half_life_h`), in which case
//! `a = 2^(-dt / half_life)`, `mu' = a mu` and `P' = a^2 P + q^2 dt`.
//!
//! **Update (inverse-variance weighting).** A reading `z` with standard
//! deviation `s` and trust `w` in (0, 1] is an observation with variance
//! `R = s^2 / w`. It is fused with the predicted state by
//!
//! ```text
//! mu = (mu / P + z / R) / (1 / P + 1 / R)
//! P  = 1 / (1 / P + 1 / R)
//! ```
//!
//! which is the Kalman update with gain `K = P / (P + R)`. The first reading in
//! a cell simply becomes the state. Trust is computed server-side by the
//! gateway, never taken from the person reporting.
//!
//! **Conflicting readings widen, never shrink.** Inverse-variance fusion of
//! two readings that disagree badly gives a confident answer halfway between
//! them, which is wrong for both. When a direct reading differs from the
//! cell's direct estimate by more than `3 x sqrt(sigma_old^2 + sigma_new^2)`
//! the cell counts a conflict (`conflicts`) and, with `delta` the difference:
//!
//! - a newer reading replaces the mean, with variance
//!   `max(sigma_new^2, delta^2 / 4)`;
//! - an older reading is fused with its variance inflated to at least
//!   `delta^2 / 4`.
//!
//! Either way `delta^2 / 4` becomes a floor on the cell's direct variance,
//! and the deeper of the two disagreeing means is kept as
//! `conflict_high_mean_cm`, which routing blocks on. Both last until the
//! conflict clears or `expire_after_h` hours pass. A conflict clears when
//! `CONFLICT_CLEAR_READINGS` (5) consecutive direct readings, each observed at
//! least `CONFLICT_CLEAR_AFTER_MIN` (30) minutes after the deeper side was
//! reported, agree with the cell's current mean within
//! `3 x sqrt(sigma_meas_cell^2 + sigma_meas_reading^2)`. A reading that
//! does not agree resets the count.
//!
//! **Measurement uncertainty.** Each track also carries `meas_var`, the
//! variance of the same fusion with no growth with time (plus the neighbour
//! offset for neighbour tracks). Routing blocks on it and a bounded growth
//! term, while the displayed sigma and the unknown flag use the full,
//! growing variance.
//!
//! **Out-of-order readings.** A reading whose `observed_at` is earlier than
//! the cell's state time is carried forward to the state time with the same
//! prediction step (`R' = R + q^2 dt`) and then fused. With one earlier state
//! this gives exactly the in-order answer. With three or more readings it is
//! an approximation: the carried reading and the state both include the
//! process noise of the interval they share, so the fused sigma comes out a
//! few percent smaller than in time order.
//!
//! **Neighbours.** A reading also informs the six cells of its k = 1 ring.
//! Adjacent cells are about 66 m apart at resolution 10 and the street may
//! slope, so the depth next door differs from the depth here by an unknown
//! offset with standard deviation `n` (`neighbour_depth_sigma_cm`, 8 cm by
//! default). That offset is the same for every report from the same
//! neighbour, so it does not average away: each cell keeps the readings from
//! each adjacent cell in a separate track, fused at their own variance, and
//! adds `n^2` to that track once, when the cell is viewed. One reading from a
//! neighbour therefore counts with variance `R + n^2`, and any number of
//! readings from the same neighbour never count for less than `n^2`. Fifty
//! reports of a dry street cannot outvote one direct report of deep water in
//! the underpass beside it. The cell counts neighbour evidence separately
//! (`n_neighbour`) from direct readings (`n_readings`).
//!
//! **Input floors.** An observation's standard deviation is floored at
//! `MIN_OBS_DEPTH_SIGMA_CM` (1 cm) and `MIN_OBS_VELOCITY_SIGMA_MPS`
//! (0.05 m/s), so one over-confident report cannot pin a cell, and every
//! variance is capped at `MAX_VARIANCE` so it stays finite and serialisable.
//!
//! **Unknown.** A cell is reported as unknown when its depth standard
//! deviation, predicted to now, exceeds `unknown_depth_sigma_cm` (10 cm), or
//! when no evidence has arrived for `stale_after_h` hours (6 h). Unknown is
//! never shown as safe, and an unknown cell that still holds evidence keeps
//! blocking vehicles by the same rule as a known one (see
//! [`crate::routing`]).
//!
//! **Expiry.** Each track (the direct readings, and the readings from each
//! adjacent cell) expires `expire_after_h` hours (12 h) after its latest
//! reading. An expired track no longer counts. When every track has
//! expired the cell is `expired`: unknown, never blocking, and reporting the
//! last direct level (`last_depth_cm`) and its age so a map can say "last
//! report 40 cm, 13 h ago". Cells are dropped from memory after
//! `forget_after_h` hours (48 h).
//!
//! **Velocity.** Velocity uses the same steps with its own noise rate
//! (`velocity_noise_mps_per_sqrt_h`) and neighbour inflation. Velocity is
//! shown as unknown when it has never been observed, its standard deviation
//! exceeds `unknown_velocity_sigma_mps`, or the cell is stale; callers then
//! substitute a conservative default and flag it.

use chrono::{DateTime, Duration, Utc};
use h3o::{CellIndex, LatLng, Resolution};
use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::hash::{BuildHasherDefault, Hasher};
use thiserror::Error;

/// Smallest variance the filter will hold, to keep the weights finite.
const MIN_VARIANCE: f64 = 1e-6;

/// Largest variance the filter will hold. A very low trust can push `s^2 / w`
/// past `f64::MAX`; an infinite variance serialises as JSON `null`, which
/// would make the next snapshot unloadable and wipe the map on restart.
pub const MAX_VARIANCE: f64 = 1e12;

/// Smallest depth standard deviation accepted from one observation, cm.
pub const MIN_OBS_DEPTH_SIGMA_CM: f64 = 1.0;

/// Smallest velocity standard deviation accepted from one observation, m/s.
pub const MIN_OBS_VELOCITY_SIGMA_MPS: f64 = 0.05;

/// A direct reading conflicts with the cell when it differs by more than this
/// many combined standard deviations.
pub const CONFLICT_SIGMAS: f64 = 3.0;

/// A conflict clears after this many consecutive direct readings agree with
/// the cell's current estimate...
pub const CONFLICT_CLEAR_READINGS: u32 = 5;

/// ...each observed at least this many minutes after the deeper side of the
/// conflict was reported.
pub const CONFLICT_CLEAR_AFTER_MIN: i64 = 30;

fn clamp_var(v: f64) -> f64 {
    v.clamp(MIN_VARIANCE, MAX_VARIANCE)
}

/// Tuning for the fusion model. See the module docs.
#[derive(Clone, Copy, Debug, PartialEq, Serialize, Deserialize)]
pub struct FusionParams {
    /// Half-life of an opt-in recession toward dry, hours. `None`, the
    /// default, holds the last level.
    pub half_life_h: Option<f64>,
    /// Depth process noise, cm per square-root hour.
    pub depth_noise_cm_per_sqrt_h: f64,
    /// Velocity process noise, m/s per square-root hour.
    pub velocity_noise_mps_per_sqrt_h: f64,
    /// Extra depth standard deviation for a neighbouring cell, cm.
    pub neighbour_depth_sigma_cm: f64,
    /// Extra velocity standard deviation for a neighbouring cell, m/s.
    pub neighbour_velocity_sigma_mps: f64,
    /// Depth sigma above which a cell is unknown, cm.
    pub unknown_depth_sigma_cm: f64,
    /// Velocity sigma above which velocity is unknown, m/s.
    pub unknown_velocity_sigma_mps: f64,
    /// Hours without evidence after which a cell is unknown.
    pub stale_after_h: f64,
    /// Hours after its latest reading at which a track stops counting.
    pub expire_after_h: f64,
    /// Hours without evidence after which a cell is dropped from memory.
    pub forget_after_h: f64,
}

impl Default for FusionParams {
    fn default() -> Self {
        FusionParams {
            half_life_h: None,
            depth_noise_cm_per_sqrt_h: 6.0,
            velocity_noise_mps_per_sqrt_h: 0.2,
            neighbour_depth_sigma_cm: 8.0,
            neighbour_velocity_sigma_mps: 0.3,
            unknown_depth_sigma_cm: 10.0,
            unknown_velocity_sigma_mps: 0.5,
            stale_after_h: 6.0,
            expire_after_h: 12.0,
            forget_after_h: 48.0,
        }
    }
}

impl FusionParams {
    /// Recession factor for `dt_h` hours: 1 (hold the level) unless a
    /// half-life is configured, then `2^(-dt / half_life)`.
    #[must_use]
    pub fn decay(&self, dt_h: f64) -> f64 {
        match self.half_life_h {
            Some(h) if dt_h > 0.0 && h.is_finite() && h > 0.0 => (-dt_h / h).exp2(),
            _ => 1.0,
        }
    }

    fn expire_after(&self) -> Duration {
        Duration::milliseconds((self.expire_after_h * 3_600_000.0) as i64)
    }
}

/// A scalar Gaussian: mean and variance.
#[derive(Clone, Copy, Debug, PartialEq, Serialize, Deserialize)]
pub struct Gaussian {
    /// Mean.
    pub mean: f64,
    /// Variance (sigma squared).
    pub var: f64,
}

impl Gaussian {
    /// From a mean and a standard deviation.
    #[must_use]
    pub fn new(mean: f64, sigma: f64) -> Self {
        Gaussian {
            mean,
            var: clamp_var(sigma * sigma),
        }
    }

    /// Standard deviation.
    #[must_use]
    pub fn sigma(&self) -> f64 {
        self.var.sqrt()
    }

    /// `mean + k x sigma`.
    #[must_use]
    pub fn upper(&self, k: f64) -> f64 {
        self.mean + k * self.sigma()
    }

    /// `mean + k x min(sigma, cap)`: an upper estimate that never falls when
    /// sigma grows.
    #[must_use]
    pub fn upper_capped(&self, k: f64, cap: f64) -> f64 {
        self.mean + k * self.sigma().min(cap)
    }

    /// Kalman prediction: scale the mean by `a`, the variance by `a^2`, and
    /// add `noise^2 x dt_h`.
    #[must_use]
    pub fn predict(self, a: f64, noise_per_sqrt_h: f64, dt_h: f64) -> Self {
        let dt = dt_h.max(0.0);
        Gaussian {
            mean: a * self.mean,
            var: clamp_var(a * a * self.var + noise_per_sqrt_h * noise_per_sqrt_h * dt),
        }
    }

    /// Inverse-variance fusion of two independent estimates.
    #[must_use]
    pub fn fuse(self, other: Gaussian) -> Self {
        let w1 = 1.0 / self.var.max(MIN_VARIANCE);
        let w2 = 1.0 / other.var.max(MIN_VARIANCE);
        Gaussian {
            mean: (w1 * self.mean + w2 * other.mean) / (w1 + w2),
            var: 1.0 / (w1 + w2),
        }
    }

    /// The same Gaussian with `extra_sigma` added in quadrature.
    #[must_use]
    pub fn inflate(self, extra_sigma: f64) -> Self {
        Gaussian {
            mean: self.mean,
            var: clamp_var(self.var + extra_sigma * extra_sigma),
        }
    }

    fn with_var_at_least(self, floor: f64) -> Self {
        Gaussian {
            mean: self.mean,
            var: clamp_var(self.var.max(floor)),
        }
    }

    fn is_valid(&self) -> bool {
        self.mean.is_finite() && self.var.is_finite() && self.var > 0.0
    }
}

/// Hours from `a` to `b` (negative when `b` is earlier).
fn hours_between(a: DateTime<Utc>, b: DateTime<Utc>) -> f64 {
    (b - a).num_milliseconds() as f64 / 3_600_000.0
}

/// Fused depth and velocity from one source of evidence, valid at the
/// owning cell's `as_of`.
#[derive(Clone, Copy, Debug, PartialEq, Serialize, Deserialize)]
pub struct Track {
    /// Depth, cm. Its variance includes growth with time.
    pub depth: Gaussian,
    /// Variance of the fused measurements alone, cm^2: the same fusion with
    /// no growth with time. Blocking uses it (see [`crate::routing`]).
    pub meas_var: f64,
    /// Velocity, m/s, if ever observed.
    pub velocity: Option<Gaussian>,
}

impl Track {
    fn predict(self, dt_h: f64, p: &FusionParams) -> Self {
        let a = p.decay(dt_h);
        Track {
            depth: self.depth.predict(a, p.depth_noise_cm_per_sqrt_h, dt_h),
            meas_var: self.meas_var,
            velocity: self
                .velocity
                .map(|v| v.predict(a, p.velocity_noise_mps_per_sqrt_h, dt_h)),
        }
    }

    fn fuse(self, other: Track) -> Self {
        Track {
            depth: self.depth.fuse(other.depth),
            meas_var: fuse_var(self.meas_var, other.meas_var),
            velocity: fuse_velocity(self.velocity, other.velocity),
        }
    }

    fn inflate(self, p: &FusionParams) -> Self {
        Track {
            depth: self.depth.inflate(p.neighbour_depth_sigma_cm),
            meas_var: clamp_var(self.meas_var + p.neighbour_depth_sigma_cm.powi(2)),
            velocity: self
                .velocity
                .map(|v| v.inflate(p.neighbour_velocity_sigma_mps)),
        }
    }

    fn is_valid(&self) -> bool {
        self.depth.is_valid()
            && self.meas_var.is_finite()
            && self.meas_var > 0.0
            && self.velocity.is_none_or(|v| v.is_valid())
    }
}

fn fuse_var(a: f64, b: f64) -> f64 {
    clamp_var(1.0 / (1.0 / a.max(MIN_VARIANCE) + 1.0 / b.max(MIN_VARIANCE)))
}

fn fuse_velocity(a: Option<Gaussian>, b: Option<Gaussian>) -> Option<Gaussian> {
    match (a, b) {
        (Some(s), Some(o)) => Some(s.fuse(o)),
        (Some(x), None) | (None, Some(x)) => Some(x),
        (None, None) => None,
    }
}

/// Readings from one adjacent cell, before the spatial offset is added.
#[derive(Clone, Copy, Debug, PartialEq, Serialize, Deserialize)]
pub struct NeighbourTrack {
    /// Raw H3 index of the adjacent cell the readings were taken in.
    pub from: u64,
    /// Latest `observed_at` of these readings.
    pub last_at: DateTime<Utc>,
    /// The fused readings.
    #[serde(flatten)]
    pub track: Track,
}

/// One piece of evidence about one cell.
#[derive(Clone, Copy, Debug)]
struct Evidence {
    track: Track,
    at: DateTime<Utc>,
    /// `None` for a reading taken in this cell, else the adjacent cell.
    from: Option<u64>,
}

/// Stored state of one cell.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct CellState {
    /// Readings taken in this cell, fused. `None` when only neighbours have
    /// reported.
    pub direct: Option<Track>,
    /// Readings taken in each adjacent cell, one track per adjacent cell.
    pub neighbours: Vec<NeighbourTrack>,
    /// Time the estimates are valid at.
    pub as_of: DateTime<Utc>,
    /// Latest `observed_at` of any evidence, direct or neighbour.
    pub last_evidence_at: DateTime<Utc>,
    /// Latest `observed_at` of a direct reading.
    #[serde(default)]
    pub last_direct_at: Option<DateTime<Utc>>,
    /// The direct depth estimate just after the latest direct reading, cm.
    #[serde(default)]
    pub last_depth_cm: Option<f64>,
    /// Direct readings that disagreed with the cell (see the module docs).
    #[serde(default)]
    pub conflicts: u32,
    /// Floor on the direct variance left by the latest conflicts, cm^2.
    #[serde(default)]
    pub conflict_floor_var: f64,
    /// When the conflict floor and the conflict's deep mean stop applying.
    #[serde(default)]
    pub conflict_until: Option<DateTime<Utc>>,
    /// The deeper side of the active conflict, cm. Routing blocks on it.
    #[serde(default)]
    pub conflict_high_mean_cm: Option<f64>,
    /// When the deeper side of the active conflict was reported.
    #[serde(default)]
    pub conflict_high_at: Option<DateTime<Utc>>,
    /// Consecutive agreeing direct readings counted toward clearing it.
    #[serde(default)]
    pub conflict_agree: u32,
    /// Direct readings fused into this cell.
    pub n_readings: u32,
    /// Neighbour readings fused into this cell.
    pub n_neighbour: u32,
}

impl CellState {
    fn empty(at: DateTime<Utc>) -> Self {
        CellState {
            direct: None,
            neighbours: Vec::new(),
            as_of: at,
            last_evidence_at: at,
            last_direct_at: None,
            last_depth_cm: None,
            conflicts: 0,
            conflict_floor_var: 0.0,
            conflict_until: None,
            conflict_high_mean_cm: None,
            conflict_high_at: None,
            conflict_agree: 0,
            n_readings: 0,
            n_neighbour: 0,
        }
    }

    /// True when the state holds evidence and every number is usable.
    #[must_use]
    pub fn is_valid(&self) -> bool {
        (self.direct.is_some() || !self.neighbours.is_empty())
            && self.direct.is_none_or(|t| t.is_valid())
            && self.neighbours.iter().all(|n| n.track.is_valid())
            && self.conflict_floor_var.is_finite()
            && self.conflict_floor_var >= 0.0
            && self.last_depth_cm.is_none_or(f64::is_finite)
            && self.conflict_high_mean_cm.is_none_or(f64::is_finite)
    }

    /// True while a conflict is in force at `t`.
    fn conflict_active(&self, t: DateTime<Utc>) -> bool {
        self.conflict_until.is_some_and(|until| t < until)
    }

    fn clear_conflict(&mut self) {
        self.conflict_until = None;
        self.conflict_floor_var = 0.0;
        self.conflict_high_mean_cm = None;
        self.conflict_high_at = None;
        self.conflict_agree = 0;
    }

    /// Whether a direct track is in force at `t`, and the latest reading
    /// time among the tracks in force.
    fn live_info(&self, t: DateTime<Utc>, p: &FusionParams) -> (bool, Option<DateTime<Utc>>) {
        let live = |at: DateTime<Utc>| hours_between(at, t) <= p.expire_after_h;
        let direct = self
            .last_direct_at
            .filter(|at| self.direct.is_some() && live(*at));
        let newest = self
            .neighbours
            .iter()
            .map(|n| n.last_at)
            .filter(|at| live(*at))
            .chain(direct)
            .max();
        (direct.is_some(), newest)
    }

    /// The conflict floor on the direct variance in force at `t`.
    fn floor_at(&self, t: DateTime<Utc>) -> f64 {
        if self.conflict_active(t) {
            self.conflict_floor_var
        } else {
            0.0
        }
    }

    /// The direct track carried forward to `t`, with the conflict floor.
    fn direct_at(&self, t: DateTime<Utc>, p: &FusionParams) -> Option<Track> {
        let dt = hours_between(self.as_of, t).max(0.0);
        let floor = self.floor_at(t);
        self.direct.map(|d| {
            let d = d.predict(dt, p);
            Track {
                depth: d.depth.with_var_at_least(floor),
                ..d
            }
        })
    }

    /// The combined depth and velocity estimate carried forward to `t`.
    /// With `live_only`, tracks that have expired by `t` are left out.
    /// Returns `None` when no track is left.
    fn combined(&self, t: DateTime<Utc>, p: &FusionParams, live_only: bool) -> Option<Track> {
        let dt = hours_between(self.as_of, t).max(0.0);
        let live = |at: DateTime<Utc>| !live_only || hours_between(at, t) <= p.expire_after_h;
        let direct = self
            .direct_at(t, p)
            .filter(|_| self.last_direct_at.is_none_or(live));
        self.neighbours
            .iter()
            .filter(|n| live(n.last_at))
            .map(|n| n.track.predict(dt, p).inflate(p))
            .chain(direct)
            .reduce(Track::fuse)
    }

    /// The combined depth and velocity estimate carried forward to `t`,
    /// counting expired tracks too. Times before `as_of` return the stored
    /// estimate unchanged.
    #[must_use]
    pub fn predicted(&self, t: DateTime<Utc>, p: &FusionParams) -> (Gaussian, Option<Gaussian>) {
        let combined = self.combined(t, p, false).unwrap_or(Track {
            // A state without evidence: as uncertain as nothing at all.
            depth: Gaussian {
                mean: 0.0,
                var: MAX_VARIANCE,
            },
            meas_var: MAX_VARIANCE,
            velocity: None,
        });
        (combined.depth, combined.velocity)
    }

    fn fuse_direct(&mut self, obs: Track, newer: bool, at: DateTime<Utc>, p: &FusionParams) {
        let mut floor = self.floor_at(self.as_of);
        let Some(d) = self.direct_at(self.as_of, p) else {
            self.direct = Some(obs);
            return;
        };
        let delta = (obs.depth.mean - d.depth.mean).abs();
        let gate = CONFLICT_SIGMAS * (d.depth.var + obs.depth.var).sqrt();
        let fused = if delta > gate {
            self.conflicts = self.conflicts.saturating_add(1);
            let wide = delta * delta / 4.0;
            let fused = if newer {
                Track {
                    depth: obs.depth.with_var_at_least(wide),
                    meas_var: obs.meas_var,
                    velocity: fuse_velocity(d.velocity, obs.velocity),
                }
            } else {
                d.fuse(Track {
                    depth: obs.depth.with_var_at_least(wide),
                    meas_var: obs.meas_var.max(wide),
                    ..obs
                })
            };
            // Remember the deeper side, keeping a deeper one already in force.
            let (high, high_at) = if obs.depth.mean > d.depth.mean {
                (obs.depth.mean, at)
            } else {
                (d.depth.mean, self.last_direct_at.unwrap_or(at))
            };
            let active = self.conflict_active(self.as_of);
            if !(active && self.conflict_high_mean_cm.is_some_and(|h| h >= high)) {
                self.conflict_high_mean_cm = Some(high);
                self.conflict_high_at = Some(high_at);
            }
            self.conflict_until = Some(self.as_of + p.expire_after());
            floor = floor.max(wide);
            self.conflict_floor_var = floor;
            self.conflict_agree = 0;
            fused
        } else {
            if self.conflict_active(self.as_of) {
                // A reading agrees when it is within 3 measurement sigmas of
                // the current estimate. Enough agreeing readings, all taken
                // well after the deep report, clear the conflict.
                let agree_gate = CONFLICT_SIGMAS * (d.meas_var + obs.meas_var).sqrt();
                if delta <= agree_gate {
                    let after = self.conflict_high_at.unwrap_or(at)
                        + Duration::minutes(CONFLICT_CLEAR_AFTER_MIN);
                    if at >= after {
                        self.conflict_agree += 1;
                    }
                    if self.conflict_agree >= CONFLICT_CLEAR_READINGS {
                        self.clear_conflict();
                        floor = 0.0;
                    }
                } else {
                    self.conflict_agree = 0;
                }
            }
            d.fuse(obs)
        };
        self.direct = Some(Track {
            depth: fused.depth.with_var_at_least(floor),
            ..fused
        });
    }

    fn assimilate(&mut self, ev: &Evidence, p: &FusionParams) {
        let newer = ev.at >= self.as_of;
        let obs = if newer {
            let dt = hours_between(self.as_of, ev.at);
            // The floor in force is applied by `direct_at`; keep the raw track.
            self.direct = self.direct.map(|d| d.predict(dt, p));
            for n in &mut self.neighbours {
                n.track = n.track.predict(dt, p);
            }
            self.as_of = ev.at;
            ev.track
        } else {
            // Carry the late reading forward to the state time.
            ev.track.predict(hours_between(ev.at, self.as_of), p)
        };
        match ev.from {
            None => {
                self.fuse_direct(obs, newer, ev.at, p);
                self.n_readings = self.n_readings.saturating_add(1);
                self.last_direct_at = Some(self.last_direct_at.map_or(ev.at, |t| t.max(ev.at)));
                self.last_depth_cm = self.direct.map(|d| d.depth.mean.max(0.0));
            }
            Some(from) => {
                match self.neighbours.iter_mut().find(|n| n.from == from) {
                    Some(n) => {
                        n.track = n.track.fuse(obs);
                        n.last_at = n.last_at.max(ev.at);
                    }
                    None => self.neighbours.push(NeighbourTrack {
                        from,
                        last_at: ev.at,
                        track: obs,
                    }),
                }
                self.n_neighbour = self.n_neighbour.saturating_add(1);
            }
        }
        self.last_evidence_at = self.last_evidence_at.max(ev.at);
    }
}

/// What a cell looks like at a given moment.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct CellView {
    /// The cell.
    pub cell: CellIndex,
    /// Depth in cm, mean clamped at zero, from the tracks still in force
    /// (from every track when the cell has expired).
    pub depth: Gaussian,
    /// Velocity in m/s, present only when known well enough to show.
    pub velocity: Option<Gaussian>,
    /// Velocity in m/s whenever any track in force holds one, however
    /// uncertain. Routing uses it so that a growing velocity sigma never
    /// lowers the velocity it assumes.
    pub velocity_any: Option<Gaussian>,
    /// True when the evidence is too thin or too old. Never treat as safe.
    pub unknown: bool,
    /// True when every track has expired: unknown and never blocking.
    pub expired: bool,
    /// Standard deviation of the fused measurements in force, with no growth
    /// with time, including the neighbour offset where it applies, cm.
    pub sigma_meas_cm: f64,
    /// Growth with time, `q x sqrt(hours since the latest evidence in force)`, cm.
    pub growth_cm: f64,
    /// True when no direct reading is in force: all evidence is from
    /// adjacent cells.
    pub neighbour_only: bool,
    /// The deeper side of a conflict in force, cm.
    pub conflict_high_mean_cm: Option<f64>,
    /// Direct readings.
    pub n_readings: u32,
    /// Neighbour readings.
    pub n_neighbour: u32,
    /// Direct readings that disagreed with the cell.
    pub conflicts: u32,
    /// Latest evidence time.
    pub updated_at: DateTime<Utc>,
    /// The direct depth estimate just after the latest direct reading, cm.
    pub last_depth_cm: Option<f64>,
    /// Hours since the latest direct reading.
    pub last_reading_age_h: Option<f64>,
}

/// A validated observation, as accepted by [`Grid::ingest`].
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Observation {
    /// Latitude, degrees.
    pub lat: f64,
    /// Longitude, degrees.
    pub lon: f64,
    /// Depth, cm.
    pub depth_cm: f64,
    /// Depth standard deviation, cm.
    pub depth_sigma_cm: f64,
    /// Surface velocity, m/s, when measured.
    pub velocity_mps: Option<f64>,
    /// Velocity standard deviation, m/s.
    pub velocity_sigma_mps: Option<f64>,
    /// When the water was seen.
    pub observed_at: DateTime<Utc>,
    /// Trust in (0, 1], computed by the gateway; the variance is divided by it.
    pub trust: f64,
}

/// Why an observation was refused by the grid.
#[derive(Debug, Error, PartialEq)]
pub enum FusionError {
    /// Coordinates outside WGS84.
    #[error("invalid coordinates: {0}")]
    Coordinates(String),
    /// A number was missing, negative or not finite.
    #[error("invalid observation: {0}")]
    Value(&'static str),
}

/// Result of ingesting one observation.
#[derive(Clone, Debug, PartialEq)]
pub struct Ingested {
    /// The cell the reading fell in.
    pub cell: CellIndex,
    /// Every cell whose state changed: the cell and its ring.
    pub touched: Vec<CellIndex>,
}

/// The live hex map.
#[derive(Clone, Debug)]
pub struct Grid {
    params: FusionParams,
    resolution: Resolution,
    cells: HashMap<CellIndex, CellState>,
}

impl Grid {
    /// An empty grid.
    #[must_use]
    pub fn new(params: FusionParams, resolution: Resolution) -> Self {
        Grid {
            params,
            resolution,
            cells: HashMap::new(),
        }
    }

    /// The fusion parameters.
    #[must_use]
    pub fn params(&self) -> &FusionParams {
        &self.params
    }

    /// The H3 resolution.
    #[must_use]
    pub fn resolution(&self) -> Resolution {
        self.resolution
    }

    /// Number of cells holding state.
    #[must_use]
    pub fn len(&self) -> usize {
        self.cells.len()
    }

    /// True when no cell holds state.
    #[must_use]
    pub fn is_empty(&self) -> bool {
        self.cells.is_empty()
    }

    /// Raw stored states, for snapshots.
    pub fn states(&self) -> impl Iterator<Item = (&CellIndex, &CellState)> {
        self.cells.iter()
    }

    /// Replace a cell's stored state (used when loading a snapshot).
    pub fn insert_state(&mut self, cell: CellIndex, state: CellState) {
        self.cells.insert(cell, state);
    }

    /// The cell containing a point.
    pub fn cell_at(&self, lat: f64, lon: f64) -> Result<CellIndex, FusionError> {
        if !((-90.0..=90.0).contains(&lat) && (-180.0..=180.0).contains(&lon)) {
            return Err(FusionError::Coordinates(format!(
                "({lat}, {lon}) is outside WGS84"
            )));
        }
        let ll = LatLng::new(lat, lon).map_err(|e| FusionError::Coordinates(e.to_string()))?;
        Ok(ll.to_cell(self.resolution))
    }

    /// Fuse one observation into its cell and, as neighbour evidence, its
    /// k = 1 ring.
    pub fn ingest(&mut self, obs: &Observation) -> Result<Ingested, FusionError> {
        if !(obs.depth_cm.is_finite() && obs.depth_cm >= 0.0) {
            return Err(FusionError::Value("depth must be a non-negative number"));
        }
        if !(obs.depth_sigma_cm.is_finite() && obs.depth_sigma_cm > 0.0) {
            return Err(FusionError::Value("depth sigma must be positive"));
        }
        if !(obs.trust.is_finite() && obs.trust > 0.0 && obs.trust <= 1.0) {
            return Err(FusionError::Value("trust must be in (0, 1]"));
        }
        let velocity = match (obs.velocity_mps, obs.velocity_sigma_mps) {
            (None, _) => None,
            (Some(v), Some(s)) if v.is_finite() && v >= 0.0 && s.is_finite() && s > 0.0 => Some(
                Gaussian::new(v, s.max(MIN_OBS_VELOCITY_SIGMA_MPS) / obs.trust.sqrt()),
            ),
            _ => {
                return Err(FusionError::Value(
                    "velocity needs a non-negative value and a positive sigma",
                ))
            }
        };
        let cell = self.cell_at(obs.lat, obs.lon)?;
        let depth = Gaussian::new(
            obs.depth_cm,
            obs.depth_sigma_cm.max(MIN_OBS_DEPTH_SIGMA_CM) / obs.trust.sqrt(),
        );
        let track = Track {
            depth,
            meas_var: depth.var,
            velocity,
        };
        let direct = Evidence {
            track,
            at: obs.observed_at,
            from: None,
        };
        // Neighbours get the raw reading; the spatial offset is added once per
        // adjacent cell when the cell is viewed (see the module docs).
        let neighbour = Evidence {
            from: Some(u64::from(cell)),
            ..direct
        };
        let ring: Vec<CellIndex> = cell.grid_disk(1);
        let mut touched = Vec::with_capacity(ring.len());
        self.apply(cell, &direct);
        touched.push(cell);
        for n in ring.into_iter().filter(|n| *n != cell) {
            self.apply(n, &neighbour);
            touched.push(n);
        }
        Ok(Ingested { cell, touched })
    }

    fn apply(&mut self, cell: CellIndex, ev: &Evidence) {
        let params = self.params;
        self.cells
            .entry(cell)
            .or_insert_with(|| CellState::empty(ev.at))
            .assimilate(ev, &params);
    }

    /// The cell as it stands at `now`, or `None` when nothing is known.
    #[must_use]
    pub fn view(&self, cell: CellIndex, now: DateTime<Utc>) -> Option<CellView> {
        self.cells.get(&cell).map(|s| self.view_state(cell, s, now))
    }

    fn view_state(&self, cell: CellIndex, s: &CellState, now: DateTime<Utc>) -> CellView {
        let p = &self.params;
        let live = s.combined(now, p, true);
        let expired = live.is_none();
        let (direct_live, newest) = s.live_info(now, p);
        let (mut depth, meas_var, velocity_any) = match live {
            Some(t) => (t.depth, t.meas_var, t.velocity),
            None => (s.predicted(now, p).0, MAX_VARIANCE, None),
        };
        let growth_cm = newest.map_or(0.0, |at| {
            p.depth_noise_cm_per_sqrt_h * hours_between(at, now).max(0.0).sqrt()
        });
        depth.mean = depth.mean.max(0.0);
        let velocity_any = velocity_any.map(|mut v| {
            v.mean = v.mean.max(0.0);
            v
        });
        let stale = hours_between(s.last_evidence_at, now) > p.stale_after_h;
        let unknown = expired || stale || depth.sigma() > p.unknown_depth_sigma_cm;
        let velocity = velocity_any.filter(|v| !stale && v.sigma() <= p.unknown_velocity_sigma_mps);
        CellView {
            cell,
            depth,
            velocity,
            velocity_any,
            unknown,
            expired,
            sigma_meas_cm: meas_var.sqrt(),
            growth_cm,
            neighbour_only: !direct_live,
            conflict_high_mean_cm: s.conflict_high_mean_cm.filter(|_| s.conflict_active(now)),
            n_readings: s.n_readings,
            n_neighbour: s.n_neighbour,
            conflicts: s.conflicts,
            updated_at: s.last_evidence_at,
            last_depth_cm: s.last_depth_cm,
            last_reading_age_h: s.last_direct_at.map(|at| hours_between(at, now).max(0.0)),
        }
    }

    /// Every cell with state, as it stands at `now`.
    pub fn views(&self, now: DateTime<Utc>) -> impl Iterator<Item = CellView> + '_ {
        self.cells
            .iter()
            .map(move |(c, s)| self.view_state(*c, s, now))
    }

    /// A lookup table for routing: every cell with state, keyed by its raw
    /// H3 index.
    #[must_use]
    pub fn water_map(&self, now: DateTime<Utc>) -> WaterMap {
        self.views(now).map(|v| (u64::from(v.cell), v)).collect()
    }

    /// Drop cells with no evidence for `forget_after_h`. Returns how many
    /// were removed.
    pub fn forget_old(&mut self, now: DateTime<Utc>) -> usize {
        let before = self.cells.len();
        let limit = self.params.forget_after_h;
        self.cells
            .retain(|_, s| hours_between(s.last_evidence_at, now) <= limit);
        before - self.cells.len()
    }
}

/// A fast hasher for raw H3 indexes, used on the routing hot path.
///
/// The standard SipHash is built to resist hash flooding, which does not
/// apply to keys the engine computes itself. Plain identity hashing would be
/// poor: at resolution 10 the low 15 bits of every index are the same unused
/// digits. The splitmix64 finaliser spreads all 64 bits.
#[derive(Clone, Copy, Debug, Default)]
pub struct CellHasher(u64);

impl Hasher for CellHasher {
    fn finish(&self) -> u64 {
        self.0
    }

    fn write(&mut self, bytes: &[u8]) {
        for &b in bytes {
            self.write_u64(self.0.rotate_left(8) ^ u64::from(b));
        }
    }

    fn write_u64(&mut self, x: u64) {
        let mut z = x ^ self.0;
        z = (z ^ (z >> 30)).wrapping_mul(0xbf58_476d_1ce4_e5b9);
        z = (z ^ (z >> 27)).wrapping_mul(0x94d0_49bb_1331_11eb);
        self.0 = z ^ (z >> 31);
    }
}

/// Cell views keyed by raw H3 index, for fast lookup during routing.
pub type WaterMap = HashMap<u64, CellView, BuildHasherDefault<CellHasher>>;

#[cfg(test)]
mod tests {
    use super::*;
    use chrono::TimeZone;

    fn t(h: f64) -> DateTime<Utc> {
        Utc.with_ymd_and_hms(2026, 10, 28, 10, 0, 0).unwrap()
            + chrono::Duration::milliseconds((h * 3_600_000.0) as i64)
    }

    fn obs(depth: f64, sigma: f64, at: DateTime<Utc>) -> Observation {
        Observation {
            lat: 12.9791,
            lon: 80.2209,
            depth_cm: depth,
            depth_sigma_cm: sigma,
            velocity_mps: None,
            velocity_sigma_mps: None,
            observed_at: at,
            trust: 1.0,
        }
    }

    fn grid() -> Grid {
        Grid::new(FusionParams::default(), Resolution::Ten)
    }

    fn close(a: f64, b: f64, tol: f64) -> bool {
        (a - b).abs() < tol
    }

    #[test]
    fn inverse_variance_fusion() {
        let f = Gaussian::new(10.0, 2.0).fuse(Gaussian::new(20.0, 2.0));
        assert!(close(f.mean, 15.0, 1e-12) && close(f.var, 2.0, 1e-12));
        let f = Gaussian::new(10.0, 1.0).fuse(Gaussian::new(20.0, 3.0));
        assert!(close(f.mean, 11.0, 1e-12) && close(f.var, 0.9, 1e-12));
    }

    #[test]
    fn first_reading_becomes_the_state_and_second_tightens_it() {
        let mut g = grid();
        let r = g.ingest(&obs(20.0, 4.0, t(0.0))).unwrap();
        let v = g.view(r.cell, t(0.0)).unwrap();
        assert!(close(v.depth.mean, 20.0, 1e-9) && close(v.depth.sigma(), 4.0, 1e-9));
        assert_eq!(v.n_readings, 1);
        assert!(!v.unknown);
        g.ingest(&obs(30.0, 4.0, t(0.0))).unwrap();
        let v = g.view(r.cell, t(0.0)).unwrap();
        assert!(close(v.depth.mean, 25.0, 1e-9));
        assert!(close(v.depth.var, 8.0, 1e-9));
        assert_eq!(v.n_readings, 2);
    }

    #[test]
    fn the_level_is_held_and_uncertainty_grows() {
        let mut g = grid();
        let r = g.ingest(&obs(40.0, 3.0, t(0.0))).unwrap();
        let v = g.view(r.cell, t(3.0)).unwrap();
        // No recession by default: the last level is held.
        assert!(close(v.depth.mean, 40.0, 1e-9));
        // P + q^2 dt = 9 + 36 * 3
        assert!(close(v.depth.var, 9.0 + 36.0 * 3.0, 1e-6));
        // It is now too uncertain to call, but it has not expired.
        assert!(v.unknown && !v.expired);
        // A view in the past does not rewind.
        let v0 = g.view(r.cell, t(-1.0)).unwrap();
        assert!(close(v0.depth.mean, 40.0, 1e-9));
    }

    #[test]
    fn recession_is_opt_in() {
        let p = FusionParams {
            half_life_h: Some(3.0),
            ..FusionParams::default()
        };
        let mut g = Grid::new(p, Resolution::Ten);
        let r = g.ingest(&obs(40.0, 3.0, t(0.0))).unwrap();
        let v = g.view(r.cell, t(3.0)).unwrap();
        assert!(close(v.depth.mean, 20.0, 1e-6));
        assert!(close(v.depth.var, 0.25 * 9.0 + 36.0 * 3.0, 1e-6));
    }

    #[test]
    fn evidence_expires_and_leaves_the_last_level() {
        let mut g = grid();
        let r = g.ingest(&obs(40.0, 3.0, t(0.0))).unwrap();
        // Before expiry the old reading still holds 40 cm.
        let v = g.view(r.cell, t(11.9)).unwrap();
        assert!(!v.expired && close(v.depth.mean, 40.0, 1e-9));
        // After expiry the cell is unknown and says what was last seen.
        let v = g.view(r.cell, t(13.0)).unwrap();
        assert!(v.expired && v.unknown);
        assert_eq!(v.last_depth_cm, Some(40.0));
        assert!(close(v.last_reading_age_h.unwrap(), 13.0, 1e-6));
        assert!(v.velocity.is_none() && v.velocity_any.is_none());
        // A neighbour-only cell expires on its neighbour's clock.
        let n = g.view(r.touched[1], t(13.0)).unwrap();
        assert!(n.expired && n.last_depth_cm.is_none());
        // A fresh direct reading revives the cell.
        g.ingest(&obs(5.0, 3.0, t(14.0))).unwrap();
        let v = g.view(r.cell, t(14.0)).unwrap();
        assert!(!v.expired);
        assert_eq!(v.n_readings, 2);
    }

    #[test]
    fn conflicting_readings_widen_and_are_counted() {
        // Regression for fusing badly disagreeing reports: one 40 cm reading
        // and ten dry ones within the hour used to settle at about 3.6 +- 0.9 cm.
        let mut g = grid();
        let r = g.ingest(&obs(40.0, 3.0, t(0.0))).unwrap();
        for i in 1..=10 {
            g.ingest(&obs(0.0, 3.0, t(0.05 * f64::from(i)))).unwrap();
        }
        let v = g.view(r.cell, t(0.6)).unwrap();
        assert_eq!(v.conflicts, 1);
        // The newer reading takes the mean; the gap sets the floor: 40 / 2,
        // plus the 0.1 h of process noise since the last reading.
        assert!(close(v.depth.mean, 0.0, 1e-9));
        assert!(close(v.depth.var, 400.0 + 3.6, 1e-6), "{}", v.depth.var);
        assert!(v.unknown);
        // The floor lasts until the conflict ages out.
        let later = g.view(r.cell, t(11.0)).unwrap();
        assert!(later.depth.sigma() >= 20.0);

        // An older conflicting reading is fused with the gap as its variance.
        let mut g = grid();
        let r = g.ingest(&obs(0.0, 3.0, t(1.0))).unwrap();
        g.ingest(&obs(40.0, 3.0, t(0.0))).unwrap();
        let v = g.view(r.cell, t(1.0)).unwrap();
        assert_eq!(v.conflicts, 1);
        let expected = (40.0 / 400.0) / (1.0 / 9.0 + 1.0 / 400.0);
        assert!(close(v.depth.mean, expected, 1e-9), "{}", v.depth.mean);
        assert!(close(v.depth.var, 400.0, 1e-9));
    }

    #[test]
    fn agreeing_readings_still_tighten() {
        let mut g = grid();
        let r = g.ingest(&obs(20.0, 3.0, t(0.0))).unwrap();
        g.ingest(&obs(26.0, 3.0, t(0.1))).unwrap();
        let v = g.view(r.cell, t(0.1)).unwrap();
        assert_eq!(v.conflicts, 0);
        assert!(v.depth.sigma() < 3.0);
    }

    #[test]
    fn stale_cells_are_unknown_even_when_confident() {
        let p = FusionParams {
            depth_noise_cm_per_sqrt_h: 0.0,
            ..FusionParams::default()
        };
        let mut g = Grid::new(p, Resolution::Ten);
        let r = g.ingest(&obs(10.0, 1.0, t(0.0))).unwrap();
        assert!(!g.view(r.cell, t(5.9)).unwrap().unknown);
        assert!(g.view(r.cell, t(6.1)).unwrap().unknown);
    }

    #[test]
    fn out_of_order_readings_give_the_same_answer() {
        let mut a = grid();
        a.ingest(&obs(50.0, 5.0, t(0.0))).unwrap();
        let ra = a.ingest(&obs(30.0, 3.0, t(2.0))).unwrap();
        let mut b = grid();
        b.ingest(&obs(30.0, 3.0, t(2.0))).unwrap();
        let rb = b.ingest(&obs(50.0, 5.0, t(0.0))).unwrap();
        let va = a.view(ra.cell, t(2.5)).unwrap();
        let vb = b.view(rb.cell, t(2.5)).unwrap();
        assert!(close(va.depth.mean, vb.depth.mean, 1e-9));
        assert!(close(va.depth.var, vb.depth.var, 1e-9));
    }

    #[test]
    fn three_late_readings_are_slightly_overconfident() {
        // The figures quoted in docs/ENGINE.md: 30, 24 and 28 cm an hour apart.
        let run = |order: [usize; 3]| {
            let rs = [(30.0, 0.0), (24.0, 1.0), (28.0, 2.0)];
            let mut g = grid();
            let mut cell = None;
            for i in order {
                cell = Some(g.ingest(&obs(rs[i].0, 3.0, t(rs[i].1))).unwrap().cell);
            }
            let v = g.view(cell.unwrap(), t(2.0)).unwrap();
            assert_eq!(v.conflicts, 0);
            (v.depth.mean, v.depth.sigma())
        };
        let (m_in, s_in) = run([0, 1, 2]);
        let (m_late, s_late) = run([0, 2, 1]);
        assert!(
            close(s_in, 2.731, 1e-3) && close(s_late, 2.620, 1e-3),
            "{s_in} {s_late}"
        );
        assert!(
            close(m_in, 27.49, 1e-2) && close(m_late, 27.56, 1e-2),
            "{m_in} {m_late}"
        );
    }

    #[test]
    fn neighbours_get_inflated_variance() {
        let mut g = grid();
        let r = g.ingest(&obs(30.0, 3.0, t(0.0))).unwrap();
        assert_eq!(r.touched.len(), 7);
        let n = r.touched[1];
        let v = g.view(n, t(0.0)).unwrap();
        assert!(close(v.depth.mean, 30.0, 1e-9));
        assert!(close(v.depth.var, 9.0 + 64.0, 1e-9));
        assert_eq!((v.n_readings, v.n_neighbour), (0, 1));
        let centre = g.view(r.cell, t(0.0)).unwrap();
        assert_eq!((centre.n_readings, centre.n_neighbour), (1, 0));
    }

    #[test]
    fn many_neighbour_reports_cannot_outvote_a_direct_reading() {
        // Regression: an underpass with 60 cm of water next to a dry street.
        // Fifty dry reports from the street used to count as fifty
        // independent looks at the underpass and pulled it to 8 cm, clear
        // for a car.
        let mut g = grid();
        let r = g.ingest(&obs(60.0, 3.0, t(0.0))).unwrap();
        let street = h3o::LatLng::from(r.touched[1]);
        for _ in 0..50 {
            let mut o = obs(0.0, 3.0, t(0.0));
            o.lat = street.lat();
            o.lon = street.lng();
            g.ingest(&o).unwrap();
        }
        let v = g.view(r.cell, t(0.0)).unwrap();
        assert_eq!((v.n_readings, v.n_neighbour), (1, 50));
        // Direct 60 +- 3 against the street's 0 +- sqrt(9/50 + 64).
        let street_var = 9.0 / 50.0 + 64.0;
        let expected = (60.0 / 9.0) / (1.0 / 9.0 + 1.0 / street_var);
        assert!(close(v.depth.mean, expected, 1e-6), "{}", v.depth.mean);
        assert!(v.depth.mean > 50.0);
    }

    #[test]
    fn single_reports_from_different_neighbours_are_independent() {
        let mut g = grid();
        let c = g.cell_at(12.9791, 80.2209).unwrap();
        let ring: Vec<CellIndex> = c.grid_disk(1);
        let others: Vec<CellIndex> = ring.into_iter().filter(|n| *n != c).collect();
        for (cell, depth) in [(others[0], 0.0), (others[3], 40.0)] {
            let ll = h3o::LatLng::from(cell);
            let mut o = obs(depth, 3.0, t(0.0));
            o.lat = ll.lat();
            o.lon = ll.lng();
            g.ingest(&o).unwrap();
        }
        let v = g.view(c, t(0.0)).unwrap();
        assert!(close(v.depth.mean, 20.0, 1e-9));
        assert!(close(v.depth.var, 73.0 / 2.0, 1e-9));
        assert_eq!((v.n_readings, v.n_neighbour), (0, 2));
    }

    #[test]
    fn observation_sigma_is_floored_and_variance_stays_finite() {
        let mut g = grid();
        let r = g.ingest(&obs(10.0, 1e-300, t(0.0))).unwrap();
        let v = g.view(r.cell, t(0.0)).unwrap();
        assert!(close(v.depth.sigma(), MIN_OBS_DEPTH_SIGMA_CM, 1e-9));
        let mut o = obs(10.0, 3.0, t(0.0));
        o.lat = 13.05;
        o.trust = 1e-310;
        let r = g.ingest(&o).unwrap();
        let v = g.view(r.cell, t(1.0)).unwrap();
        assert!(v.depth.var.is_finite() && v.depth.var <= MAX_VARIANCE);
        assert!(v.unknown);
    }

    #[test]
    fn trust_inflates_variance() {
        let mut g = grid();
        let mut o = obs(30.0, 3.0, t(0.0));
        o.trust = 0.25;
        let r = g.ingest(&o).unwrap();
        let v = g.view(r.cell, t(0.0)).unwrap();
        assert!(close(v.depth.sigma(), 6.0, 1e-9));
    }

    #[test]
    fn velocity_is_fused_and_hidden_when_unknown() {
        let mut g = grid();
        let r = g.ingest(&obs(30.0, 3.0, t(0.0))).unwrap();
        assert!(g.view(r.cell, t(0.0)).unwrap().velocity.is_none());
        let mut o = obs(30.0, 3.0, t(0.0));
        o.velocity_mps = Some(0.8);
        o.velocity_sigma_mps = Some(0.2);
        g.ingest(&o).unwrap();
        let v = g.view(r.cell, t(0.0)).unwrap().velocity.unwrap();
        assert!(close(v.mean, 0.8, 1e-9));
        // After many hours the velocity sigma exceeds the limit.
        assert!(g.view(r.cell, t(10.0)).unwrap().velocity.is_none());
    }

    #[test]
    fn rejects_bad_values() {
        let mut g = grid();
        assert!(g.ingest(&obs(-1.0, 3.0, t(0.0))).is_err());
        assert!(g.ingest(&obs(1.0, 0.0, t(0.0))).is_err());
        assert!(g.ingest(&obs(f64::NAN, 1.0, t(0.0))).is_err());
        let mut o = obs(1.0, 1.0, t(0.0));
        o.velocity_mps = Some(1.0);
        assert!(g.ingest(&o).is_err());
        let mut o = obs(1.0, 1.0, t(0.0));
        o.trust = 0.0;
        assert!(g.ingest(&o).is_err());
        let mut o = obs(1.0, 1.0, t(0.0));
        o.lat = 95.0;
        assert!(g.ingest(&o).is_err());
        assert!(g.is_empty());
    }

    #[test]
    fn forgets_old_cells() {
        let mut g = grid();
        g.ingest(&obs(30.0, 3.0, t(0.0))).unwrap();
        assert_eq!(g.forget_old(t(47.0)), 0);
        assert_eq!(g.forget_old(t(49.0)), 7);
        assert!(g.is_empty());
    }
}
