//! Snapshots of the cell map, so a restart keeps it.
//!
//! Two files, each a JSON document written atomically: the bytes go to a
//! temporary file in the same directory, are flushed to disk, and the file is
//! renamed over the old one, so a crash mid-write leaves the previous file
//! intact.
//!
//! - The **cell snapshot** (`cells.json`) holds every cell's state, which is
//!   what routing reads. It is written at most once per snapshot interval
//!   (5 s by default) when something changed.
//! - The **history file** (`cells.history.json`, next to it) holds each
//!   cell's depth history for the recession view. It can be far larger, so it
//!   is written on its own slower cadence (60 s by default).
//!
//! Both are written once more on shutdown. A crash therefore loses at most
//! one history interval of history and never more than one snapshot interval
//! of cell state; on the next start [`Grid::finish_load`] adds back the
//! latest reading of any cell whose history file missed it, marked
//! `migrated`, from what the cell state kept.

use crate::fusion::{CellState, Grid, HistoryPoint};
use chrono::{DateTime, Utc};
use h3o::CellIndex;
use serde::{Deserialize, Serialize};
use std::fs;
use std::io::Write;
use std::path::{Path, PathBuf};

const FORMAT: &str = "neermattam-engine-cells";
/// Version 5 moves the depth history out to the history file. Version 4 held
/// it inline and is read with it; version 3 had none and its cells get a
/// single history point marked `migrated` from the latest direct level it
/// kept. Older files are refused and moved aside at start.
const VERSION: u32 = 5;
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

const HISTORY_FORMAT: &str = "neermattam-engine-history";
/// The history file's own version, on the same scheme as the cell snapshot.
const HISTORY_VERSION: u32 = 1;

/// The history of every cell, copied out of the grid so it can be encoded
/// without holding the map's lock.
#[derive(Serialize, Deserialize)]
pub struct HistorySnapshot {
    format: String,
    version: u32,
    resolution: u8,
    saved_at: DateTime<Utc>,
    cells: Vec<HistoryCell>,
}

#[derive(Serialize, Deserialize)]
struct HistoryCell {
    cell: String,
    history: Vec<HistoryPoint>,
}

/// The history file that goes with a cell snapshot: `cells.json` gives
/// `cells.history.json` in the same directory.
#[must_use]
pub fn history_path(snapshot: &Path) -> PathBuf {
    let stem = snapshot
        .file_stem()
        .map_or_else(|| "cells".into(), |s| s.to_string_lossy().into_owned());
    snapshot.with_file_name(format!("{stem}.history.json"))
}

/// Copy every cell's history out of the grid. Cheap enough to do under the
/// read lock; encoding is done afterwards with [`HistorySnapshot::encode`].
#[must_use]
pub fn copy_history(grid: &Grid, now: DateTime<Utc>) -> HistorySnapshot {
    let mut cells: Vec<HistoryCell> = grid
        .states()
        .filter(|(_, s)| !s.history.is_empty())
        .map(|(c, s)| HistoryCell {
            cell: c.to_string(),
            history: s.history.iter().copied().collect(),
        })
        .collect();
    cells.sort_unstable_by(|a, b| a.cell.cmp(&b.cell));
    HistorySnapshot {
        format: HISTORY_FORMAT.into(),
        version: HISTORY_VERSION,
        resolution: u8::from(grid.resolution()),
        saved_at: now,
        cells,
    }
}

impl HistorySnapshot {
    /// Points held.
    #[must_use]
    pub fn points(&self) -> usize {
        self.cells.iter().map(|c| c.history.len()).sum()
    }

    /// Serialise to JSON.
    #[must_use]
    pub fn encode(&self) -> Vec<u8> {
        serde_json::to_vec(self).expect("history types always serialise")
    }
}

/// Encode and atomically write every cell's history. Returns the points saved.
pub fn save_history(grid: &Grid, path: &Path, now: DateTime<Utc>) -> Result<usize, PersistError> {
    let snap = copy_history(grid, now);
    write_atomic(path, &snap.encode())?;
    Ok(snap.points())
}

