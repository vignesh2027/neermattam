//! Small geodesy helpers: great-circle distance and bounding boxes.

use serde::{Deserialize, Serialize};
use std::fmt;
use std::str::FromStr;

/// Mean Earth radius in metres (IUGG mean radius R1), the same value H3 uses.
pub const EARTH_RADIUS_M: f64 = 6_371_007.180_918_475;

/// Great-circle distance in metres between two WGS84 points, by the haversine
/// formula. Accurate to well under 0.5 percent at city scale, which is far
/// below the uncertainty of anything else the engine computes.
#[must_use]
pub fn haversine_m(lat1: f64, lon1: f64, lat2: f64, lon2: f64) -> f64 {
    let (p1, p2) = (lat1.to_radians(), lat2.to_radians());
    let dp = p2 - p1;
    let dl = (lon2 - lon1).to_radians();
    let a = (dp / 2.0).sin().powi(2) + p1.cos() * p2.cos() * (dl / 2.0).sin().powi(2);
    2.0 * EARTH_RADIUS_M * a.sqrt().min(1.0).asin()
}

/// A longitude and latitude box, written `minLon,minLat,maxLon,maxLat` as in
/// the GeoJSON `bbox` member.
#[derive(Clone, Copy, Debug, PartialEq, Serialize, Deserialize)]
pub struct BBox {
    /// Western edge, degrees.
    pub min_lon: f64,
    /// Southern edge, degrees.
    pub min_lat: f64,
    /// Eastern edge, degrees.
    pub max_lon: f64,
    /// Northern edge, degrees.
    pub max_lat: f64,
}

impl BBox {
    /// The default service area: Greater Chennai.
    pub const CHENNAI: BBox = BBox {
        min_lon: 80.10,
        min_lat: 12.85,
        max_lon: 80.33,
        max_lat: 13.24,
    };

    /// True when the point lies inside the box, edges included.
    #[must_use]
    pub fn contains(&self, lat: f64, lon: f64) -> bool {
        lat >= self.min_lat && lat <= self.max_lat && lon >= self.min_lon && lon <= self.max_lon
    }

    /// Area in square degrees, used only to cap the size of map queries.
    #[must_use]
    pub fn area_deg2(&self) -> f64 {
        (self.max_lon - self.min_lon) * (self.max_lat - self.min_lat)
    }

    /// True when the two boxes overlap.
    #[must_use]
    pub fn intersects(&self, other: &BBox) -> bool {
        self.min_lon <= other.max_lon
            && other.min_lon <= self.max_lon
            && self.min_lat <= other.max_lat
            && other.min_lat <= self.max_lat
    }
}

impl FromStr for BBox {
    type Err = String;

    fn from_str(s: &str) -> Result<Self, Self::Err> {
        let parts: Vec<&str> = s.split(',').map(str::trim).collect();
        if parts.len() != 4 {
            return Err("bbox must have four numbers: minLon,minLat,maxLon,maxLat".into());
        }
        let mut v = [0.0_f64; 4];
        for (slot, p) in v.iter_mut().zip(&parts) {
            *slot = p
                .parse::<f64>()
                .ok()
                .filter(|x| x.is_finite())
                .ok_or_else(|| format!("bbox value '{p}' is not a number"))?;
        }
        let b = BBox {
            min_lon: v[0],
            min_lat: v[1],
            max_lon: v[2],
            max_lat: v[3],
        };
        if !(-180.0..=180.0).contains(&b.min_lon) || !(-180.0..=180.0).contains(&b.max_lon) {
            return Err("bbox longitudes must be between -180 and 180".into());
        }
        if !(-90.0..=90.0).contains(&b.min_lat) || !(-90.0..=90.0).contains(&b.max_lat) {
            return Err("bbox latitudes must be between -90 and 90".into());
        }
        if b.min_lon >= b.max_lon || b.min_lat >= b.max_lat {
            return Err(
                "bbox minimum must be smaller than maximum (minLon,minLat,maxLon,maxLat)".into(),
            );
        }
        Ok(b)
    }
}

impl fmt::Display for BBox {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            f,
            "{},{},{},{}",
            self.min_lon, self.min_lat, self.max_lon, self.max_lat
        )
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn haversine_matches_known_distance() {
        // Velachery station to Guindy station, about 3.07 km apart.
        let d = haversine_m(12.9791, 80.2209, 13.0067, 80.2206);
        assert!((d - 3069.0).abs() < 15.0, "got {d}");
        assert_eq!(haversine_m(13.0, 80.0, 13.0, 80.0), 0.0);
    }

    #[test]
    fn bbox_parses_and_validates() {
        let b: BBox = "80.10,12.85,80.33,13.24".parse().unwrap();
        assert_eq!(b, BBox::CHENNAI);
        assert!(b.contains(13.0, 80.2));
        assert!(!b.contains(13.3, 80.2));
        assert!("1,2,3".parse::<BBox>().is_err());
        assert!("80.3,12.85,80.1,13.24".parse::<BBox>().is_err());
        assert!("a,b,c,d".parse::<BBox>().is_err());
        assert!("80,95,81,96".parse::<BBox>().is_err());
        assert_eq!(b.to_string().parse::<BBox>().unwrap(), b);
    }
}
