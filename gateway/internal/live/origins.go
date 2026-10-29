// Package live fans the engine's cell stream out to browsers and keeps
// simulated and real evidence apart. The engine does not know which readings
// were simulated, so the gateway labels every cell it serves:
//
//   - "reported": real reports touched it
//   - "simulated": only the demo scenario touched it
//   - "mixed": both did
//
// Provenance is recorded explicitly, never inferred from what happens to be
// in the engine: simulated readings have ids starting "sim_" and mark the
// cells around them as simulated; real reports mark theirs as real and are
// kept as records. Both sets are saved to a small JSON file, so a restart
// keeps the labels and the engine's snapshot can be kept too.
//
// The engine streams a reading's seven changed cells before the gateway has
// seen its response, so the gateway also registers a short-lived zone
// around each reading it is about to post; a streamed cell whose centre
// falls inside a zone is labelled from it.
package live

import (
	"encoding/json"
	"errors"
	"fmt"
	"io/fs"
	"math"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"time"
)

// Origin labels.
const (
	OriginReported  = "reported"
	OriginSimulated = "simulated"
	OriginMixed     = "mixed"
)

// SimulatedPrefix starts the id of every simulated reading.
const SimulatedPrefix = "sim_"

// IsSimulatedID reports whether a reading id belongs to the demo scenario.
func IsSimulatedID(id string) bool { return strings.HasPrefix(id, SimulatedPrefix) }

// ZoneRadiusM covers a reading's own H3 resolution 10 cell and its ring of
// six neighbours (centre spacing about 115 m) with margin.
const ZoneRadiusM = 160.0

// Forget is how long provenance is kept: the engine drops cells with no
// evidence for 48 hours, so older labels describe nothing.
const Forget = 48 * time.Hour

// MaxRecords bounds the real report records kept.
const MaxRecords = 2000

type zone struct {
	lat, lon  float64
	simulated bool
	until     time.Time
}

// RealReading is the record of one real report the engine fused.
type RealReading struct {
	ReadingID    string    `json:"reading_id"`
	Cell         string    `json:"cell"`
	Lat          float64   `json:"lat"`
	Lon          float64   `json:"lon"`
	DepthCm      float64   `json:"depth_cm"`
	DepthSigmaCm float64   `json:"depth_sigma_cm"`
	Source       string    `json:"source"`
	Trust        float64   `json:"trust"`
	Place        string    `json:"place"`
	ObservedAt   time.Time `json:"observed_at"`
	ReceivedAt   time.Time `json:"received_at"`
}

// Origins tracks which cells hold simulated and real evidence.
type Origins struct {
	// Demo makes a cell with no record at all read as simulated: in a demo,
	// a label is never allowed to pass simulated water off as real.
	Demo bool
	// DemoArea, when set, limits that default to the scenario's own area:
	// a cell elsewhere with no record reads as reported, never simulated.
	DemoArea func(lat, lon float64) bool

	mu      sync.RWMutex
	sim     map[string]time.Time
	real    map[string]time.Time
	records []RealReading
	zones   []zone
	dirty   bool
	now     func() time.Time
}

// NewOrigins returns an empty tracker.
func NewOrigins() *Origins {
	return &Origins{sim: map[string]time.Time{}, real: map[string]time.Time{}, now: time.Now}
}

// WithClock replaces the clock, for tests.
func (o *Origins) WithClock(now func() time.Time) *Origins {
	o.now = now
	return o
}

// Mark records that a cell holds simulated or real evidence.
func (o *Origins) Mark(cell string, simulated bool) {
	if cell == "" {
		return
	}
	now := o.now()
	o.mu.Lock()
	defer o.mu.Unlock()
	o.markLocked(cell, simulated, now)
}

func (o *Origins) markLocked(cell string, simulated bool, now time.Time) {
	if simulated {
		o.sim[cell] = now
	} else {
		o.real[cell] = now
	}
	o.dirty = true
}

