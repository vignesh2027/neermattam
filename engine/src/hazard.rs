//! Flood hazard rating to people, after the UK Defra and Environment Agency
//! Flood Risks to People project.
//!
//! # Formula
//!
//! ```text
//! HR = d x (v + n) + DF        with n = 0.5
//! ```
//!
//! where `d` is depth in metres, `v` is velocity in metres per second and `DF`
//! is the debris factor (0, 0.5 or 1).
//!
//! # Sources (verified against the published documents)
//!
//! - Surendran S., Gibbs G. (Environment Agency), Wade S., Udale-Clarke H.
//!   (HR Wallingford), *Supplementary note on flood hazard ratings and
//!   thresholds for development planning and control purpose: clarification of
//!   Table 13.1 of FD2320/TR2 and Figure 3.2 of FD2321/TR1*, May 2008.
//!   It states `HR = d x (v + n) + DF` with `n` a constant of 0.5, identical in
//!   FD2320 and FD2321.
//! - Defra and Environment Agency (2006), *The Flood Risks to People
//!   Methodology*, FD2321/TR1, Table 3.1 (debris factors by depth, velocity and
//!   land use) and Table 3.2 (hazard classes).
//! - Defra and Environment Agency (2005), *Framework and Guidance for
//!   Assessing and Managing Flood Risk for New Development*, FD2320/TR2,
//!   Table 13.1 (the precautionary simplification).
//!
//! # Debris factor
//!
//! FD2321/TR1 Table 3.1 (Table 1 of the 2008 note):
//!
//! | Depth d                  | Pasture/Arable | Woodland | Urban |
//! |--------------------------|----------------|----------|-------|
//! | 0 to 0.25 m              | 0              | 0        | 0     |
//! | 0.25 to 0.75 m           | 0              | 0.5      | 1     |
//! | d > 0.75 m and/or v > 2  | 0.5            | 1        | 1     |
//!
//! FD2320/TR2 Table 13.1 instead applies a precautionary default whatever the
//! land use: DF = 0.5 for d up to and including 0.25 m, and DF = 1.0 above.
//! The 2008 note recommends the FD2320 table for planning use. The engine
//! defaults to the FD2320 precautionary rule because a street-level report
//! cannot tell us how much debris is in the water; the FD2321 urban column
//! is available through [`DebrisRule::Fd2321`].
//!
//! # Classes
//!
//! FD2320 thresholds (Table 2 and Table 4 of the 2008 note):
//!
//! | HR            | Class                                            |
//! |---------------|--------------------------------------------------|
//! | below 0.75    | Low: caution                                     |
//! | 0.75 to 1.25  | Danger for some: children, the elderly, the infirm |
//! | 1.25 to 2.0   | Danger for most: includes the general public     |
//! | above 2.0     | Danger for all: includes the emergency services  |
//!
//! FD2321 put the "danger for all" boundary at 2.5. FD2320 lowered it to 2.0
//! as a precaution; the engine uses 2.0. Table 4 of the note colours
//! HR = 0.75, 1.25 and 2.0 in the higher class, so each boundary belongs to the
//! class above it.
//!
//! # Interpretation choices (not from the source)
//!
//! - Depth exactly 0 gives HR = 0. The tables start at 5 cm, and adding a
//!   debris factor to a dry street would be meaningless.
//! - When velocity is unknown the caller supplies a conservative default (see
//!   `Config::default_velocity_mps`) and the result is flagged
//!   `velocity_assumed`. That default is an engineering judgement, not a value
//!   from the Defra documents.

use serde::{Deserialize, Serialize};
use std::fmt;
use std::str::FromStr;

/// The constant `n` in `HR = d x (v + n) + DF`.
pub const VELOCITY_CONSTANT_N: f64 = 0.5;

/// Lower bound of "danger for some".
pub const HR_DANGER_SOME: f64 = 0.75;
/// Lower bound of "danger for most".
pub const HR_DANGER_MOST: f64 = 1.25;
/// Lower bound of "danger for all" (FD2320 precautionary value).
pub const HR_DANGER_ALL: f64 = 2.0;

/// Hazard class reported for a hex cell.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum HazardClass {
    /// HR below 0.75. Caution.
    Low,
    /// HR 0.75 to 1.25. Danger for some, including children, the elderly and the infirm.
    DangerSome,
    /// HR 1.25 to 2.0. Danger for most, including the general public.
    DangerMost,
    /// HR 2.0 and above. Danger for all, including the emergency services.
    DangerAll,
    /// Too little evidence to say. Never treated as safe.
    Unknown,
}

impl HazardClass {
    /// Severity rank, 0 for low to 3 for danger for all. `None` for unknown.
    #[must_use]
    pub fn rank(self) -> Option<u8> {
        match self {
            HazardClass::Low => Some(0),
            HazardClass::DangerSome => Some(1),
            HazardClass::DangerMost => Some(2),
            HazardClass::DangerAll => Some(3),
            HazardClass::Unknown => None,
        }
    }

