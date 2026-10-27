//! Configuration from environment variables, each with a default.
//!
//! | Variable | Default | Meaning |
//! |---|---|---|
//! | `ENGINE_BIND` | `127.0.0.1:8302` | Listen address; keep it on the internal network |
//! | `ENGINE_OSM_PATH` | `../data/osm/chennai-roads.osm.pbf` | Road extract |
//! | `ENGINE_SNAPSHOT_PATH` | `../data/engine/cells.json` | Cell snapshot; empty disables |
//! | `ENGINE_SNAPSHOT_INTERVAL_SECS` | `5` | Minimum seconds between snapshots |
//! | `ENGINE_H3_RESOLUTION` | `10` | H3 resolution (7 to 12) |
//! | `ENGINE_BBOX` | `80.10,12.85,80.33,13.24` | Service area, minLon,minLat,maxLon,maxLat |
//! | `ENGINE_HALF_LIFE_HOURS` | `off` | Opt-in recession half-life; `off` holds the level |
//! | `ENGINE_DEPTH_NOISE_CM_PER_SQRT_HOUR` | `6` | Depth process noise |
//! | `ENGINE_VELOCITY_NOISE_MPS_PER_SQRT_HOUR` | `0.2` | Velocity process noise |
//! | `ENGINE_NEIGHBOUR_SIGMA_CM` | `8` | Extra sigma for ring cells |
//! | `ENGINE_NEIGHBOUR_VELOCITY_SIGMA_MPS` | `0.3` | Extra velocity sigma for ring cells |
//! | `ENGINE_UNKNOWN_SIGMA_CM` | `10` | Depth sigma above which a cell is unknown |
//! | `ENGINE_UNKNOWN_VELOCITY_SIGMA_MPS` | `0.5` | Velocity sigma above which velocity is unknown |
//! | `ENGINE_STALE_HOURS` | `6` | Hours without evidence before a cell is unknown |
//! | `ENGINE_EXPIRE_HOURS` | `12` | Hours after its latest reading that evidence stops counting |
//! | `ENGINE_FORGET_HOURS` | `48` | Hours without evidence before a cell is dropped |
//! | `ENGINE_SAFETY_SIGMAS` | `1.5` | k in mean + k min(sigma, cap) |
//! | `ENGINE_BLOCK_SIGMA_CAP_CM` | `10` | Depth sigma cap in the blocking rule |
//! | `ENGINE_BLOCK_VELOCITY_SIGMA_CAP_MPS` | `0.5` | Velocity sigma cap in the blocking rule |
//! | `ENGINE_BLOCK_GROWTH_CAP_CM` | `5` | Cap on growth with time in the blocking rule |
//! | `ENGINE_UNKNOWN_PENALTY` | `1.5` | Cost multiplier through unknown cells |
//! | `ENGINE_DEFAULT_VELOCITY_MPS` | `1.0` | Velocity assumed when not measured |
//! | `ENGINE_PEDESTRIAN_BLOCK_CLASS` | `danger_some` | Lowest class that blocks walkers |
//! | `ENGINE_DEBRIS_RULE` | `fd2320` | `fd2320`, `fd2321_urban`, `fd2321_woodland`, `fd2321_pasture` |
//! | `ENGINE_MAX_SNAP_M` | `1500` | Furthest a route end may be from a road |
//! | `ENGINE_MAX_OBSERVATION_SKEW_HOURS` | `24` | How old `observed_at` may be; at most 5 minutes ahead is always allowed |
//! | `ENGINE_BODY_LIMIT_BYTES` | `16384` | Request body limit |
//! | `ENGINE_REQUEST_TIMEOUT_SECS` | `15` | Timeout for non-streaming requests |
//! | `ENGINE_MAX_CELLS` | `20000` | Most features one `/v1/cells` call returns |
//! | `ENGINE_LOG_FORMAT` | `json` | `json` or `pretty`; filter with `RUST_LOG` |

use crate::fusion::FusionParams;
use crate::geo::BBox;
use crate::hazard::{DebrisRule, HazardClass};
use crate::routing::RoutePolicy;
use h3o::Resolution;
use std::net::SocketAddr;
use std::path::PathBuf;
use std::str::FromStr;
use std::time::Duration;

/// A configuration variable could not be parsed.
#[derive(Debug, thiserror::Error)]
#[error("invalid {var}={value:?}: {reason}")]
pub struct ConfigError {
    var: &'static str,
    value: String,
    reason: String,
}

