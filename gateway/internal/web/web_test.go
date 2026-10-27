package web

import (
	"strings"
	"testing"
	"time"

	"neermattam/gateway/internal/report"
	"neermattam/gateway/internal/upstream"
	"neermattam/gateway/internal/verify"
)

func TestResultCardRendersEveryStatus(t *testing.T) {
	r, err := NewRenderer()
	if err != nil {
		t.Fatal(err)
	}
	d, s := 23.4, 3.1
	fused := &report.Outcome{
		Status: report.StatusFused, Place: "near Velachery", Source: "photo", ReceivedAt: time.Now(), DepthCm: &d, DepthSigmaCm: &s,
		Measurement: &upstream.Measurement{Usable: true, Confidence: 0.82, OverlayPNGB64: "not base64 at all",
			Reference: &upstream.Reference{Kind: "two_wheeler_wheel"}},
		Verification: &verify.Result{Accepted: true, Trust: 0.72, Rules: []verify.RuleResult{{Rule: "photo_freshness", Outcome: verify.Lower, Factor: 0.8, Reason: "taken 3 hours ago"}}},
		Cell:         &upstream.ReadingResult{DepthCm: 21, DepthSigmaCm: 2.4, DepthUpperCm: 25, NReadings: 3, Hazard: upstream.Hazard{Class: "danger_some"}},
		Advisory:     report.Advisory,
	}
	html, err := r.Fragment("result_card", NewResultView(fused))
	if err != nil {
		t.Fatal(err)
	}
	for _, want := range []string{"23<small>cm</small>", "± 3.1 cm", "Near Velachery", "Taken 3 hours ago", "× 0.80", "Danger for some", "3 reports", "Advisory only"} {
		if !strings.Contains(html, want) {
			t.Errorf("fused card lacks %q", want)
		}
	}
	if strings.Contains(html, "data:image") || strings.Contains(html, "ZgotmplZ") {
		t.Error("an invalid overlay must not be embedded")
	}
	unusable := &report.Outcome{Status: report.StatusUnusable, Summary: "The photo could not be measured.", ReceivedAt: time.Now(),
		Measurement: &upstream.Measurement{Reasons: []string{"no reference object found"}}, Advisory: report.Advisory}
	html, _ = r.Fragment("result_card", NewResultView(unusable))
	if !strings.Contains(html, "no reference object found") || !strings.Contains(html, "could not measure") {
		t.Errorf("unusable card: %s", html)
	}
	html, _ = r.Fragment("result_card", ErrorView("The measurement service is not reachable right now."))
	if !strings.Contains(html, "not reachable") {
		t.Error("error card")
	}
}

func TestGaugeScalesForDeepWater(t *testing.T) {
	g := newGauge(20, 3)
	if g.Max != 100 || g.MarkPct != "20.0%" || g.Ticks[0].Pct != "15.0%" {
		t.Fatalf("%+v", g)
	}
	if g := newGauge(120, 10); g.Max != 150 {
		t.Fatalf("deep water gauge max %d", g.Max)
	}
}

func TestRouteViewEscapesGeoJSON(t *testing.T) {
	raw := []byte(`{"type":"Feature","geometry":{"type":"LineString","coordinates":[[80.2,12.9],[80.21,12.91]]},"properties":{"route_kind":"least_bad","distance_m":5000,"eta_min":15,"max_depth_cm_on_route":22.4,"max_depth_sigma_cm":5.1,"cells_on_route":10,"cells_avoided":0,"unknown_cells_crossed":4,"impassable_cells_crossed":2,"passable":false,"explanation":"</script><b>x</b>","depth_limit_cm":15,"safety_sigmas":1.5},
		"baseline":{"properties":{"distance_m":4000,"eta_min":12,"impassable_cells_crossed":3}}}`)
	v, err := NewRouteView(raw, "car", true)
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(v.GeoJSON), "</script>") {
		t.Fatal("GeoJSON could close its script element")
	}
	if v.Passable || v.Stats[2].Value != "22 ± 5 cm" || !strings.Contains(v.Stats[0].Note, "1.0 km longer") {
		t.Fatalf("%+v", v.Stats)
	}
}
