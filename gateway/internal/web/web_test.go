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
	html, err := r.Fragment("en", "result_card", NewResultView(fused, "en"))
	if err != nil {
		t.Fatal(err)
	}
	for _, want := range []string{"23<small>cm</small>", "± 3.1 cm", "Near Velachery", "Taken 3 hours ago", "× 0.80", "Danger for some", "Reports fused there: 3", "Advisory only"} {
		if !strings.Contains(html, want) {
			t.Errorf("fused card lacks %q", want)
		}
	}
	if strings.Contains(html, "data:image") || strings.Contains(html, "ZgotmplZ") {
		t.Error("an invalid overlay must not be embedded")
	}
	unusable := &report.Outcome{Status: report.StatusUnusable, Summary: "The photo could not be measured.", ReceivedAt: time.Now(),
		Measurement: &upstream.Measurement{Reasons: []string{"no reference object found"}}, Advisory: report.Advisory}
	html, _ = r.Fragment("en", "result_card", NewResultView(unusable, "en"))
	if !strings.Contains(html, "no reference object found") || !strings.Contains(html, "could not measure") {
		t.Errorf("unusable card: %s", html)
	}
	html, _ = r.Fragment("en", "result_card", ErrorView("en", &report.Error{Message: "The measurement service is not reachable right now."}))
	if !strings.Contains(html, "not reachable") {
		t.Error("error card")
	}
}

func TestGaugeScalesForDeepWater(t *testing.T) {
	g := newGauge("en", 20, 3)
	if g.Max != 100 || g.MarkPct != "20.0%" || g.Ticks[0].Pct != "15.0%" {
		t.Fatalf("%+v", g)
	}
	if g := newGauge("en", 120, 10); g.Max != 150 {
		t.Fatalf("deep water gauge max %d", g.Max)
	}
}