/// Engine configuration.
#[derive(Clone, Debug)]
pub struct Config {
    /// Listen address.
    pub bind: SocketAddr,
    /// OSM PBF extract of highway ways.
    pub osm_path: PathBuf,
    /// Snapshot file, or `None` to disable persistence.
    pub snapshot_path: Option<PathBuf>,
    /// Minimum time between snapshots.
    pub snapshot_interval: Duration,
    /// H3 resolution.
    pub resolution: Resolution,
    /// Readings and route ends must fall inside this box.
    pub service_bbox: BBox,
    /// Fusion tuning.
    pub fusion: FusionParams,
    /// Routing policy.
    pub policy: RoutePolicy,
    /// How old `observed_at` may be, hours. The future limit is fixed at
    /// [`crate::api::MAX_FUTURE_SKEW_SECS`].
    pub max_observation_skew_h: f64,
    /// Request body limit, bytes.
    pub body_limit_bytes: usize,
    /// Timeout for non-streaming requests.
    pub request_timeout: Duration,
    /// Most features one cells query returns.
    pub max_cells: usize,
    /// JSON logs (true) or human-readable logs.
    pub log_json: bool,
    /// How many recent reading ids are remembered for de-duplication.
    pub dedupe_capacity: usize,
}

impl Default for Config {
    fn default() -> Self {
        Config {
            bind: SocketAddr::from(([127, 0, 0, 1], 8302)),
            osm_path: PathBuf::from("../data/osm/chennai-roads.osm.pbf"),
            snapshot_path: Some(PathBuf::from("../data/engine/cells.json")),
            snapshot_interval: Duration::from_secs(5),
            resolution: Resolution::Ten,
            service_bbox: BBox::CHENNAI,
            fusion: FusionParams::default(),
            policy: RoutePolicy::default(),
            max_observation_skew_h: 24.0,
            body_limit_bytes: 16 * 1024,
            request_timeout: Duration::from_secs(15),
            max_cells: 20_000,
            log_json: true,
            dedupe_capacity: 100_000,
        }
    }
}

fn parse<T: FromStr>(
    get: &impl Fn(&str) -> Option<String>,
    var: &'static str,
    target: &mut T,
    check: impl Fn(&T) -> Result<(), String>,
) -> Result<(), ConfigError>
where
    T::Err: std::fmt::Display,
{
    if let Some(raw) = get(var) {
        let value = raw.trim().to_string();
        let parsed = value.parse::<T>().map_err(|e| ConfigError {
            var,
            value: value.clone(),
            reason: e.to_string(),
        })?;
        check(&parsed).map_err(|reason| ConfigError {
            var,
            value: value.clone(),
            reason,
        })?;
        *target = parsed;
    }
    Ok(())
}

fn positive(x: &f64) -> Result<(), String> {
    if x.is_finite() && *x > 0.0 {
        Ok(())
    } else {
        Err("must be a positive number".into())
    }
}

fn non_negative(x: &f64) -> Result<(), String> {
    if x.is_finite() && *x >= 0.0 {
        Ok(())
    } else {
        Err("must be zero or more".into())
    }
}

fn any<T>(_: &T) -> Result<(), String> {
    Ok(())
}

impl Config {
    /// True when the bind address is loopback or a private (RFC 1918, ULA)
    /// address. The engine has no authentication and trusts the `trust`
    /// field, so it must only be reachable from the gateway.
    #[must_use]
    pub fn bind_is_internal(&self) -> bool {
        match self.bind.ip() {
            std::net::IpAddr::V4(ip) => ip.is_loopback() || ip.is_private(),
            std::net::IpAddr::V6(ip) => ip.is_loopback() || (ip.segments()[0] & 0xfe00) == 0xfc00,
        }
    }

    /// Read from the process environment.
    pub fn from_env() -> Result<Self, ConfigError> {
        Self::from_lookup(|k| std::env::var(k).ok())
    }