    /// The lowest hazard rating that falls in this class.
    #[must_use]
    pub fn lower_bound(self) -> Option<f64> {
        match self {
            HazardClass::Low => Some(0.0),
            HazardClass::DangerSome => Some(HR_DANGER_SOME),
            HazardClass::DangerMost => Some(HR_DANGER_MOST),
            HazardClass::DangerAll => Some(HR_DANGER_ALL),
            HazardClass::Unknown => None,
        }
    }

    /// The wire name, as used in `docs/CONTRACTS.md`.
    #[must_use]
    pub fn as_str(self) -> &'static str {
        match self {
            HazardClass::Low => "low",
            HazardClass::DangerSome => "danger_some",
            HazardClass::DangerMost => "danger_most",
            HazardClass::DangerAll => "danger_all",
            HazardClass::Unknown => "unknown",
        }
    }

    /// A short plain-English description.
    #[must_use]
    pub fn describe(self) -> &'static str {
        match self {
            HazardClass::Low => "low hazard, caution",
            HazardClass::DangerSome => "danger for some, including children and the elderly",
            HazardClass::DangerMost => "danger for most people",
            HazardClass::DangerAll => "danger for all, including emergency services",
            HazardClass::Unknown => "unknown",
        }
    }
}

impl fmt::Display for HazardClass {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

impl FromStr for HazardClass {
    type Err = String;
    fn from_str(s: &str) -> Result<Self, Self::Err> {
        match s {
            "low" => Ok(HazardClass::Low),
            "danger_some" => Ok(HazardClass::DangerSome),
            "danger_most" => Ok(HazardClass::DangerMost),
            "danger_all" => Ok(HazardClass::DangerAll),
            other => Err(format!(
                "unknown hazard class '{other}', expected low, danger_some, danger_most or danger_all"
            )),
        }
    }
}

/// Dominant land use, for the FD2321 debris table.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum LandUse {
    /// Pasture or arable land.
    PastureArable,
    /// Woodland.
    Woodland,
    /// Urban streets: the case for Chennai.
    Urban,
}

/// Which debris factor table to use.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum DebrisRule {
    /// FD2320/TR2 Table 13.1: 0.5 up to 0.25 m, 1.0 above. The default.
    #[default]
    Fd2320Precautionary,
    /// FD2321/TR1 Table 3.1 for the given land use.
    Fd2321(LandUse),
}

impl FromStr for DebrisRule {
    type Err = String;
    fn from_str(s: &str) -> Result<Self, Self::Err> {
        match s {
            "fd2320" => Ok(DebrisRule::Fd2320Precautionary),
            "fd2321_urban" => Ok(DebrisRule::Fd2321(LandUse::Urban)),
            "fd2321_woodland" => Ok(DebrisRule::Fd2321(LandUse::Woodland)),
            "fd2321_pasture" => Ok(DebrisRule::Fd2321(LandUse::PastureArable)),
            other => Err(format!(
                "unknown debris rule '{other}', expected fd2320, fd2321_urban, fd2321_woodland or fd2321_pasture"
            )),
        }
    }
}

/// Debris factor for a depth (metres) and velocity (m/s) under `rule`.
#[must_use]
pub fn debris_factor(rule: DebrisRule, depth_m: f64, velocity_mps: f64) -> f64 {
    if depth_m <= 0.0 {
        return 0.0;
    }
    match rule {
        DebrisRule::Fd2320Precautionary => {
            if depth_m <= 0.25 {
                0.5
            } else {
                1.0
            }
        }
        DebrisRule::Fd2321(land) => {
            let band = if depth_m > 0.75 || velocity_mps > 2.0 {
                2
            } else if depth_m > 0.25 {
                1
            } else {
                0
            };
            match (land, band) {
                (_, 0) => 0.0,
                (LandUse::PastureArable, 1) => 0.0,
                (LandUse::Woodland, 1) => 0.5,
                (LandUse::Urban, 1) => 1.0,
                (LandUse::PastureArable, _) => 0.5,
                (LandUse::Woodland | LandUse::Urban, _) => 1.0,
            }
        }
    }
}

/// `HR = d x (v + 0.5) + DF`, with depth in metres and velocity in m/s.
/// Returns 0 for a dry street.
#[must_use]
pub fn hazard_rating(depth_m: f64, velocity_mps: f64, rule: DebrisRule) -> f64 {
    if depth_m <= 0.0 {
        return 0.0;
    }
    let v = velocity_mps.max(0.0);
    depth_m * (v + VELOCITY_CONSTANT_N) + debris_factor(rule, depth_m, v)
}

/// Class for a hazard rating. Each boundary belongs to the class above it.
#[must_use]
pub fn classify(hr: f64) -> HazardClass {
    if hr >= HR_DANGER_ALL {
        HazardClass::DangerAll
    } else if hr >= HR_DANGER_MOST {
        HazardClass::DangerMost
    } else if hr >= HR_DANGER_SOME {
        HazardClass::DangerSome
    } else {
        HazardClass::Low
    }
}

