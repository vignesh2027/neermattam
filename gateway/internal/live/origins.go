// Package live fans the engine's cell stream out to browsers and keeps
// simulated and real evidence apart. The engine does not know which readings
// were simulated, so the gateway labels every cell it serves:
//
//   - "reported": only real reports touched it
//   - "simulated": only the demo scenario touched it
//   - "mixed": both did
//
// A label is attached by cell id. Because the engine streams a reading's
// seven changed cells before the gateway has seen its response, the gateway
// also registers a short-lived zone around each reading it is about to post;
// a streamed cell whose centre falls inside a zone is labelled from it.
package live

import (
	"math"
	"sync"
	"time"
)

// Origin labels.
const (
	OriginReported  = "reported"
	OriginSimulated = "simulated"
	OriginMixed     = "mixed"
)

// ZoneRadiusM covers a reading's own H3 resolution 10 cell and its ring of
// six neighbours (centre spacing about 115 m) with margin.
const ZoneRadiusM = 160.0

type zone struct {
	lat, lon  float64
	simulated bool
	until     time.Time
}

// Origins tracks which cells hold simulated and real evidence.
type Origins struct {
	mu    sync.RWMutex
	sim   map[string]struct{}
	real  map[string]struct{}
	zones []zone
	now   func() time.Time
}

// NewOrigins returns an empty tracker.
func NewOrigins() *Origins {
	return &Origins{sim: map[string]struct{}{}, real: map[string]struct{}{}, now: time.Now}
}

// Mark records that a cell holds simulated or real evidence.
func (o *Origins) Mark(cell string, simulated bool) {
	if cell == "" {
		return
	}
	o.mu.Lock()
	defer o.mu.Unlock()
	if simulated {
		o.sim[cell] = struct{}{}
	} else {
		o.real[cell] = struct{}{}
	}
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

// HasSimulated reports whether any simulated evidence was ever posted.
func (o *Origins) HasSimulated() bool {
	o.mu.RLock()
	defer o.mu.RUnlock()
	return len(o.sim) > 0
}

// Label returns the origin of a cell whose centre is at lat, lon. Cells
// inside an active zone are marked first, so the label is stable afterwards.
func (o *Origins) Label(cell string, lat, lon float64) string {
	now := o.now()
	o.mu.Lock()
	for _, z := range o.zones {
		if z.until.After(now) && DistanceM(lat, lon, z.lat, z.lon) <= ZoneRadiusM {
			if z.simulated {
				o.sim[cell] = struct{}{}
			} else {
				o.real[cell] = struct{}{}
			}
		}
	}
	_, s := o.sim[cell]
	_, r := o.real[cell]
	o.mu.Unlock()
	switch {
	case s && r:
		return OriginMixed
	case s:
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

// HasSimulatedEvidence reports whether simulated readings touched a cell.
func (o *Origins) HasSimulatedEvidence(cell string) bool {
	o.mu.RLock()
	defer o.mu.RUnlock()
	_, s := o.sim[cell]
	return s
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
