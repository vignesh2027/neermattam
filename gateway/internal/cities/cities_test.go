package cities

import (
	"testing"

	"neermattam/gateway/internal/places"
)

func TestBuiltInCitiesMatchTheEngineDefaults(t *testing.T) {
	r := NewRegistry(nil)
	var ids []string
	for _, c := range r.All() {
		ids = append(ids, c.ID)
		if len(c.Presets) < 5 {
			t.Errorf("%s has %d route presets", c.ID, len(c.Presets))
		}
		for _, p := range c.Presets {
			if !c.Contains(p.Lat, p.Lon) {
				t.Errorf("%s preset %s at %.4f,%.4f is outside the city's box", c.ID, p.Name, p.Lat, p.Lon)
			}
		}
		if !c.Contains(c.CentreLat, c.CentreLon) {
			t.Errorf("%s centre is outside its box", c.ID)
		}
	}
	want := []string{"chennai", "mumbai", "bengaluru", "hyderabad", "kolkata", "delhi", "visakhapatnam"}
	if len(ids) != len(want) {
		t.Fatalf("cities %v, want %v", ids, want)
	}
	for i := range want {
		if ids[i] != want[i] {
			t.Fatalf("cities %v, want %v", ids, want)
		}
	}
	// Chennai reaches 12.80 N, so the Siruseri data centres are covered.
	ch, _ := r.Get("chennai")
	if !ch.Contains(12.8306, 80.2210) || ch.Contains(12.79, 80.22) {
		t.Errorf("Chennai box %+v", ch.BBox)
	}
	if c, ok := r.Find(17.7214, 83.2906); !ok || c.ID != "visakhapatnam" {
		t.Errorf("Visakhapatnam station is not in Visakhapatnam: %+v", c)
	}
	if len(places.CityPresets["visakhapatnam"]) == 0 {
		t.Error("no Visakhapatnam presets")
	}
}
