//! Fusion of flood depth and velocity readings on an H3 hex grid.
//!
//! # Model
//!
//! Each cell carries two independent scalar Gaussian states: water depth
//! `x ~ N(mu, P)` in centimetres and, once anyone has measured it, flow
//! velocity `u ~ N(mu_u, P_u)` in metres per second. Both follow the same
//! linear model, which is a one-dimensional Kalman filter.
//!
//! **Prediction (time decay).** Between time `t` and `t + dt` (hours):
//!
//! ```text
//! a      = 2^(-dt / T_half)              recession factor, T_half = 3 h by default
//! mu'    = a x mu                        the mean relaxes toward dry (0)
//! P'     = a^2 x P + q^2 x dt            uncertainty grows as a random walk
//! ```
//!
//! `q` is the process noise in cm per square-root hour (6 by default): a cell
//! that nobody has looked at for an hour gains 6 cm of standard deviation,
//! for four hours 12 cm. Floodwater recedes over hours after rain stops, so
//! an old report should both shrink toward dry and become less trusted. The
//! two effects are independent knobs (`half_life_h` and
//! `depth_noise_cm_per_sqrt_h`). The half-life and the noise rate are
//! engineering assumptions for Chennai street flooding, not fitted values.
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
//! a cell simply becomes the state.
//!
//! **Out-of-order readings.** A reading whose `observed_at` is earlier than
//! the cell's state time is carried forward to the state time with the same
//! prediction step (`z' = a z`, `R' = a^2 R + q^2 dt`) and then fused. Because
//! fusion is commutative, the result is identical to having received the
//! readings in time order.
//!
//! **Neighbours.** A reading also informs the six cells of its k = 1 ring,
//! with the observation variance inflated to `R + n^2`, where `n` is
//! `neighbour_depth_sigma_cm` (8 cm by default): adjacent cells are about
//! 66 m away at resolution 10 and the street may slope. Treating repeated
//! neighbour evidence as independent is an approximation: many reports in one
//! cell will make its neighbours look more certain than they are. The cell
//! counts neighbour evidence separately (`n_neighbour`) from direct readings
//! (`n_readings`).
//!
//! **Unknown.** A cell is reported as unknown when its depth standard
//! deviation, predicted to now, exceeds `unknown_depth_sigma_cm` (10 cm), or
//! when no evidence has arrived for `stale_after_h` hours (6 h). Unknown is
//! never shown as safe. The 10 cm default is chosen so that a known cell with
//! zero mean has an upper bound (`mean + 1.5 sigma`) of at most 15 cm, the
//! lowest vehicle limit. Otherwise a vague report of a dry street would block
//! cars while an unreported street would not.
//!
//! **Velocity.** Velocity uses the same steps with its own noise rate
//! (`velocity_noise_mps_per_sqrt_h`) and neighbour inflation. Velocity is
//! treated as unknown when it has never been observed or its standard deviation
//! exceeds `unknown_velocity_sigma_mps`; callers then substitute a
//! conservative default and flag it.

use chrono::{DateTime, Utc};
use h3o::{CellIndex, LatLng, Resolution};
use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::hash::{BuildHasherDefault, Hasher};
use thiserror::Error;

/// Smallest variance the filter will hold, to keep the weights finite.
const MIN_VARIANCE: f64 = 1e-6;

/// Tuning for the fusion model. See the module docs.
#[derive(Clone, Copy, Debug, PartialEq, Serialize, Deserialize)]
pub struct FusionParams {
    /// Half-life of the recession toward dry, hours.
    pub half_life_h: f64,
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
    /// Hours without evidence after which a cell is dropped from memory.
    pub forget_after_h: f64,
}

impl Default for FusionParams {
    fn default() -> Self {
        FusionParams {
            half_life_h: 3.0,
            depth_noise_cm_per_sqrt_h: 6.0,
            velocity_noise_mps_per_sqrt_h: 0.2,
            neighbour_depth_sigma_cm: 8.0,
            neighbour_velocity_sigma_mps: 0.3,
            unknown_depth_sigma_cm: 10.0,
            unknown_velocity_sigma_mps: 0.5,
            stale_after_h: 6.0,
            forget_after_h: 48.0,
        }
    }
}

