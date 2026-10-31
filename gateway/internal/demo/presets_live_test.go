package demo

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"testing"
	"time"

	"neermattam/gateway/internal/live"
	"neermattam/gateway/internal/places"
	"neermattam/gateway/internal/upstream"
)

// TestChennaiDefaultRouteBendsAroundTheScenario runs against a real engine
// with Chennai's road graph loaded, so it is skipped unless
// NEERMATTAM_ENGINE_URL names one. Use a fresh engine with no snapshot
// (ENGINE_SNAPSHOT_PATH= ), since it seeds the simulated scenario:
//
//	cd engine && ENGINE_SNAPSHOT_PATH= ENGINE_BIND=127.0.0.1:18399 cargo run --release &
//	cd gateway && NEERMATTAM_ENGINE_URL=http://127.0.0.1:18399 go test ./internal/demo -run Default -v
//
// The route form opens on Chennai's first two presets for a two-wheeler.
// With the scenario's water on the map, that route must exist for a
// two-wheeler and a car and go round water the shortest route crosses, both
// as seeded and with the water 18 percent higher (the top of the drip's
// rise and fall), so the first click shows a detour, not "No dry route".
func TestChennaiDefaultRouteBendsAroundTheScenario(t *testing.T) {
	base := os.Getenv("NEERMATTAM_ENGINE_URL")
	if base == "" {
		t.Skip("set NEERMATTAM_ENGINE_URL to a fresh engine to run this check")
	}
	eng := upstream.NewEngine(base, 10*time.Second, 30*time.Second, nil)
	ctx := context.Background()
	s := &Seeder{Engine: eng, Origins: live.NewOrigins()}
	from, to := places.Presets[0], places.Presets[1]
	check := func(phase string) {
		for _, v := range []string{"two_wheeler", "car"} {
			raw, err := eng.Route(ctx, from.Coord(), to.Coord(), v)
			if err != nil {
				t.Fatalf("%s, %s: %v", phase, v, err)
			}
			var r struct {
				Properties struct {
					Kind      string  `json:"route_kind"`
					Passable  bool    `json:"passable"`
					Avoided   int     `json:"cells_avoided"`
					DistanceM float64 `json:"distance_m"`
				} `json:"properties"`
				Baseline struct {
					Properties struct {
						Impassable int     `json:"impassable_cells_crossed"`
						DistanceM  float64 `json:"distance_m"`
					} `json:"properties"`
				} `json:"baseline"`
			}
			if err := json.Unmarshal(raw, &r); err != nil {
				t.Fatal(err)
			}
			p := r.Properties
			t.Logf("%s, %s, %s to %s: %s, %.0f m against %.0f m shortest, %d cells avoided", phase, v, from.Name, to.Name, p.Kind, p.DistanceM, r.Baseline.Properties.DistanceM, p.Avoided)
			if p.Kind != "water_aware" || !p.Passable || p.Avoided < 1 || r.Baseline.Properties.Impassable < 1 {
				t.Errorf("%s, %s: want a dry route that avoids water on the shortest one, got %+v, baseline %+v", phase, v, p, r.Baseline.Properties)
			}
		}
	}
	plan := s.Plan(time.Now())
	for _, r := range plan {
		if _, err := eng.PostReading(ctx, r); err != nil {
			t.Fatalf("seed: %v", err)
		}
	}
	check("as seeded")
	for i, r := range plan {
		r.ReadingID = fmt.Sprintf("%s_high_%d", r.ReadingID, i)
		r.DepthCm *= 1.18
		r.ObservedAt = time.Now().UTC().Truncate(time.Second)
		if _, err := eng.PostReading(ctx, r); err != nil {
			t.Fatalf("seed high water: %v", err)
		}
	}
	check("18 percent higher")
}
