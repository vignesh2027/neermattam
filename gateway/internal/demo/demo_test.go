package demo

import (
	"strings"
	"testing"
	"time"

	"neermattam/gateway/internal/config"
)

func TestPlanIsLabelledAndInsideTheServiceArea(t *testing.T) {
	area, _ := config.ParseBBox("80.10,12.85,80.33,13.24")
	now := time.Now()
	plan := (&Seeder{}).Plan(now)
	if len(plan) < 300 {
		t.Fatalf("only %d readings", len(plan))
	}
	for _, r := range plan {
		if !strings.HasPrefix(r.ReadingID, "sim_") {
			t.Fatalf("simulated reading without the sim_ prefix: %s", r.ReadingID)
		}
		if !area.Contains(r.Lat, r.Lon) {
			t.Fatalf("reading outside the service area: %v,%v", r.Lat, r.Lon)
		}
		if r.Source != "" {
			t.Fatalf("a simulated reading must not claim a real source, got %q", r.Source)
		}
		if r.DepthCm < 0 || r.DepthCm > 500 || r.DepthSigmaCm <= 0 || now.Sub(r.ObservedAt) > 2*time.Hour {
			t.Fatalf("reading the engine would refuse: %+v", r)
		}
		if (r.VelocityMps == nil) != (r.VelocitySigmaMps == nil) {
			t.Fatalf("velocity without sigma: %+v", r)
		}
	}
}
