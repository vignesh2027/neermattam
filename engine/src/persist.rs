//! Snapshots of the cell map, so a restart keeps it.
//!
//! The snapshot is a JSON document written atomically: the bytes go to a
//! temporary file in the same directory, are flushed to disk, and the file is
//! renamed over the old snapshot. A crash mid-write leaves the previous
//! snapshot intact. The server writes at most once per snapshot interval
//! (5 s by default) and only when something changed, plus once on shutdown.

use crate::fusion::{CellState, Grid};
use chrono::{DateTime, Utc};
use h3o::CellIndex;
use serde::{Deserialize, Serialize};
use std::fs;
use std::io::Write;
use std::path::{Path, PathBuf};

const FORMAT: &str = "neermattam-engine-cells";
/// Version 4 adds each cell's depth history. Version 3 (direct and
/// per-neighbour tracks, each with its measurement variance) is migrated on
/// load: its cells get a single history point marked `migrated` from the
/// latest direct level it kept. Older files are refused and moved aside at
/// start.
const VERSION: u32 = 4;
/// The oldest version [`load`] can migrate.
const OLDEST_MIGRATABLE: u32 = 3;

#[derive(Serialize, Deserialize)]
struct Snapshot {
    format: String,
    version: u32,
    resolution: u8,
    saved_at: DateTime<Utc>,
    cells: Vec<SnapshotCell>,
}

#[derive(Serialize, Deserialize)]
struct SnapshotCell {
    cell: String,
    #[serde(flatten)]
    state: CellState,
}

/// Errors reading or writing snapshots.
#[derive(Debug, thiserror::Error)]
pub enum PersistError {
    /// File system error.
    #[error("snapshot I/O on {path}: {source}")]
    Io {
        /// File.
        path: PathBuf,
        /// Cause.
        source: std::io::Error,
    },
    /// The file is not a valid snapshot.
    #[error("snapshot {path} is not valid: {reason}")]
    Invalid {
        /// File.
        path: PathBuf,
        /// What was wrong.
        reason: String,
    },
    /// The snapshot was written at another H3 resolution.
    #[error("snapshot {path} uses H3 resolution {found}, the engine is configured for {expected}")]
    Resolution {
        /// File.
        path: PathBuf,
        /// Resolution in the file.
        found: u8,
        /// Configured resolution.
        expected: u8,
    },
}

/// Serialise the grid's stored state.
pub fn encode(grid: &Grid, now: DateTime<Utc>) -> Vec<u8> {
    let mut cells: Vec<SnapshotCell> = grid
        .states()
        .map(|(c, s)| SnapshotCell {
            cell: c.to_string(),
            state: s.clone(),
        })
        .collect();
    cells.sort_unstable_by(|a, b| a.cell.cmp(&b.cell));
    let snap = Snapshot {
        format: FORMAT.into(),
        version: VERSION,
        resolution: u8::from(grid.resolution()),
        saved_at: now,
        cells,
    };
    serde_json::to_vec(&snap).expect("snapshot types always serialise")
}

/// Write `bytes` to `path` atomically.
pub fn write_atomic(path: &Path, bytes: &[u8]) -> Result<(), PersistError> {
    let io = |source| PersistError::Io {
        path: path.to_path_buf(),
        source,
    };
    let dir = path
        .parent()
        .filter(|p| !p.as_os_str().is_empty())
        .unwrap_or(Path::new("."));
    fs::create_dir_all(dir).map_err(io)?;
    let file_name = path
        .file_name()
        .map_or_else(|| "snapshot".into(), |n| n.to_string_lossy().into_owned());
    let tmp = dir.join(format!(".{file_name}.{}.tmp", std::process::id()));
    let result = (|| {
        let mut f = fs::File::create(&tmp)?;
        f.write_all(bytes)?;
        f.sync_all()?;
        drop(f);
        fs::rename(&tmp, path)?;
        // Make the rename itself durable where the platform allows it.
        if let Ok(d) = fs::File::open(dir) {
            let _ = d.sync_all();
        }
        Ok(())
    })();
    if result.is_err() {
        let _ = fs::remove_file(&tmp);
    }
    result.map_err(io)
}