impl FusionParams {
    /// Recession factor `a = 2^(-dt / half_life)` for `dt_h` hours.
    #[must_use]
    pub fn decay(&self, dt_h: f64) -> f64 {
        if dt_h <= 0.0 || !self.half_life_h.is_finite() || self.half_life_h <= 0.0 {
            return 1.0;
        }
        (-dt_h / self.half_life_h).exp2()
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
            var: (sigma * sigma).max(MIN_VARIANCE),
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

    /// Kalman prediction: scale the mean by `a`, the variance by `a^2`, and
    /// add `noise^2 x dt_h`.
    #[must_use]
    pub fn predict(self, a: f64, noise_per_sqrt_h: f64, dt_h: f64) -> Self {
        let dt = dt_h.max(0.0);
        Gaussian {
            mean: a * self.mean,
            var: (a * a * self.var + noise_per_sqrt_h * noise_per_sqrt_h * dt).max(MIN_VARIANCE),
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
            var: self.var + extra_sigma * extra_sigma,
        }
    }
}

/// Hours from `a` to `b` (negative when `b` is earlier).
fn hours_between(a: DateTime<Utc>, b: DateTime<Utc>) -> f64 {
    (b - a).num_milliseconds() as f64 / 3_600_000.0
}

/// One piece of evidence about one cell.
#[derive(Clone, Copy, Debug)]
struct Evidence {
    depth: Gaussian,
    velocity: Option<Gaussian>,
    at: DateTime<Utc>,
    direct: bool,
}

/// Stored state of one cell.
#[derive(Clone, Copy, Debug, PartialEq, Serialize, Deserialize)]
pub struct CellState {
    /// Depth estimate in cm, valid at `as_of`.
    pub depth: Gaussian,
    /// Velocity estimate in m/s, valid at `as_of`, if ever observed.
    pub velocity: Option<Gaussian>,
    /// Time the estimates are valid at.
    pub as_of: DateTime<Utc>,
    /// Latest `observed_at` of any evidence, direct or neighbour.
    pub last_evidence_at: DateTime<Utc>,
    /// Direct readings fused into this cell.
    pub n_readings: u32,
    /// Neighbour readings fused into this cell.
    pub n_neighbour: u32,
}

impl CellState {
    fn from_evidence(ev: &Evidence) -> Self {
        CellState {
            depth: ev.depth,
            velocity: ev.velocity,
            as_of: ev.at,
            last_evidence_at: ev.at,
            n_readings: u32::from(ev.direct),
            n_neighbour: u32::from(!ev.direct),
        }
    }

    /// The state carried forward to `t`. Times before `as_of` return the
    /// stored state unchanged.
    #[must_use]
    pub fn predicted(&self, t: DateTime<Utc>, p: &FusionParams) -> (Gaussian, Option<Gaussian>) {
        let dt = hours_between(self.as_of, t).max(0.0);
        let a = p.decay(dt);
        (
            self.depth.predict(a, p.depth_noise_cm_per_sqrt_h, dt),
            self.velocity
                .map(|v| v.predict(a, p.velocity_noise_mps_per_sqrt_h, dt)),
        )
    }