// Record keeps a real report and marks its cell as real.
func (o *Origins) Record(r RealReading) {
	if IsSimulatedID(r.ReadingID) {
		return
	}
	now := o.now()
	o.mu.Lock()
	defer o.mu.Unlock()
	o.records = append(o.records, r)
	if len(o.records) > MaxRecords {
		o.records = o.records[len(o.records)-MaxRecords:]
	}
	o.markLocked(r.Cell, false, now)
}

// Records returns the kept real reports, newest first.
func (o *Origins) Records() []RealReading {
	o.mu.RLock()
	defer o.mu.RUnlock()
	out := make([]RealReading, len(o.records))
	for i, r := range o.records {
		out[len(out)-1-i] = r
	}
	return out
}

// Expect registers a zone around a reading that is about to be posted.
func (o *Origins) Expect(lat, lon float64, simulated bool) {
	now := o.now()
	o.mu.Lock()
	defer o.mu.Unlock()
	kept := o.zones[:0]
	for _, z := range o.zones {
		if z.until.After(now) {
			kept = append(kept, z)
		}
	}
	o.zones = append(kept, zone{lat: lat, lon: lon, simulated: simulated, until: now.Add(15 * time.Second)})
}

// MarkAround labels every cell centre within ZoneRadiusM of any of the
// points. It is how the scenario labels the cells its readings touched,
// after a seed, from one query of the whole map.
func (o *Origins) MarkAround(cells []CellCentre, points [][2]float64, simulated bool) int {
	now := o.now()
	o.mu.Lock()
	defer o.mu.Unlock()
	n := 0
	for _, c := range cells {
		for _, p := range points {
			// A cheap box test first; most pairs are kilometres apart.
			if math.Abs(c.Lat-p[0]) > 0.002 || math.Abs(c.Lon-p[1]) > 0.002 {
				continue
			}
			if DistanceM(c.Lat, c.Lon, p[0], p[1]) <= ZoneRadiusM {
				o.markLocked(c.ID, simulated, now)
				n++
				break
			}
		}
	}
	return n
}

// CellCentre is a cell id with its centre.
type CellCentre struct {
	ID       string
	Lat, Lon float64
}

// HasSimulated reports whether any simulated evidence is recorded.
func (o *Origins) HasSimulated() bool {
	o.mu.RLock()
	defer o.mu.RUnlock()
	return len(o.sim) > 0 || o.Demo
}

// Label returns the origin of a cell whose centre is at lat, lon. Cells
// inside an active zone are marked first, so the label is stable afterwards.
func (o *Origins) Label(cell string, lat, lon float64) string {
	now := o.now()
	o.mu.Lock()
	for _, z := range o.zones {
		if z.until.After(now) && DistanceM(lat, lon, z.lat, z.lon) <= ZoneRadiusM {
			o.markLocked(cell, z.simulated, now)
		}
	}
	_, s := o.sim[cell]
	_, r := o.real[cell]
	demo := o.Demo && (o.DemoArea == nil || o.DemoArea(lat, lon))
	o.mu.Unlock()
	switch {
	case s && r:
		return OriginMixed
	case s:
		return OriginSimulated
	case r:
		return OriginReported
	case demo:
		return OriginSimulated
	default:
		return OriginReported
	}
}

// IsSimulatedOnly reports whether a cell holds only simulated evidence.
func (o *Origins) IsSimulatedOnly(cell string) bool {
	o.mu.RLock()
	defer o.mu.RUnlock()
	_, s := o.sim[cell]
	_, r := o.real[cell]
	return s && !r
}

// HasSimulatedEvidence reports whether simulated readings touched a cell,
// or, in a demo, whether nothing is known about it.
func (o *Origins) HasSimulatedEvidence(cell string) bool {
	o.mu.RLock()
	defer o.mu.RUnlock()
	_, s := o.sim[cell]
	_, r := o.real[cell]
	return s || (o.Demo && !r)
}

