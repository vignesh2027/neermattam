//! The cities the engine covers.
//!
//! Each city has an id, a display name, a metro bounding box and a road
//! extract. Readings are accepted anywhere inside any city's box, and a route
//! is planned on the graph of the one city whose box holds both of its ends.
//! The H3 grid itself is global, so a cell needs no city to exist; a city is
//! only a way to scope queries and to choose a road graph.
//!
//! # Default metro boxes
//!
//! Each box covers the continuously built-up metro area that floods together,
//! not the administrative district. They were drawn by hand from OpenStreetMap
//! and are listed in `docs/ENGINE.md` with what each one includes.
//!
//! | id | Box (minLon,minLat,maxLon,maxLat) | Covers |
//! |---|---|---|
//! | chennai | 80.10,12.85,80.33,13.24 | Greater Chennai Corporation, Tambaram to Ennore |
//! | mumbai | 72.77,18.88,73.13,19.32 | Island city, western and eastern suburbs, Thane, Navi Mumbai to Belapur |
//! | bengaluru | 77.44,12.82,77.80,13.15 | BBMP inside the outer ring, Electronic City to Yelahanka |
//! | hyderabad | 78.24,17.22,78.66,17.62 | GHMC and Secunderabad, inside the outer ring road |
//! | kolkata | 88.20,22.42,88.52,22.72 | Kolkata Municipal Corporation, Howrah, Salt Lake and New Town |
//! | delhi | 76.84,28.40,77.35,28.89 | National Capital Territory of Delhi |

use crate::geo::BBox;
use serde::Serialize;
use std::path::{Path, PathBuf};

/// One covered city.
#[derive(Clone, Debug, PartialEq, Serialize)]
pub struct City {
    /// Short lower-case id used in query strings, such as `chennai`.
    pub id: String,
    /// Display name, such as `Chennai`.
    pub name: String,
    /// Metro bounding box.
    pub bbox: BBox,
    /// OSM PBF extract of the city's highway ways.
    #[serde(skip)]
    pub osm_path: PathBuf,
}

impl City {
    /// Centre of the box as `(lat, lon)`.
    #[must_use]
    pub fn centre(&self) -> (f64, f64) {
        (
            (self.bbox.min_lat + self.bbox.max_lat) / 2.0,
            (self.bbox.min_lon + self.bbox.max_lon) / 2.0,
        )
    }

    /// True when the point lies in the city's box.
    #[must_use]
    pub fn contains(&self, lat: f64, lon: f64) -> bool {
        self.bbox.contains(lat, lon)
    }
}

/// The built-in cities: id, display name, metro box.
pub const DEFAULT_CITIES: [(&str, &str, BBox); 6] = [
    ("chennai", "Chennai", BBox::CHENNAI),
    (
        "mumbai",
        "Mumbai",
        BBox {
            min_lon: 72.77,
            min_lat: 18.88,
            max_lon: 73.13,
            max_lat: 19.32,
        },
    ),
    (
        "bengaluru",
        "Bengaluru",
        BBox {
            min_lon: 77.44,
            min_lat: 12.82,
            max_lon: 77.80,
            max_lat: 13.15,
        },
    ),
    (
        "hyderabad",
        "Hyderabad",
        BBox {
            min_lon: 78.24,
            min_lat: 17.22,
            max_lon: 78.66,
            max_lat: 17.62,
        },
    ),
    (
        "kolkata",
        "Kolkata",
        BBox {
            min_lon: 88.20,
            min_lat: 22.42,
            max_lon: 88.52,
            max_lat: 22.72,
        },
    ),
    (
        "delhi",
        "Delhi",
        BBox {
            min_lon: 76.84,
            min_lat: 28.40,
            max_lon: 77.35,
            max_lat: 28.89,
        },
    ),
];

/// The built-in cities, with extracts at `<osm_dir>/<id>-roads.osm.pbf`.
#[must_use]
pub fn default_cities(osm_dir: &Path) -> Vec<City> {
    DEFAULT_CITIES
        .iter()
        .map(|(id, name, bbox)| City {
            id: (*id).to_string(),
            name: (*name).to_string(),
            bbox: *bbox,
            osm_path: osm_dir.join(format!("{id}-roads.osm.pbf")),
        })
        .collect()
}

/// True when `id` is a usable city id: 1 to 32 characters of `a-z`, `0-9`
/// and `_`, starting with a letter.
#[must_use]
pub fn valid_id(id: &str) -> bool {
    !id.is_empty()
        && id.len() <= 32
        && id.starts_with(|c: char| c.is_ascii_lowercase())
        && id
            .chars()
            .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '_')
}

/// Look up cities by position or id.
pub trait CityLookup {
    /// The first city whose box holds the point.
    fn city_at(&self, lat: f64, lon: f64) -> Option<&City>;
    /// The city with this id.
    fn city(&self, id: &str) -> Option<&City>;
    /// Ids joined for error messages, such as `chennai, mumbai`.
    fn ids(&self) -> String;
}

impl CityLookup for [City] {
    fn city_at(&self, lat: f64, lon: f64) -> Option<&City> {
        self.iter().find(|c| c.contains(lat, lon))
    }

    fn city(&self, id: &str) -> Option<&City> {
        self.iter().find(|c| c.id == id)
    }

    fn ids(&self) -> String {
        self.iter()
            .map(|c| c.id.as_str())
            .collect::<Vec<_>>()
            .join(", ")
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn defaults_are_distinct_and_do_not_overlap() {
        let cities = default_cities(Path::new("/d"));
        assert_eq!(cities.len(), 6);
        for (i, a) in cities.iter().enumerate() {
            assert!(valid_id(&a.id));
            assert_eq!(
                a.osm_path,
                PathBuf::from(format!("/d/{}-roads.osm.pbf", a.id))
            );
            // Every box fits in one /v1/cells request.
            assert!(a.bbox.area_deg2() <= 1.0, "{}", a.id);
            let (lat, lon) = a.centre();
            assert_eq!(
                cities.city_at(lat, lon).map(|c| c.id.as_str()),
                Some(a.id.as_str())
            );
            for b in &cities[i + 1..] {
                assert!(!a.bbox.intersects(&b.bbox), "{} overlaps {}", a.id, b.id);
            }
        }
    }

    #[test]
    fn landmarks_fall_in_their_city() {
        let cities = default_cities(Path::new("."));
        for (lat, lon, id) in [
            (12.9791, 80.2209, "chennai"),   // Velachery station
            (18.9398, 72.8355, "mumbai"),    // Chhatrapati Shivaji Terminus
            (19.1860, 72.9750, "mumbai"),    // Thane station
            (12.9767, 77.5713, "bengaluru"), // Majestic
            (17.4344, 78.5013, "hyderabad"), // Secunderabad station
            (22.5839, 88.3426, "kolkata"),   // Howrah station
            (28.6431, 77.2197, "delhi"),     // New Delhi station
        ] {
            assert_eq!(cities.city_at(lat, lon).map(|c| c.id.as_str()), Some(id));
        }
        assert!(cities.city_at(15.0, 75.0).is_none());
        assert_eq!(cities.city("kolkata").unwrap().name, "Kolkata");
        assert!(cities.city("pune").is_none());
        assert!(cities.ids().starts_with("chennai, mumbai"));
    }

    #[test]
    fn ids_are_checked() {
        assert!(valid_id("pune"));
        assert!(valid_id("navi_mumbai2"));
        assert!(!valid_id(""));
        assert!(!valid_id("Pune"));
        assert!(!valid_id("2pune"));
        assert!(!valid_id("pune-city"));
        assert!(!valid_id(&"a".repeat(33)));
    }
}
