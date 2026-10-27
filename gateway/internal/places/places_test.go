package places

import (
	"strings"
	"testing"
)

func TestPresetsAndNear(t *testing.T) {
	for _, p := range Presets {
		if p.Lat < 12.85 || p.Lat > 13.24 || p.Lon < 80.10 || p.Lon > 80.33 {
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
