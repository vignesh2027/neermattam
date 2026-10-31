package places

import (
	"strings"
	"testing"
)

func TestPresetsAndNear(t *testing.T) {
	for _, p := range Presets {
		if p.Lat < 12.80 || p.Lat > 13.24 || p.Lon < 80.10 || p.Lon > 80.33 {
			t.Errorf("%s is outside the service area", p.Name)
		}
	}
	if got := Near(12.9795, 80.2212); got != "near Velachery" {
		t.Errorf("got %q", got)
	}
	if got := Near(12.5, 79.5); !strings.HasPrefix(got, "at ") {
		t.Errorf("far point: %q", got)
	}
}

func TestEveryCityHasPresetsWithUniqueIDs(t *testing.T) {
	want := []string{"chennai", "mumbai", "bengaluru", "hyderabad", "kolkata", "delhi", "visakhapatnam"}
	seen := map[string]bool{}
	for _, c := range want {
		list := CityPresets[c]
		if len(list) < 5 {
			t.Errorf("%s has %d presets, want at least 5", c, len(list))
		}
		for _, p := range list {
			if p.ID == "" || p.Name == "" || seen[p.ID] {
				t.Errorf("%s: preset %+v has no id, no name or a repeated id", c, p)
			}
			seen[p.ID] = true
			if got, ok := PresetByID(p.ID); !ok || got != p {
				t.Errorf("PresetByID(%q) = %+v", p.ID, got)
			}
		}
	}
	if len(CityPresets) != len(want) {
		t.Errorf("presets for %d cities, want %d", len(CityPresets), len(want))
	}
	if Presets[0].ID != "kodambakkam" || Presets[1].ID != "guindy" {
		t.Errorf("Chennai's default pair changed: %s to %s; recheck it against the simulated scenario (internal/demo/presets_live_test.go)", Presets[0].Name, Presets[1].Name)
	}
}