// snapshot is the file format.
type snapshot struct {
	Version        int                  `json:"version"`
	SavedAt        time.Time            `json:"saved_at"`
	RealCells      map[string]time.Time `json:"real_cells"`
	SimulatedCells map[string]time.Time `json:"simulated_cells"`
	RealReadings   []RealReading        `json:"real_readings"`
}

// FileName is the provenance file inside the data directory.
const FileName = "provenance.json"

// Load reads a saved record. A missing file is not an error.
func (o *Origins) Load(path string) (int, error) {
	raw, err := os.ReadFile(path)
	if errors.Is(err, fs.ErrNotExist) {
		return 0, nil
	}
	if err != nil {
		return 0, err
	}
	var s snapshot
	if err := json.Unmarshal(raw, &s); err != nil {
		return 0, fmt.Errorf("provenance file %s is not valid: %w", path, err)
	}
	if s.Version != 1 {
		return 0, fmt.Errorf("provenance file %s has version %d, want 1", path, s.Version)
	}
	o.mu.Lock()
	defer o.mu.Unlock()
	for k, v := range s.RealCells {
		o.real[k] = v
	}
	for k, v := range s.SimulatedCells {
		o.sim[k] = v
	}
	o.records = append(s.RealReadings, o.records...)
	o.pruneLocked(o.now())
	return len(o.real) + len(o.sim), nil
}

func (o *Origins) pruneLocked(now time.Time) {
	for k, t := range o.sim {
		if now.Sub(t) > Forget {
			delete(o.sim, k)
		}
	}
	for k, t := range o.real {
		if now.Sub(t) > Forget {
			delete(o.real, k)
		}
	}
	kept := o.records[:0]
	for _, r := range o.records {
		if now.Sub(r.ReceivedAt) <= Forget {
			kept = append(kept, r)
		}
	}
	o.records = kept
}

// Save writes the record atomically (temporary file, then rename) when it
// changed since the last save.
func (o *Origins) Save(path string) error {
	now := o.now()
	o.mu.Lock()
	if !o.dirty {
		o.mu.Unlock()
		return nil
	}
	o.pruneLocked(now)
	s := snapshot{Version: 1, SavedAt: now.UTC(), RealCells: map[string]time.Time{}, SimulatedCells: map[string]time.Time{}}
	for k, v := range o.real {
		s.RealCells[k] = v
	}
	for k, v := range o.sim {
		s.SimulatedCells[k] = v
	}
	s.RealReadings = append([]RealReading(nil), o.records...)
	o.dirty = false
	o.mu.Unlock()
	sort.Slice(s.RealReadings, func(i, j int) bool { return s.RealReadings[i].ReceivedAt.Before(s.RealReadings[j].ReceivedAt) })

	raw, err := json.Marshal(s)
	if err == nil {
		err = writeAtomic(path, raw)
	}
	if err != nil {
		o.mu.Lock()
		o.dirty = true
		o.mu.Unlock()
	}
	return err
}

func writeAtomic(path string, data []byte) error {
	dir := filepath.Dir(path)
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return err
	}
	f, err := os.CreateTemp(dir, ".provenance-*.json")
	if err != nil {
		return err
	}
	tmp := f.Name()
	if _, err := f.Write(data); err != nil {
		f.Close()
		os.Remove(tmp)
		return err
	}
	if err := f.Sync(); err != nil {
		f.Close()
		os.Remove(tmp)
		return err
	}
	if err := f.Close(); err != nil {
		os.Remove(tmp)
		return err
	}
	return os.Rename(tmp, path)
}

// DistanceM is the haversine distance in metres.
func DistanceM(lat1, lon1, lat2, lon2 float64) float64 {
	const r = 6371008.8
	p1, p2 := lat1*math.Pi/180, lat2*math.Pi/180
	dp := p2 - p1
	dl := (lon2 - lon1) * math.Pi / 180
	a := math.Sin(dp/2)*math.Sin(dp/2) + math.Cos(p1)*math.Cos(p2)*math.Sin(dl/2)*math.Sin(dl/2)
	return 2 * r * math.Asin(math.Min(1, math.Sqrt(a)))
}