/// Encode and atomically write the grid. Returns the number of cells saved.
pub fn save(grid: &Grid, path: &Path, now: DateTime<Utc>) -> Result<usize, PersistError> {
    let bytes = encode(grid, now);
    write_atomic(path, &bytes)?;
    Ok(grid.len())
}

/// Load a snapshot into `grid`. A missing file loads nothing and is not an
/// error. Returns the number of cells loaded.
pub fn load(path: &Path, grid: &mut Grid) -> Result<usize, PersistError> {
    let bytes = match fs::read(path) {
        Ok(b) => b,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(0),
        Err(source) => {
            return Err(PersistError::Io {
                path: path.to_path_buf(),
                source,
            })
        }
    };
    let invalid = |reason: String| PersistError::Invalid {
        path: path.to_path_buf(),
        reason,
    };
    let snap: Snapshot = serde_json::from_slice(&bytes).map_err(|e| invalid(e.to_string()))?;
    if snap.format != FORMAT || !(OLDEST_MIGRATABLE..=VERSION).contains(&snap.version) {
        return Err(invalid(format!(
            "unsupported format {} v{}",
            snap.format, snap.version
        )));
    }
    let expected = u8::from(grid.resolution());
    if snap.resolution != expected {
        return Err(PersistError::Resolution {
            path: path.to_path_buf(),
            found: snap.resolution,
            expected,
        });
    }
    let mut parsed = Vec::with_capacity(snap.cells.len());
    for c in snap.cells {
        let cell: CellIndex = c
            .cell
            .parse()
            .map_err(|e| invalid(format!("cell {}: {e}", c.cell)))?;
        if cell.resolution() != grid.resolution() {
            return Err(invalid(format!("cell {} has the wrong resolution", c.cell)));
        }
        if !c.state.is_valid() {
            return Err(invalid(format!(
                "cell {} holds no evidence or a number that is not finite",
                c.cell
            )));
        }
        let mut state = c.state;
        if snap.version < 4 {
            state.migrate_v3_history(grid.params());
        }
        parsed.push((cell, state));
    }
    let n = parsed.len();
    for (cell, state) in parsed {
        grid.insert_state(cell, state);
    }
    Ok(n)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::fusion::{FusionParams, Observation};
    use h3o::Resolution;

    fn sample() -> Grid {
        let mut g = Grid::new(FusionParams::default(), Resolution::Ten);
        g.ingest(&Observation {
            lat: 12.9791,
            lon: 80.2209,
            depth_cm: 23.4,
            depth_sigma_cm: 3.1,
            velocity_mps: Some(0.4),
            velocity_sigma_mps: Some(0.2),
            observed_at: Utc::now(),
            trust: 0.9,
        })
        .unwrap();
        g
    }

    #[test]
    fn round_trips_through_disk() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("nested/cells.json");
        let g = sample();
        assert_eq!(save(&g, &path, Utc::now()).unwrap(), 7);
        let mut back = Grid::new(FusionParams::default(), Resolution::Ten);
        assert_eq!(load(&path, &mut back).unwrap(), 7);
        let mut a: Vec<_> = g.states().map(|(c, s)| (*c, s.clone())).collect();
        let mut b: Vec<_> = back.states().map(|(c, s)| (*c, s.clone())).collect();
        a.sort_by_key(|x| u64::from(x.0));
        b.sort_by_key(|x| u64::from(x.0));
        assert_eq!(a, b);
        // No temporary files are left behind.
        let leftovers: Vec<_> = fs::read_dir(path.parent().unwrap())
            .unwrap()
            .filter_map(Result::ok)
            .filter(|e| e.file_name().to_string_lossy().ends_with(".tmp"))
            .collect();
        assert!(leftovers.is_empty());
    }

    #[test]
    fn a_vanishing_trust_cannot_poison_the_snapshot() {
        // Regression: trust 1e-310 made the variance infinite, which JSON
        // writes as null, so the next start refused the snapshot and lost
        // every cell.
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("cells.json");
        let mut g = sample();
        g.ingest(&Observation {
            lat: 13.05,
            lon: 80.25,
            depth_cm: 10.0,
            depth_sigma_cm: 3.0,
            velocity_mps: Some(0.5),
            velocity_sigma_mps: Some(0.2),
            observed_at: Utc::now(),
            trust: 1e-310,
        })
        .unwrap();
        save(&g, &path, Utc::now()).unwrap();
        let mut back = Grid::new(FusionParams::default(), Resolution::Ten);
        assert_eq!(load(&path, &mut back).unwrap(), 14);
        // A hand-edited file with a non-finite or negative variance is refused.
        let text = fs::read_to_string(&path).unwrap();
        let broken = text.replacen("\"var\":", "\"var\":-", 1);
        fs::write(&path, broken).unwrap();
        let mut g2 = Grid::new(FusionParams::default(), Resolution::Ten);
        assert!(matches!(
            load(&path, &mut g2),
            Err(PersistError::Invalid { .. })
        ));
        assert!(g2.is_empty());
    }

    #[test]
    fn version_3_snapshots_are_migrated() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("cells.json");
        let g = sample();
        save(&g, &path, Utc::now()).unwrap();
        // Rewrite as version 3: no history field anywhere.
        let mut v: serde_json::Value = serde_json::from_slice(&fs::read(&path).unwrap()).unwrap();
        v["version"] = 3.into();
        for c in v["cells"].as_array_mut().unwrap() {
            c.as_object_mut().unwrap().remove("history");
        }
        fs::write(&path, serde_json::to_vec(&v).unwrap()).unwrap();

        let mut back = Grid::new(FusionParams::default(), Resolution::Ten);
        assert_eq!(load(&path, &mut back).unwrap(), 7);
        let (cell, orig) = g.states().find(|(_, s)| s.n_readings == 1).unwrap();
        let migrated = back.state(*cell).unwrap();
        assert_eq!(migrated.history.len(), 1);
        let p = migrated.history[0];
        assert!(p.migrated);
        assert_eq!(Some(p.at), orig.last_direct_at);
        assert_eq!(Some(p.fused_cm), orig.last_depth_cm);
        // Apart from the history, the state is unchanged.
        let mut stripped = migrated.clone();
        stripped.history = orig.history.clone();
        assert_eq!(&stripped, orig);
        // Neighbour-only cells get no history.
        assert!(back
            .states()
            .filter(|(_, s)| s.n_readings == 0)
            .all(|(_, s)| s.history.is_empty()));
        // Version 4 round trip keeps the real history, and version 2 is refused.
        save(&back, &path, Utc::now()).unwrap();
        let text = fs::read_to_string(&path).unwrap();
        assert!(text.contains("\"version\":4") && text.contains("\"migrated\":true"));
        let v2 = text.replace("\"version\":4", "\"version\":2");
        fs::write(&path, v2).unwrap();
        let mut g2 = Grid::new(FusionParams::default(), Resolution::Ten);
        assert!(matches!(
            load(&path, &mut g2),
            Err(PersistError::Invalid { .. })
        ));
    }

    #[test]
    fn missing_file_is_empty_and_bad_files_are_errors() {
        let dir = tempfile::tempdir().unwrap();
        let mut g = Grid::new(FusionParams::default(), Resolution::Ten);
        assert_eq!(load(&dir.path().join("none.json"), &mut g).unwrap(), 0);
        let bad = dir.path().join("bad.json");
        fs::write(&bad, b"{not json").unwrap();
        assert!(matches!(
            load(&bad, &mut g),
            Err(PersistError::Invalid { .. })
        ));
        let other = dir.path().join("res9.json");
        save(&sample(), &other, Utc::now()).unwrap();
        let mut g9 = Grid::new(FusionParams::default(), Resolution::Nine);
        assert!(matches!(
            load(&other, &mut g9),
            Err(PersistError::Resolution { .. })
        ));
    }
}
