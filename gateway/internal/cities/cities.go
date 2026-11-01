// Package cities is the gateway's view of the cities the engine covers. The
// list starts from a built-in copy of the engine's default cities (so pages
// work before the engine answers) and is refreshed from GET /v1/cities,
// which adds each city's cell count and routing state. Route presets are
// the gateway's own: a few well-known places per city whose coordinates
// were checked to snap to that city's road graph.
package cities

import (
	"context"
	"log/slog"
	"sort"
	"sync"
	"time"

	"neermattam/gateway/internal/config"
	"neermattam/gateway/internal/places"
	"neermattam/gateway/internal/upstream"
)

// Default is the city shown when none is chosen. It is also the only city
// that holds the simulated demo scenario.
const Default = "chennai"

// Routing states, as the engine reports a city's road graph.
const (
	RoutingReady   = "ready"
	RoutingLoading = "loading"
	RoutingFailed  = "failed"
	RoutingUnknown = "unknown"
)

// City is one covered city.
type City struct {
	ID                   string
	Name                 string
	BBox                 config.BBox
	CentreLat, CentreLon float64
	Cells                int    // cells with state, from the engine
	Routing              string // ready, loading, failed or unknown
	RoutingError         string
	Presets              []places.Place
}

// Contains reports whether a point lies in the city's box.
func (c City) Contains(lat, lon float64) bool { return c.BBox.Contains(lat, lon) }

// builtIn mirrors the engine's default ENGINE_CITIES (docs/ENGINE.md,
// Cities). The engine's answer replaces boxes and names when it differs.
var builtIn = []City{
	{ID: "chennai", Name: "Chennai", BBox: config.BBox{MinLon: 80.10, MinLat: 12.80, MaxLon: 80.33, MaxLat: 13.24}, CentreLat: 13.02, CentreLon: 80.215},
	{ID: "mumbai", Name: "Mumbai", BBox: config.BBox{MinLon: 72.77, MinLat: 18.88, MaxLon: 73.13, MaxLat: 19.32}, CentreLat: 19.1, CentreLon: 72.95},
	{ID: "bengaluru", Name: "Bengaluru", BBox: config.BBox{MinLon: 77.44, MinLat: 12.82, MaxLon: 77.80, MaxLat: 13.15}, CentreLat: 12.985, CentreLon: 77.62},
	{ID: "hyderabad", Name: "Hyderabad", BBox: config.BBox{MinLon: 78.24, MinLat: 17.22, MaxLon: 78.66, MaxLat: 17.62}, CentreLat: 17.42, CentreLon: 78.45},
	{ID: "kolkata", Name: "Kolkata", BBox: config.BBox{MinLon: 88.20, MinLat: 22.42, MaxLon: 88.52, MaxLat: 22.72}, CentreLat: 22.57, CentreLon: 88.36},
	{ID: "delhi", Name: "Delhi", BBox: config.BBox{MinLon: 76.84, MinLat: 28.40, MaxLon: 77.35, MaxLat: 28.89}, CentreLat: 28.645, CentreLon: 77.095},
	{ID: "visakhapatnam", Name: "Visakhapatnam", BBox: config.BBox{MinLon: 83.11, MinLat: 17.55, MaxLon: 83.53, MaxLat: 18.07}, CentreLat: 17.81, CentreLon: 83.32},
}

// Registry holds the current list.
type Registry struct {
	mu      sync.RWMutex
	list    []City
	fetched time.Time
	log     *slog.Logger
}

// NewRegistry starts from the built-in list.
func NewRegistry(log *slog.Logger) *Registry {
	r := &Registry{log: log}
	r.list = make([]City, len(builtIn))
	for i, c := range builtIn {
		c.Routing = RoutingUnknown
		c.Presets = places.CityPresets[c.ID]
		r.list[i] = c
	}
	return r
}

// All returns the cities in the engine's order.
func (r *Registry) All() []City {
	r.mu.RLock()
	defer r.mu.RUnlock()
	return append([]City(nil), r.list...)
}

// Get finds a city by id.
func (r *Registry) Get(id string) (City, bool) {
	r.mu.RLock()
	defer r.mu.RUnlock()
	for _, c := range r.list {
		if c.ID == id {
			return c, true
		}
	}
	return City{}, false
}

// Find returns the first city whose box holds the point.
func (r *Registry) Find(lat, lon float64) (City, bool) {
	r.mu.RLock()
	defer r.mu.RUnlock()
	for _, c := range r.list {
		if c.Contains(lat, lon) {
			return c, true
		}
	}
	return City{}, false
}

// Contains reports whether any city holds the point.
func (r *Registry) Contains(lat, lon float64) bool {
	_, ok := r.Find(lat, lon)
	return ok
}

// Names lists the city names, for messages.
func (r *Registry) Names() []string {
	r.mu.RLock()
	defer r.mu.RUnlock()
	out := make([]string, len(r.list))
	for i, c := range r.list {
		out[i] = c.Name
	}
	return out
}

// Fetched is when the engine last answered, or zero.
func (r *Registry) Fetched() time.Time {
	r.mu.RLock()
	defer r.mu.RUnlock()
	return r.fetched
}

// Refresh replaces the list with the engine's.
func (r *Registry) Refresh(ctx context.Context, e *upstream.Engine) error {
	got, err := e.Cities(ctx)
	if err != nil {
		return err
	}
	if len(got) == 0 {
		return nil
	}
	list := make([]City, 0, len(got))
	for _, ec := range got {
		c := City{
			ID: ec.ID, Name: ec.Name,
			BBox:      config.BBox{MinLon: ec.BBox[0], MinLat: ec.BBox[1], MaxLon: ec.BBox[2], MaxLat: ec.BBox[3]},
			CentreLat: ec.Centre.Lat, CentreLon: ec.Centre.Lon,
			Cells: ec.Cells, Routing: ec.Graph.Status, RoutingError: ec.Graph.Error,
		}
		if c.Name == "" {
			c.Name = c.ID
		}
		if c.Routing == "" {
			c.Routing = RoutingUnknown
		}
		// Presets outside the engine's box (a custom box) are dropped.
		for _, p := range places.CityPresets[c.ID] {
			if c.Contains(p.Lat, p.Lon) {
				c.Presets = append(c.Presets, p)
			}
		}
		list = append(list, c)
	}
	r.mu.Lock()
	r.list = list
	r.fetched = time.Now()
	r.mu.Unlock()
	return nil
}

// Run refreshes every interval until ctx ends, sooner while the engine has
// not answered yet.
func (r *Registry) Run(ctx context.Context, e *upstream.Engine, every time.Duration) {
	for {
		wait := every
		if err := r.Refresh(ctx, e); err != nil {
			wait = min(every, 5*time.Second)
			if r.log != nil {
				r.log.Debug("city list not refreshed", "error", err.Error())
			}
		}
		select {
		case <-ctx.Done():
			return
		case <-time.After(wait):
		}
	}
}

// SortedIDs returns the ids, for tests and stable output.
func (r *Registry) SortedIDs() []string {
	ids := []string{}
	for _, c := range r.All() {
		ids = append(ids, c.ID)
	}
	sort.Strings(ids)
	return ids
}