/// Load a history file into a grid whose cell states are already loaded. A
/// missing file loads nothing and is not an error. Cells the grid does not
/// hold (forgotten since) are skipped. The whole file is checked before any
/// of it is used, so a bad file changes nothing. Returns the points loaded.
pub fn load_history(path: &Path, grid: &mut Grid) -> Result<usize, PersistError> {
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
    let snap: HistorySnapshot =
        serde_json::from_slice(&bytes).map_err(|e| invalid(e.to_string()))?;
    if snap.format != HISTORY_FORMAT || snap.version != HISTORY_VERSION {
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
        if !c.history.iter().all(HistoryPoint::is_valid) {
            return Err(invalid(format!(
                "cell {} holds a history point that is not finite",
                c.cell
            )));
        }
        parsed.push((cell, c.history));
    }
    let mut points = 0;
    for (cell, history) in parsed {
        let n = history.len();
        if grid.set_history(cell, history.into()) {
            points += n;
        }
    }
    grid.finish_load();
    Ok(points)
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
        parsed.push((cell, c.state));
    }
    let n = parsed.len();
    for (cell, state) in parsed {
        grid.insert_state(cell, state);
    }
    // Order and bound each history (a hand-edited or longer one may be out of
    // order or too long; the drain fit relies on time order), recover the
    // latest reading where the history lacks it (version 3, or a history file
    // not loaded yet), and apply the global cap.
    grid.finish_load();
    Ok(n)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::fusion::{FusionParams, HistoryPoint, Observation};
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

    fn sorted(g: &Grid) -> Vec<(CellIndex, CellState)> {
        let mut a: Vec<_> = g.states().map(|(c, s)| (*c, s.clone())).collect();
        a.sort_by_key(|x| u64::from(x.0));
        a
    }

    fn reading_at(depth: f64, at: DateTime<Utc>) -> Observation {
        Observation {
            lat: 12.9791,
            lon: 80.2209,
            depth_cm: depth,
            depth_sigma_cm: 3.0,
            velocity_mps: None,
            velocity_sigma_mps: None,
            observed_at: at,
            trust: 1.0,
        }
    }

    #[test]
    fn round_trips_through_disk() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("nested/cells.json");
        let hpath = history_path(&path);
        assert_eq!(hpath, dir.path().join("nested/cells.history.json"));
        let mut g = sample();
        for i in 1..5 {
            g.ingest(&reading_at(
                23.0 - f64::from(i),
                Utc::now() + chrono::Duration::milliseconds(i64::from(i)),
            ))
            .unwrap();
        }
        assert_eq!(save(&g, &path, Utc::now()).unwrap(), 7);
        assert_eq!(save_history(&g, &hpath, Utc::now()).unwrap(), 5);
        // The cell snapshot carries no history; the history file does.
        let cells_text = fs::read_to_string(&path).unwrap();
        assert!(cells_text.contains("\"version\":5") && !cells_text.contains("history"));
        let mut back = Grid::new(FusionParams::default(), Resolution::Ten);
        assert_eq!(load(&path, &mut back).unwrap(), 7);
        assert_eq!(load_history(&hpath, &mut back).unwrap(), 5);
        assert_eq!(sorted(&g), sorted(&back));
        assert_eq!(back.history_points(), 5);
        // No temporary files are left behind.
        let leftovers: Vec<_> = fs::read_dir(path.parent().unwrap())
            .unwrap()
            .filter_map(Result::ok)
            .filter(|e| e.file_name().to_string_lossy().ends_with(".tmp"))
            .collect();
        assert!(leftovers.is_empty());
    }

    #[test]
    fn a_crash_loses_history_since_its_last_save_but_never_cell_state() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("cells.json");
        let hpath = history_path(&path);
        let start = Utc::now() - chrono::Duration::hours(1);
        let mut g = Grid::new(FusionParams::default(), Resolution::Ten);
        let mut cell = None;
        for i in 0..4 {
            cell = Some(
                g.ingest(&reading_at(
                    40.0 - 2.0 * f64::from(i),
                    start + chrono::Duration::minutes(10 * i64::from(i)),
                ))
                .unwrap()
                .cell,
            );
        }
        let cell = cell.unwrap();
        // History saved at minute 30, and cells at the same time...
        save_history(&g, &hpath, Utc::now()).unwrap();
        save(&g, &path, Utc::now()).unwrap();
        // ...then two more readings, a cell save on its 5 s cadence, and a
        // crash before the 60 s history save.
        for i in 4..6 {
            g.ingest(&reading_at(
                40.0 - 2.0 * f64::from(i),
                start + chrono::Duration::minutes(10 * i64::from(i)),
            ))
            .unwrap();
        }
        save(&g, &path, Utc::now()).unwrap();

        let mut back = Grid::new(FusionParams::default(), Resolution::Ten);
        load(&path, &mut back).unwrap();
        load_history(&hpath, &mut back).unwrap();
        // Cell state is exactly as before the crash.
        let strip = |v: Vec<(CellIndex, CellState)>| {
            v.into_iter()
                .map(|(c, mut s)| {
                    s.history.clear();
                    (c, s)
                })
                .collect::<Vec<_>>()
        };
        assert_eq!(strip(sorted(&g)), strip(sorted(&back)));
        // History: the four saved readings, then the latest level recovered
        // from the cell state and marked migrated. The fifth reading is lost.
        let h = &back.state(cell).unwrap().history;
        let readings: Vec<(f64, bool)> = h.iter().map(|p| (p.reading_cm, p.migrated)).collect();
        assert_eq!(readings.len(), 5, "{readings:?}");
        assert_eq!(
            &readings[..4],
            &[(40.0, false), (38.0, false), (36.0, false), (34.0, false)]
        );
        let last = h.back().unwrap();
        assert!(last.migrated);
        assert_eq!(Some(last.at), g.state(cell).unwrap().last_direct_at);
        assert_eq!(back.history_points(), 5);
        // A missing history file (a crash before its first save) still leaves
        // each cell its latest level.
        fs::remove_file(&hpath).unwrap();
        let mut fresh = Grid::new(FusionParams::default(), Resolution::Ten);
        load(&path, &mut fresh).unwrap();
        assert_eq!(load_history(&hpath, &mut fresh).unwrap(), 0);
        assert_eq!(fresh.state(cell).unwrap().history.len(), 1);
        assert!(fresh.state(cell).unwrap().history[0].migrated);
    }

    #[test]
    fn a_bad_history_file_never_costs_the_cell_state() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("cells.json");
        let hpath = history_path(&path);
        let g = sample();
        save(&g, &path, Utc::now()).unwrap();
        save_history(&g, &hpath, Utc::now()).unwrap();
        let good = fs::read_to_string(&hpath).unwrap();
        for (name, text) in [
            ("cut in half", good[..good.len() / 2].to_string()),
            (
                "negative sigma",
                good.replacen("\"reading_sigma_cm\":", "\"reading_sigma_cm\":-", 1),
            ),
            (
                "wrong format",
                good.replace("neermattam-engine-history", "something-else"),
            ),
            (
                "future version",
                good.replace("\"version\":1", "\"version\":9"),
            ),
        ] {
            fs::write(&hpath, &text).unwrap();
            let mut back = Grid::new(FusionParams::default(), Resolution::Ten);
            load(&path, &mut back).unwrap();
            let before = sorted(&back);
            assert!(
                matches!(
                    load_history(&hpath, &mut back),
                    Err(PersistError::Invalid { .. })
                ),
                "{name}"
            );
            assert_eq!(sorted(&back), before, "{name} changed the map");
            assert_eq!(back.len(), 7);
        }
        // A history for a cell the map has since forgotten is skipped.
        fs::write(&hpath, &good).unwrap();
        let mut empty = Grid::new(FusionParams::default(), Resolution::Ten);
        assert_eq!(load_history(&hpath, &mut empty).unwrap(), 0);
        assert!(empty.is_empty());
    }

    #[test]
    fn version_4_inline_history_is_still_read() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("cells.json");
        let mut g = sample();
        g.ingest(&reading_at(20.0, Utc::now() + chrono::Duration::seconds(1)))
            .unwrap();
        save(&g, &path, Utc::now()).unwrap();
        // Rewrite as version 4, with the history inline as that version kept it.
        let mut v: serde_json::Value = serde_json::from_slice(&fs::read(&path).unwrap()).unwrap();
        v["version"] = 4.into();
        for c in v["cells"].as_array_mut().unwrap() {
            let cell: CellIndex = c["cell"].as_str().unwrap().parse().unwrap();
            let h: Vec<HistoryPoint> = g.state(cell).unwrap().history.iter().copied().collect();
            c["history"] = serde_json::to_value(h).unwrap();
        }
        fs::write(&path, serde_json::to_vec(&v).unwrap()).unwrap();
        let mut back = Grid::new(FusionParams::default(), Resolution::Ten);
        assert_eq!(load(&path, &mut back).unwrap(), 7);
        assert_eq!(sorted(&g), sorted(&back));
        assert_eq!(back.history_points(), 2);
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
        // The migrated point goes to the history file, and version 2 is refused.
        save(&back, &path, Utc::now()).unwrap();
        let hpath = history_path(&path);
        save_history(&back, &hpath, Utc::now()).unwrap();
        assert!(fs::read_to_string(&hpath)
            .unwrap()
            .contains("\"migrated\":true"));
        let text = fs::read_to_string(&path).unwrap();
        let v2 = text.replace("\"version\":5", "\"version\":2");
        fs::write(&path, v2).unwrap();
        let mut g2 = Grid::new(FusionParams::default(), Resolution::Ten);
        assert!(matches!(
            load(&path, &mut g2),
            Err(PersistError::Invalid { .. })
        ));
    }

    #[test]
    fn corrupt_and_partial_snapshots_are_refused_whole() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("cells.json");
        let g = sample();
        save(&g, &path, Utc::now()).unwrap();
        let good = fs::read(&path).unwrap();
        let mut v3: serde_json::Value = serde_json::from_slice(&good).unwrap();
        v3["version"] = 3.into();
        for c in v3["cells"].as_array_mut().unwrap() {
            c.as_object_mut().unwrap().remove("history");
        }
        let v3 = serde_json::to_vec(&v3).unwrap();
        for (name, bytes) in [
            ("v5 cut in half", good[..good.len() / 2].to_vec()),
            ("v3 cut in half", v3[..v3.len() / 2].to_vec()),
            ("v3 missing its last byte", v3[..v3.len() - 1].to_vec()),
            ("empty", Vec::new()),
        ] {
            fs::write(&path, &bytes).unwrap();
            let mut back = Grid::new(FusionParams::default(), Resolution::Ten);
            assert!(
                matches!(load(&path, &mut back), Err(PersistError::Invalid { .. })),
                "{name}"
            );
            assert!(back.is_empty(), "{name} loaded a partial map");
        }
        // The intact v3 file migrates, and a v5 round trip is exact.
        fs::write(&path, &v3).unwrap();
        let mut back = Grid::new(FusionParams::default(), Resolution::Ten);
        assert_eq!(load(&path, &mut back).unwrap(), 7);
        save(&back, &path, Utc::now()).unwrap();
        let mut again = Grid::new(FusionParams::default(), Resolution::Ten);
        assert_eq!(load(&path, &mut again).unwrap(), 7);
        let mut a: Vec<_> = back.states().map(|(c, s)| (*c, s.clone())).collect();
        let mut b: Vec<_> = again.states().map(|(c, s)| (*c, s.clone())).collect();
        a.sort_by_key(|x| u64::from(x.0));
        b.sort_by_key(|x| u64::from(x.0));
        assert_eq!(a, b);
    }

    #[test]
    fn loaded_history_is_ordered_and_bounded() {
        // Regression: a snapshot with more history than the engine keeps, or
        // with points out of order, was loaded as is.
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("cells.json");
        let mut g = Grid::new(FusionParams::default(), Resolution::Ten);
        let start = Utc::now() - chrono::Duration::hours(3);
        let mut cell = None;
        for i in 0..10 {
            cell = Some(
                g.ingest(&Observation {
                    lat: 12.9791,
                    lon: 80.2209,
                    depth_cm: 40.0 - f64::from(i),
                    depth_sigma_cm: 3.0,
                    velocity_mps: None,
                    velocity_sigma_mps: None,
                    observed_at: start + chrono::Duration::minutes(10 * i64::from(i)),
                    trust: 1.0,
                })
                .unwrap()
                .cell,
            );
        }
        save(&g, &path, Utc::now()).unwrap();
        let hpath = history_path(&path);
        save_history(&g, &hpath, Utc::now()).unwrap();
        // Shuffle the history in the file.
        let mut v: serde_json::Value = serde_json::from_slice(&fs::read(&hpath).unwrap()).unwrap();
        for c in v["cells"].as_array_mut().unwrap() {
            if let Some(h) = c["history"].as_array_mut() {
                h.reverse();
            }
        }
        fs::write(&hpath, serde_json::to_vec(&v).unwrap()).unwrap();
        let short = FusionParams {
            history_len: 4,
            ..FusionParams::default()
        };
        let mut back = Grid::new(short, Resolution::Ten);
        load(&path, &mut back).unwrap();
        load_history(&hpath, &mut back).unwrap();
        let h = &back.state(cell.unwrap()).unwrap().history;
        let depths: Vec<f64> = h.iter().map(|p| p.reading_cm).collect();
        assert_eq!(depths, [34.0, 33.0, 32.0, 31.0]);
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
