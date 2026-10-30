//! Vehicles, their advisory crossing limits and their urban speeds.
//!
//! A vehicle may cross a cell only when the *upper* estimate of depth,
//! `mean + k x sigma` with `k = 1.5` by default, is at or below its depth
//! limit, and the upper depth times the upper velocity is at or below its
//! depth-velocity limit. Pedestrians use the Defra hazard class instead (see
//! [`crate::hazard`]). All limits are advisory: a photo-based depth estimate is
//! never a guarantee, and no one should enter floodwater on the strength of
//! this engine alone.
//!
//! # Sources
//!
//! - US National Weather Service, *Turn Around Don't Drown*
//!   (<https://www.weather.gov/tsa/hydro_tadd>, checked 2026-10-26): "Six
//!   inches of water will reach the bottom of most passenger cars causing loss
//!   of control and possible stalling. A foot of water will float many
//!   vehicles. Two feet of rushing water can carry away most vehicles including
//!   sport utility vehicles (SUV's) and pick-ups."
//! - Smith G. P., Modra B. D., Tucker T. A., Cox R. J., *Vehicle Stability
//!   Testing for Flood Flows*, WRL Technical Report 2017/07, UNSW Water
//!   Research Laboratory, May 2017, Table ES-1, which confirms the interim
//!   Australian Rainfall and Runoff Project 10 thresholds (after Shand et al.,
//!   2011): small passenger vehicles, limiting still-water depth 0.3 m and
//!   D x V at most 0.3 m2/s; large passenger vehicles 0.4 m and 0.45 m2/s;
//!   large 4WD 0.5 m and 0.6 m2/s. These are stability limits (the point a
//!   vehicle starts to slide or float), not safe driving limits, which is why
//!   the depth limits below are lower.
//!
//! # Values and how much of each is sourced
//!
//! | Vehicle     | Depth limit | D x V limit | Basis |
//! |-------------|-------------|-------------|-------|
//! | car         | 15 cm       | 0.30 m2/s   | NWS six inches; ARR small passenger D x V |
//! | auto        | 15 cm       | 0.30 m2/s   | No auto-rickshaw source found. Treated like a small car: lighter than 1250 kg with low clearance. Unverified. |
//! | two_wheeler | 15 cm       | 0.30 m2/s   | No two-wheeler source found. Riders and exhausts on Indian commuter bikes sit low, so the car limit is used as a floor. Unverified. |
//! | bus         | 30 cm       | 0.60 m2/s   | No bus source found. Kept at the NWS "a foot of water will float many vehicles" figure, with the ARR large 4WD D x V. Unverified. |
//! | truck       | 30 cm       | 0.60 m2/s   | AIDR Guideline 7-3, Tables 1 and 2: above 0.5 m still water, or D x V above 0.6 m2/s, floodwater is "unsafe for vehicles" of every size (class H3). The D x V limit is that figure; the depth limit keeps the NWS one-foot figure, 20 cm under the ceiling. |
//!
//! - Australian Institute for Disaster Resilience, *Australian Disaster
//!   Resilience Guideline 7-3: Technical flood risk management guideline:
//!   Flood hazard*, 2014 (CC BY-NC), Table 1 (H2 "unsafe for small vehicles",
//!   H3 "unsafe for vehicles, children and the elderly") and Table 2 (H2: D x V
//!   at most 0.6 m2/s and still water at most 0.5 m; H3 above that), modelled
//!   after Smith and Wasko (2012) and, for vehicles, Shand et al. (2011).
//!   <https://knowledge.aidr.org.au/media/1891/guideline-7-3-technical-flood-risk-management.pdf>,
//!   checked 2026-10-30. The guideline has no separate class for heavy goods
//!   vehicles; its H3 boundary is where every vehicle is unsafe, so it is the
//!   ceiling for a tanker, not a crossing depth.
//!
//! No Indian government or Indian Roads Congress figure for safe crossing
//! depth was found during the build. If one is found it should replace the
//! unverified rows above.
//!
//! Speeds are rough urban averages used only for `eta_min`. They are
//! engineering assumptions, not measured Chennai values.

use serde::{Deserialize, Serialize};
use std::fmt;
use std::str::FromStr;

/// A kind of road user.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Vehicle {
    /// On foot.
    Pedestrian,
    /// Motorcycle or scooter.
    TwoWheeler,
    /// Auto-rickshaw (three-wheeler).
    Auto,
    /// Passenger car.
    Car,
    /// City bus.
    Bus,
    /// Diesel tanker or other heavy goods vehicle, for access to critical
    /// sites.
    Truck,
}

/// What limits a vehicle's crossing of water.
#[derive(Clone, Copy, Debug, PartialEq)]
pub enum CrossingLimit {
    /// A depth limit in centimetres and a depth-times-velocity limit in m2/s.
    Depth {
        /// Maximum upper-bound depth, centimetres.
        max_depth_cm: f64,
        /// Maximum upper depth (m) times upper velocity (m/s).
        max_dv_m2s: f64,
    },
    /// Use the Defra hazard class (pedestrians).
    HazardClass,
}