/// A full hazard assessment for one depth and velocity.
#[derive(Clone, Copy, Debug, PartialEq, Serialize)]
pub struct Hazard {
    /// The hazard rating HR.
    pub rating: f64,
    /// The FD2320 class.
    pub class: HazardClass,
    /// The debris factor used.
    pub debris_factor: f64,
    /// The velocity used, in m/s.
    pub velocity_mps: f64,
    /// True when velocity was not observed and the conservative default was used.
    pub velocity_assumed: bool,
}

/// Assess hazard for a depth in centimetres. When `velocity_mps` is `None`
/// the conservative `default_velocity_mps` is used and flagged.
#[must_use]
pub fn assess(
    depth_cm: f64,
    velocity_mps: Option<f64>,
    default_velocity_mps: f64,
    rule: DebrisRule,
) -> Hazard {
    let (v, assumed) = match velocity_mps {
        Some(v) => (v.max(0.0), false),
        None => (default_velocity_mps, true),
    };
    let d = (depth_cm / 100.0).max(0.0);
    let rating = hazard_rating(d, v, rule);
    Hazard {
        rating,
        class: classify(rating),
        debris_factor: debris_factor(rule, d, v),
        velocity_mps: v,
        velocity_assumed: assumed,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const P: DebrisRule = DebrisRule::Fd2320Precautionary;

    fn close(a: f64, b: f64) -> bool {
        (a - b).abs() < 1e-9
    }

    #[test]
    fn matches_fd2320_table_4_cells() {
        // Values read from Table 4 of the 2008 supplementary note.
        assert!(close(hazard_rating(0.05, 0.0, P), 0.525)); // printed 0.53
        assert!(close(hazard_rating(0.25, 0.5, P), 0.75));
        assert!(close(hazard_rating(0.30, 0.0, P), 1.15));
        assert!(close(hazard_rating(0.50, 0.0, P), 1.25));
        assert!(close(hazard_rating(1.00, 0.5, P), 2.0));
        assert!(close(hazard_rating(0.60, 1.0, P), 1.90));
        assert!(close(hazard_rating(2.50, 0.0, P), 2.25));
    }

    #[test]
    fn class_boundaries_belong_to_the_higher_class() {
        assert_eq!(classify(0.0), HazardClass::Low);
        assert_eq!(classify(0.749), HazardClass::Low);
        assert_eq!(classify(0.75), HazardClass::DangerSome);
        assert_eq!(classify(1.249), HazardClass::DangerSome);
        assert_eq!(classify(1.25), HazardClass::DangerMost);
        assert_eq!(classify(1.999), HazardClass::DangerMost);
        assert_eq!(classify(2.0), HazardClass::DangerAll);
        assert_eq!(classify(9.0), HazardClass::DangerAll);
    }

    #[test]
    fn fd2321_debris_table() {
        let u = DebrisRule::Fd2321(LandUse::Urban);
        let w = DebrisRule::Fd2321(LandUse::Woodland);
        let p = DebrisRule::Fd2321(LandUse::PastureArable);
        assert_eq!(debris_factor(u, 0.2, 0.5), 0.0);
        assert_eq!(debris_factor(u, 0.5, 0.5), 1.0);
        assert_eq!(debris_factor(u, 0.9, 0.5), 1.0);
        assert_eq!(debris_factor(w, 0.5, 0.5), 0.5);
        assert_eq!(debris_factor(w, 0.9, 0.5), 1.0);
        assert_eq!(debris_factor(p, 0.5, 0.5), 0.0);
        assert_eq!(debris_factor(p, 0.9, 0.5), 0.5);
        // Fast water moves a shallow cell into the top band.
        assert_eq!(debris_factor(p, 0.1, 2.5), 0.5);
        assert_eq!(debris_factor(u, 0.1, 2.5), 1.0);
    }

    #[test]
    fn dry_street_is_zero() {
        assert_eq!(hazard_rating(0.0, 3.0, P), 0.0);
        assert_eq!(assess(0.0, None, 1.0, P).class, HazardClass::Low);
    }

    #[test]
    fn unknown_velocity_uses_default_and_flags_it() {
        let h = assess(30.0, None, 1.0, P);
        assert!(h.velocity_assumed);
        assert!(close(h.rating, 0.3 * 1.5 + 1.0));
        assert_eq!(h.class, HazardClass::DangerMost);
        let h = assess(30.0, Some(0.0), 1.0, P);
        assert!(!h.velocity_assumed);
        assert_eq!(h.class, HazardClass::DangerSome);
    }

    #[test]
    fn parse_names() {
        assert_eq!(
            "danger_most".parse::<HazardClass>().unwrap(),
            HazardClass::DangerMost
        );
        assert!("unknown".parse::<HazardClass>().is_err());
        assert_eq!(
            "fd2320".parse::<DebrisRule>().unwrap(),
            DebrisRule::Fd2320Precautionary
        );
        assert!("x".parse::<DebrisRule>().is_err());
    }
}