    fn assimilate(&mut self, ev: &Evidence, p: &FusionParams) {
        let (depth_obs, vel_obs) = if ev.at >= self.as_of {
            let (d, v) = self.predicted(ev.at, p);
            self.depth = d;
            self.velocity = v;
            self.as_of = ev.at;
            (ev.depth, ev.velocity)
        } else {
            // Carry the late reading forward to the state time.
            let dt = hours_between(ev.at, self.as_of);
            let a = p.decay(dt);
            (
                ev.depth.predict(a, p.depth_noise_cm_per_sqrt_h, dt),
                ev.velocity
                    .map(|v| v.predict(a, p.velocity_noise_mps_per_sqrt_h, dt)),
            )
        };
        self.depth = self.depth.fuse(depth_obs);
        self.velocity = match (self.velocity, vel_obs) {
            (Some(s), Some(o)) => Some(s.fuse(o)),
            (None, Some(o)) => Some(o),
            (s, None) => s,
        };
        self.last_evidence_at = self.last_evidence_at.max(ev.at);
        if ev.direct {
            self.n_readings = self.n_readings.saturating_add(1);
        } else {
            self.n_neighbour = self.n_neighbour.saturating_add(1);
        }
    }
}

/// What a cell looks like at a given moment.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct CellView {
    /// The cell.
    pub cell: CellIndex,
    /// Depth in cm, mean clamped at zero.
    pub depth: Gaussian,
    /// Velocity in m/s, present only when known well enough to use.
    pub velocity: Option<Gaussian>,
    /// True when the evidence is too thin or too old. Never treat as safe.
    pub unknown: bool,
    /// Direct readings.
    pub n_readings: u32,
    /// Neighbour readings.
    pub n_neighbour: u32,
    /// Latest evidence time.
    pub updated_at: DateTime<Utc>,
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
    /// Trust in (0, 1]; the variance is divided by it.
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

    /// Fuse one observation into its cell and, with inflated variance, its
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
            (Some(v), Some(s)) if v.is_finite() && v >= 0.0 && s.is_finite() && s > 0.0 => {
                Some(Gaussian::new(v, s / obs.trust.sqrt()))
            }
            _ => {
                return Err(FusionError::Value(
                    "velocity needs a non-negative value and a positive sigma",
                ))
            }
        };
        let cell = self.cell_at(obs.lat, obs.lon)?;
        let direct = Evidence {
            depth: Gaussian::new(obs.depth_cm, obs.depth_sigma_cm / obs.trust.sqrt()),
            velocity,
            at: obs.observed_at,
            direct: true,
        };
        let neighbour = Evidence {
            depth: direct.depth.inflate(self.params.neighbour_depth_sigma_cm),
            velocity: velocity.map(|v| v.inflate(self.params.neighbour_velocity_sigma_mps)),
            at: obs.observed_at,
            direct: false,
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
            .and_modify(|s| s.assimilate(ev, &params))
            .or_insert_with(|| CellState::from_evidence(ev));
    }

    /// The cell as it stands at `now`, or `None` when nothing is known.
    #[must_use]
    pub fn view(&self, cell: CellIndex, now: DateTime<Utc>) -> Option<CellView> {
        self.cells.get(&cell).map(|s| self.view_state(cell, s, now))
    }

    fn view_state(&self, cell: CellIndex, s: &CellState, now: DateTime<Utc>) -> CellView {
        let p = &self.params;
        let (mut depth, velocity) = s.predicted(now, p);
        depth.mean = depth.mean.max(0.0);
        let stale = hours_between(s.last_evidence_at, now) > p.stale_after_h;
        let unknown = stale || depth.sigma() > p.unknown_depth_sigma_cm;
        let velocity = velocity
            .filter(|v| !stale && v.sigma() <= p.unknown_velocity_sigma_mps)
            .map(|mut v| {
                v.mean = v.mean.max(0.0);
                v
            });
        CellView {
            cell,
            depth,
            velocity,
            unknown,
            n_readings: s.n_readings,
            n_neighbour: s.n_neighbour,
            updated_at: s.last_evidence_at,
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
    fn decay_halves_the_mean_per_half_life_and_grows_variance() {
        let mut g = grid();
        let r = g.ingest(&obs(40.0, 3.0, t(0.0))).unwrap();
        let v = g.view(r.cell, t(3.0)).unwrap();
        assert!(close(v.depth.mean, 20.0, 1e-6));
        // a^2 P + q^2 dt = 0.25 * 9 + 36 * 3
        assert!(close(v.depth.var, 0.25 * 9.0 + 36.0 * 3.0, 1e-6));
        // It is now too uncertain to call.
        assert!(v.unknown);
        // A view in the past does not rewind.
        let v0 = g.view(r.cell, t(-1.0)).unwrap();
        assert!(close(v0.depth.mean, 40.0, 1e-9));
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