    /// Read from any key lookup (used by tests).
    pub fn from_lookup(get: impl Fn(&str) -> Option<String>) -> Result<Self, ConfigError> {
        let mut c = Config::default();
        parse(&get, "ENGINE_BIND", &mut c.bind, any)?;
        parse(&get, "ENGINE_OSM_PATH", &mut c.osm_path, any)?;
        if let Some(p) = get("ENGINE_SNAPSHOT_PATH") {
            c.snapshot_path = (!p.trim().is_empty()).then(|| PathBuf::from(p.trim()));
        }
        let mut secs = c.snapshot_interval.as_secs_f64();
        parse(&get, "ENGINE_SNAPSHOT_INTERVAL_SECS", &mut secs, positive)?;
        c.snapshot_interval = Duration::from_secs_f64(secs);
        let mut res: u8 = u8::from(c.resolution);
        parse(&get, "ENGINE_H3_RESOLUTION", &mut res, |r| {
            if (7..=12).contains(r) {
                Ok(())
            } else {
                Err("must be between 7 and 12".into())
            }
        })?;
        c.resolution = Resolution::try_from(res).expect("checked range");
        parse(&get, "ENGINE_BBOX", &mut c.service_bbox, any)?;

        let f = &mut c.fusion;
        if let Some(raw) = get("ENGINE_HALF_LIFE_HOURS") {
            let raw = raw.trim();
            f.half_life_h = if matches!(raw, "" | "off" | "none") {
                None
            } else {
                let mut h = 0.0;
                parse(
                    &|_: &str| Some(raw.to_string()),
                    "ENGINE_HALF_LIFE_HOURS",
                    &mut h,
                    positive,
                )?;
                Some(h)
            };
        }
        parse(
            &get,
            "ENGINE_DEPTH_NOISE_CM_PER_SQRT_HOUR",
            &mut f.depth_noise_cm_per_sqrt_h,
            non_negative,
        )?;
        parse(
            &get,
            "ENGINE_VELOCITY_NOISE_MPS_PER_SQRT_HOUR",
            &mut f.velocity_noise_mps_per_sqrt_h,
            non_negative,
        )?;
        parse(
            &get,
            "ENGINE_NEIGHBOUR_SIGMA_CM",
            &mut f.neighbour_depth_sigma_cm,
            non_negative,
        )?;
        parse(
            &get,
            "ENGINE_NEIGHBOUR_VELOCITY_SIGMA_MPS",
            &mut f.neighbour_velocity_sigma_mps,
            non_negative,
        )?;
        parse(
            &get,
            "ENGINE_UNKNOWN_SIGMA_CM",
            &mut f.unknown_depth_sigma_cm,
            positive,
        )?;
        parse(
            &get,
            "ENGINE_UNKNOWN_VELOCITY_SIGMA_MPS",
            &mut f.unknown_velocity_sigma_mps,
            positive,
        )?;
        parse(&get, "ENGINE_STALE_HOURS", &mut f.stale_after_h, positive)?;
        parse(&get, "ENGINE_EXPIRE_HOURS", &mut f.expire_after_h, positive)?;
        parse(&get, "ENGINE_FORGET_HOURS", &mut f.forget_after_h, positive)?;

        let p = &mut c.policy;
        parse(
            &get,
            "ENGINE_SAFETY_SIGMAS",
            &mut p.safety_sigmas,
            non_negative,
        )?;
        parse(
            &get,
            "ENGINE_BLOCK_SIGMA_CAP_CM",
            &mut p.sigma_cap_cm,
            non_negative,
        )?;
        parse(
            &get,
            "ENGINE_BLOCK_VELOCITY_SIGMA_CAP_MPS",
            &mut p.velocity_sigma_cap_mps,
            non_negative,
        )?;
        parse(
            &get,
            "ENGINE_BLOCK_GROWTH_CAP_CM",
            &mut p.growth_cap_cm,
            non_negative,
        )?;
        parse(
            &get,
            "ENGINE_UNKNOWN_PENALTY",
            &mut p.unknown_penalty,
            |x| {
                if x.is_finite() && *x >= 1.0 {
                    Ok(())
                } else {
                    Err("must be at least 1".into())
                }
            },
        )?;
        parse(
            &get,
            "ENGINE_DEFAULT_VELOCITY_MPS",
            &mut p.default_velocity_mps,
            non_negative,
        )?;
        parse(
            &get,
            "ENGINE_PEDESTRIAN_BLOCK_CLASS",
            &mut p.pedestrian_block_class,
            any::<HazardClass>,
        )?;
        parse(
            &get,
            "ENGINE_DEBRIS_RULE",
            &mut p.debris_rule,
            any::<DebrisRule>,
        )?;
        parse(&get, "ENGINE_MAX_SNAP_M", &mut p.max_snap_m, positive)?;

        parse(
            &get,
            "ENGINE_MAX_OBSERVATION_SKEW_HOURS",
            &mut c.max_observation_skew_h,
            positive,
        )?;
        parse(
            &get,
            "ENGINE_BODY_LIMIT_BYTES",
            &mut c.body_limit_bytes,
            |b| {
                if (1024..=1 << 20).contains(b) {
                    Ok(())
                } else {
                    Err("must be between 1024 and 1048576".into())
                }
            },
        )?;
        let mut timeout = c.request_timeout.as_secs_f64();
        parse(&get, "ENGINE_REQUEST_TIMEOUT_SECS", &mut timeout, positive)?;
        c.request_timeout = Duration::from_secs_f64(timeout);
        parse(&get, "ENGINE_MAX_CELLS", &mut c.max_cells, |n| {
            if *n > 0 {
                Ok(())
            } else {
                Err("must be positive".into())
            }
        })?;
        let mut fmt = "json".to_string();
        parse(&get, "ENGINE_LOG_FORMAT", &mut fmt, |s: &String| {
            if s == "json" || s == "pretty" {
                Ok(())
            } else {
                Err("must be json or pretty".into())
            }
        })?;
        c.log_json = fmt == "json";
        Ok(c)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::HashMap;

    fn lookup(pairs: &[(&str, &str)]) -> impl Fn(&str) -> Option<String> {
        let m: HashMap<String, String> = pairs
            .iter()
            .map(|(k, v)| ((*k).into(), (*v).into()))
            .collect();
        move |k| m.get(k).cloned()
    }

    #[test]
    fn defaults_without_environment() {
        let c = Config::from_lookup(|_| None).unwrap();
        assert_eq!(c.bind.port(), 8302);
        // The engine is internal: by default it listens on loopback only.
        assert!(c.bind.ip().is_loopback());
        assert!(c.bind_is_internal());
        // No recession unless asked for; evidence expires after 12 hours.
        assert_eq!(c.fusion.half_life_h, None);
        assert_eq!(c.fusion.expire_after_h, 12.0);
        assert_eq!(c.policy.sigma_cap_cm, 10.0);
        assert_eq!(c.policy.growth_cap_cm, 5.0);
        assert_eq!(c.resolution, Resolution::Ten);
        assert_eq!(c.service_bbox, BBox::CHENNAI);
        assert_eq!(c.policy.safety_sigmas, 1.5);
    }

    #[test]
    fn overrides_and_errors() {
        let c = Config::from_lookup(lookup(&[
            ("ENGINE_H3_RESOLUTION", "9"),
            ("ENGINE_HALF_LIFE_HOURS", "5"),
            ("ENGINE_SNAPSHOT_PATH", ""),
            ("ENGINE_PEDESTRIAN_BLOCK_CLASS", "danger_most"),
            ("ENGINE_LOG_FORMAT", "pretty"),
        ]))
        .unwrap();
        assert_eq!(c.resolution, Resolution::Nine);
        assert_eq!(c.fusion.half_life_h, Some(5.0));
        assert!(c.snapshot_path.is_none());
        assert_eq!(c.policy.pedestrian_block_class, HazardClass::DangerMost);
        assert!(!c.log_json);

        let e = Config::from_lookup(lookup(&[("ENGINE_H3_RESOLUTION", "15")])).unwrap_err();
        assert!(e.to_string().contains("ENGINE_H3_RESOLUTION"));
        assert!(Config::from_lookup(lookup(&[("ENGINE_HALF_LIFE_HOURS", "-1")])).is_err());
        assert!(Config::from_lookup(lookup(&[("ENGINE_UNKNOWN_PENALTY", "0.5")])).is_err());
        assert!(Config::from_lookup(lookup(&[("ENGINE_BIND", "nope")])).is_err());
        let c = Config::from_lookup(lookup(&[
            ("ENGINE_HALF_LIFE_HOURS", "off"),
            ("ENGINE_EXPIRE_HOURS", "8"),
            ("ENGINE_BLOCK_SIGMA_CAP_CM", "12"),
            ("ENGINE_BIND", "0.0.0.0:8302"),
        ]))
        .unwrap();
        assert_eq!(c.fusion.half_life_h, None);
        assert_eq!(c.fusion.expire_after_h, 8.0);
        assert_eq!(c.policy.sigma_cap_cm, 12.0);
        assert!(!c.bind_is_internal());
    }
}