/// A vehicle's routing profile.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Profile {
    /// Crossing rule.
    pub limit: CrossingLimit,
    /// Average urban speed in km/h, for `eta_min`.
    pub speed_kmh: f64,
}

impl Vehicle {
    /// Every vehicle, in bit order.
    pub const ALL: [Vehicle; 6] = [
        Vehicle::Pedestrian,
        Vehicle::TwoWheeler,
        Vehicle::Auto,
        Vehicle::Car,
        Vehicle::Bus,
        Vehicle::Truck,
    ];

    /// The access bit for this vehicle in [`crate::graph`] masks.
    #[must_use]
    pub const fn bit(self) -> u8 {
        match self {
            Vehicle::Pedestrian => 1,
            Vehicle::TwoWheeler => 2,
            Vehicle::Auto => 4,
            Vehicle::Car => 8,
            Vehicle::Bus => 16,
            Vehicle::Truck => 32,
        }
    }

    /// The wire name.
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Vehicle::Pedestrian => "pedestrian",
            Vehicle::TwoWheeler => "two_wheeler",
            Vehicle::Auto => "auto",
            Vehicle::Car => "car",
            Vehicle::Bus => "bus",
            Vehicle::Truck => "truck",
        }
    }

    /// A plain-English noun for explanations.
    #[must_use]
    pub const fn noun(self) -> &'static str {
        match self {
            Vehicle::Pedestrian => "someone on foot",
            Vehicle::TwoWheeler => "a two-wheeler",
            Vehicle::Auto => "an auto-rickshaw",
            Vehicle::Car => "a car",
            Vehicle::Bus => "a bus",
            Vehicle::Truck => "a diesel tanker or truck",
        }
    }

    /// The vehicle's crossing limits and speed. See the module docs for sources.
    #[must_use]
    pub const fn profile(self) -> Profile {
        match self {
            Vehicle::Pedestrian => Profile {
                limit: CrossingLimit::HazardClass,
                speed_kmh: 4.0,
            },
            Vehicle::TwoWheeler => Profile {
                limit: CrossingLimit::Depth {
                    max_depth_cm: 15.0,
                    max_dv_m2s: 0.30,
                },
                speed_kmh: 20.0,
            },
            Vehicle::Auto => Profile {
                limit: CrossingLimit::Depth {
                    max_depth_cm: 15.0,
                    max_dv_m2s: 0.30,
                },
                speed_kmh: 18.0,
            },
            Vehicle::Car => Profile {
                limit: CrossingLimit::Depth {
                    max_depth_cm: 15.0,
                    max_dv_m2s: 0.30,
                },
                speed_kmh: 20.0,
            },
            Vehicle::Bus => Profile {
                limit: CrossingLimit::Depth {
                    max_depth_cm: 30.0,
                    max_dv_m2s: 0.60,
                },
                speed_kmh: 15.0,
            },
            // AIDR 7-3 Table 2: every vehicle is unsafe above 0.5 m or
            // D x V 0.6 m2/s. The depth limit stays 20 cm under that ceiling.
            Vehicle::Truck => Profile {
                limit: CrossingLimit::Depth {
                    max_depth_cm: 30.0,
                    max_dv_m2s: 0.60,
                },
                speed_kmh: 15.0,
            },
        }
    }
}

impl fmt::Display for Vehicle {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

impl FromStr for Vehicle {
    type Err = String;
    fn from_str(s: &str) -> Result<Self, Self::Err> {
        Vehicle::ALL
            .into_iter()
            .find(|v| v.as_str() == s)
            .ok_or_else(|| {
                format!("unknown vehicle '{s}', expected pedestrian, two_wheeler, auto, car, bus or truck")
            })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn names_round_trip_and_bits_are_distinct() {
        let mut seen = 0u8;
        for v in Vehicle::ALL {
            assert_eq!(v.as_str().parse::<Vehicle>().unwrap(), v);
            assert_eq!(seen & v.bit(), 0);
            seen |= v.bit();
        }
        assert!("tractor".parse::<Vehicle>().is_err());
    }

    #[test]
    fn bus_tolerates_more_water_than_car() {
        let depth = |v: Vehicle| match v.profile().limit {
            CrossingLimit::Depth { max_depth_cm, .. } => max_depth_cm,
            CrossingLimit::HazardClass => f64::NAN,
        };
        assert!(depth(Vehicle::Bus) > depth(Vehicle::Car));
        // The tanker limit stays under the AIDR ceiling where every vehicle
        // is unsafe (0.5 m, D x V 0.6 m2/s) and above the car's.
        match Vehicle::Truck.profile().limit {
            CrossingLimit::Depth {
                max_depth_cm,
                max_dv_m2s,
            } => {
                assert!(max_depth_cm < 50.0 && max_depth_cm > depth(Vehicle::Car));
                assert!(max_dv_m2s <= 0.6);
            }
            CrossingLimit::HazardClass => panic!("a truck has a depth limit"),
        }
        assert_eq!(
            Vehicle::Pedestrian.profile().limit,
            CrossingLimit::HazardClass
        );
    }
}