func TestRouteViewEscapesGeoJSON(t *testing.T) {
	raw := []byte(`{"type":"Feature","geometry":{"type":"LineString","coordinates":[[80.2,12.9],[80.21,12.91]]},"properties":{"route_kind":"least_bad","distance_m":5000,"eta_min":15,"max_depth_cm_on_route":22.4,"max_depth_sigma_cm":5.1,"cells_on_route":10,"cells_avoided":0,"unknown_cells_crossed":4,"impassable_cells_crossed":2,"passable":false,"explanation":"</script><b>x</b>","depth_limit_cm":15,"safety_sigmas":1.5},
		"baseline":{"properties":{"distance_m":4000,"eta_min":12,"impassable_cells_crossed":3}}}`)
	v, err := NewRouteView(raw, "car", true, "en")
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

func TestTraceStepClassification(t *testing.T) {
	cases := []struct {
		step, decision, kind, tone, label, detail string
	}{
		{"scene", "segment and verify 2 wheels and the water", "scene", "neutral", "Read the scene", ""},
		{"segment", "verify", "segment", "neutral", "Segment", ""},
		{"verify", "accept", "verify", "ok", "Verify", ""},
		{"confirm", "accept (coverage partial)", "confirm", "ok", "Confirm", "coverage partial"},
		{"verify", "refine with box [1402,636,1519,813] px", "refine", "refine", "Refine", "with box [1402,636,1519,813] px"},
		{"verify", "give up: tyre seen head-on, its outline cannot be measured", "giveup", "stop", "Give up", "tyre seen head-on, its outline cannot be measured"},
		{"geometry", "report measurement", "geometry", "neutral", "Geometry", ""},
		{"estimate", "report model estimate", "estimate", "neutral", "Estimate", ""},
		{"mystery", "", "other", "neutral", "Mystery", ""},
		{"budget", "answer with what was verified in time", "budget", "neutral", "Time budget", ""},
	}
	for _, c := range cases {
		v := NewTraceStep("en", 0, upstream.AgentStep{Step: c.step, Decision: c.decision, LatencyMs: 11377})
		if v.Kind != c.kind || v.Tone != c.tone || v.Label != c.label || v.Detail != c.detail {
			t.Errorf("%s/%q: got kind %s tone %s label %s detail %q", c.step, c.decision, v.Kind, v.Tone, v.Label, v.Detail)
		}
		if v.Latency != "11.4 s" || !v.Slow {
			t.Errorf("latency %q slow %v", v.Latency, v.Slow)
		}
	}
	tv := NewTraceView("en", []upstream.AgentStep{
		{Step: "scene", Target: "photo", Model: "muse-spark-open", LatencyMs: 15000},
		{Step: "segment", Target: "wheel 1", Model: "sam-2.1-hiera-small", Decision: "verify", LatencyMs: 900},
		{Step: "verify", Target: "wheel 1", Model: "muse-spark-open", Decision: "give up", LatencyMs: 61000},
	}, false)
	if tv.Count != 3 || tv.GiveUps != 1 || tv.TargetsSeen != 1 || len(tv.ModelsUsed) != 2 || tv.TotalTime != "1 min 16 s" {
		t.Fatalf("%+v", tv)
	}
}

func TestTracePanelRenders(t *testing.T) {
	r, _ := NewRenderer()
	tv := NewTraceView("en", []upstream.AgentStep{{Step: "verify", Target: "two wheeler wheel 1", Model: "muse-spark-open", Input: "crop <b>", Output: "is_target=true", Decision: "accept", LatencyMs: 18000}}, false)
	html, err := r.Fragment("en", "trace_panel", tv)
	if err != nil {
		t.Fatal(err)
	}
	// The target is named in the page language: the agent writes the kind
	// with spaces, and English shows it as "two-wheeler wheel 1".
	for _, w := range []string{"How the models reached this answer", "two-wheeler wheel 1", "18.0 s", "Accept", "crop &lt;b&gt;", "timeline reveal"} {
		if !strings.Contains(html, w) {
			t.Errorf("panel lacks %q", w)
		}
	}
}

func TestResultCardLinksTheCertificateAndTraceShowsPromptVersions(t *testing.T) {
	r, _ := NewRenderer()
	d, s := 23.4, 3.1
	o := &report.Outcome{Status: report.StatusFused, Source: "photo", ReceivedAt: time.Now(), DepthCm: &d, DepthSigmaCm: &s,
		Measurement: &upstream.Measurement{Usable: true}, CertificateURL: "/verify/m_01M660TESTVERIFY"}
	for lang, want := range map[string]string{"en": "Verify this measurement", "ta": "இந்த அளவீட்டைச் சரிபார்க்கவும்", "hi": "इस माप की पुष्टि करें"} {
		html, err := r.Fragment(lang, "result_card", NewResultView(o, lang))
		if err != nil {
			t.Fatal(err)
		}
		if !strings.Contains(html, `href="/verify/m_01M660TESTVERIFY"`) || !strings.Contains(html, want) {
			t.Errorf("%s card lacks the verify link", lang)
		}
	}
	o.CertificateURL = "javascript:alert(1)"
	if html, _ := r.Fragment("en", "result_card", NewResultView(o, "en")); strings.Contains(html, "Verify this measurement") {
		t.Error("a link that is not a verify page was shown")
	}
	o.CertificateURL = ""
	if html, _ := r.Fragment("en", "result_card", NewResultView(o, "en")); strings.Contains(html, "vfy-link") {
		t.Error("a verify link without a certificate")
	}
	tv := NewTraceView("en", []upstream.AgentStep{
		{Step: "verify", Target: "wheel 1", Model: "muse-spark-open", Decision: "accept", PromptVersion: "verify-wheel@1d51d73f5037"},
		{Step: "validate", Target: "wheel 1", Model: "validator", Output: "is_target: 'yes' is not true or false", Decision: "retry"},
	}, false)
	html, _ := r.Fragment("en", "trace_panel", tv)
	if !strings.Contains(html, "verify-wheel@1d51d73f5037") || tv.Steps[1].Kind != "validate" || tv.Steps[1].Label != "Check" {
		t.Errorf("trace: kind %s label %s", tv.Steps[1].Kind, tv.Steps[1].Label)
	}
}

// Every map is built by mapcore's create(), which must resize the map when
// its container's size settles: MapLibre skips its observer's first size
// report, and a map built before the layout settled drew in part of its box.
func TestMapsResizeToTheirSettledContainer(t *testing.T) {
	b, err := templateFS.ReadFile("templates/mapcore.html")
	if err != nil {
		t.Fatal(err)
	}
	src := string(b)
	create := src[strings.Index(src, "function create("):]
	create = create[:strings.Index(create, "return m;")]
	for _, want := range []string{"new ResizeObserver(", ".observe(m.getContainer())", "m.resize()", `m.once("load", fit)`} {
		if !strings.Contains(create, want) {
			t.Errorf("create() lacks %q", want)
		}
	}
}
