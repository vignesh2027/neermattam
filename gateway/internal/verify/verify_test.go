package verify

import (
	"context"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

type fakeCells struct {
	cells []NearbyCell
	err   error
}

func (f fakeCells) Nearby(context.Context, float64, float64, float64) ([]NearbyCell, error) {
	return f.cells, f.err
}

type fakeRain struct {
	s   RainSummary
	err error
}

func (f fakeRain) Rain(context.Context, float64, float64, time.Time) (RainSummary, error) {
	return f.s, f.err
}

var now = time.Date(2026, 10, 28, 10, 0, 0, 0, time.UTC)

func baseInput() Input {
	captured := now.Add(-20 * time.Minute)
	return Input{Lat: 12.98, Lon: 80.22, DepthCm: 30, DepthSigmaCm: 3, Confidence: 0.8, Source: "photo", CapturedAt: &captured, ReceivedAt: now}
}

func verifier(cells Neighbourhood, rain Rainfall) *Verifier {
	return &Verifier{T: DefaultThresholds(), Cells: cells, Rain: rain}
}

func rule(t *testing.T, r Result, name string) RuleResult {
	t.Helper()
	for _, x := range r.Rules {
		if x.Rule == name {
			return x
		}
	}
	t.Fatalf("rule %s missing", name)
	return RuleResult{}
}

func TestCleanReportKeepsFullTrust(t *testing.T) {
	v := verifier(fakeCells{cells: []NearbyCell{{DepthCm: 28, DepthSigmaCm: 3, DistanceM: 80}}}, fakeRain{s: RainSummary{Past48hMM: 60, Hours: 48}})
	r := v.Verify(context.Background(), baseInput())
	if !r.Accepted || r.Trust != 1 {
		t.Fatalf("got %+v", r)
	}
	if r.TimeSource != "exif" || !r.ObservedAt.Equal(now.Add(-20*time.Minute)) {
		t.Errorf("observed_at should be the capture time: %v %s", r.ObservedAt, r.TimeSource)
	}
	for _, x := range r.Rules {
		if x.Reason == "" {
			t.Errorf("rule %s has no reason", x.Rule)
		}
	}
}

func TestConfidenceFloor(t *testing.T) {
	v := verifier(nil, nil)
	in := baseInput()
	in.Confidence = 0.2
	r := v.Verify(context.Background(), in)
	if r.Accepted || rule(t, r, "confidence_floor").Outcome != Reject {
		t.Fatal("a 0.2 confidence measurement must be rejected")
	}
	in.Confidence = 0.45
	r = v.Verify(context.Background(), in)
	if c := rule(t, r, "confidence_floor"); !r.Accepted || c.Outcome != Lower || c.Factor != 0.75 {
		t.Fatalf("0.45 should scale trust by 0.45/0.6: %+v", c)
	}
	in.Confidence = -1
	if c := rule(t, v.Verify(context.Background(), in), "confidence_floor"); c.Outcome != Skip {
		t.Fatalf("a voice note has no confidence to check: %+v", c)
	}
}

func TestFreshness(t *testing.T) {
	v := verifier(nil, nil)
	cases := []struct {
		age     time.Duration
		outcome Outcome
		factor  float64
		source  string
	}{
		{30 * time.Minute, Pass, 1, "exif"},
		{3 * time.Hour, Lower, 0.8, "exif"},
		{9 * time.Hour, Lower, 0.5, "exif"},
		{30 * time.Hour, Reject, 0, "exif"},
		{-2 * time.Hour, Lower, 0.6, "received"}, // camera clock ahead
	}
	for _, c := range cases {
		in := baseInput()
		at := now.Add(-c.age)
		in.CapturedAt = &at
		r := v.Verify(context.Background(), in)
		f := rule(t, r, "photo_freshness")
		if f.Outcome != c.outcome || f.Factor != c.factor || r.TimeSource != c.source {
			t.Errorf("age %v: got %+v (%s)", c.age, f, r.TimeSource)
		}
	}
	in := baseInput()
	in.CapturedAt = nil
	r := v.Verify(context.Background(), in)
	if f := rule(t, r, "photo_freshness"); f.Outcome != Lower || f.Factor != 0.9 || r.TimeSource != "received" || !r.ObservedAt.Equal(now) {
		t.Errorf("missing EXIF: %+v %s", f, r.TimeSource)
	}
}

func TestNeighbourDisagreementLowersButNeverRejects(t *testing.T) {
	in := baseInput()
	in.DepthCm = 80
	v := verifier(fakeCells{cells: []NearbyCell{{DepthCm: 5, DepthSigmaCm: 2, DistanceM: 60}, {DepthCm: 8, DepthSigmaCm: 3, DistanceM: 150}}}, nil)
	r := v.Verify(context.Background(), in)
	a := rule(t, r, "neighbour_agreement")
	if !r.Accepted || a.Outcome != Lower || a.Factor != 0.4 || !strings.Contains(a.Reason, "not rejected") {
		t.Fatalf("got %+v accepted=%v", a, r.Accepted)
	}
	// A moderate difference lowers trust less.
	in.DepthCm = 26
	v = verifier(fakeCells{cells: []NearbyCell{{DepthCm: 10, DepthSigmaCm: 3, DistanceM: 100}}}, nil)
	if a := rule(t, v.Verify(context.Background(), in), "neighbour_agreement"); a.Outcome != Lower || a.Factor != 0.75 {
		t.Fatalf("moderate: %+v", a)
	}
	// Distance widens the tolerance: the same gap far away is fine.
	v = verifier(fakeCells{cells: []NearbyCell{{DepthCm: 10, DepthSigmaCm: 3, DistanceM: 290}}}, nil)
	if a := rule(t, v.Verify(context.Background(), in), "neighbour_agreement"); a.Outcome != Pass {
		t.Fatalf("far cell: %+v", a)
	}
	for _, f := range []fakeCells{{}, {err: errors.New("down")}} {
		if a := rule(t, verifier(f, nil).Verify(context.Background(), in), "neighbour_agreement"); a.Outcome != Skip || a.Factor != 1 {
			t.Fatalf("no cells or engine down must skip: %+v", a)
		}
	}
}

func TestRainfall(t *testing.T) {
	in := baseInput()
	in.DepthCm = 40
	dry := rule(t, verifier(nil, fakeRain{s: RainSummary{Past48hMM: 0.2, Hours: 48}}).Verify(context.Background(), in), "rainfall_plausibility")
	if dry.Outcome != Lower || dry.Factor != 0.6 {
		t.Fatalf("deep water on a dry day: %+v", dry)
	}
	in.DepthCm = 6
	if r := rule(t, verifier(nil, fakeRain{s: RainSummary{Past48hMM: 0.2, Hours: 48}}).Verify(context.Background(), in), "rainfall_plausibility"); r.Outcome != Pass {
		t.Fatalf("shallow water needs no rain: %+v", r)
	}
	if r := rule(t, verifier(nil, fakeRain{err: errors.New("timeout")}).Verify(context.Background(), in), "rainfall_plausibility"); r.Outcome != Skip {
		t.Fatalf("API down must skip: %+v", r)
	}
}

func TestTrustIsTheProductClampedAndVoiceCountsLess(t *testing.T) {
	in := baseInput()
	in.Source = "voice"
	in.Confidence = -1
	in.CapturedAt = nil
	in.DepthCm = 60
	v := verifier(fakeCells{cells: []NearbyCell{{DepthCm: 2, DepthSigmaCm: 1, DistanceM: 50}}}, fakeRain{s: RainSummary{Past48hMM: 0, Hours: 48}})
	r := v.Verify(context.Background(), in)
	want := 0.9 * 0.6 * 0.4 * 0.6
	if !r.Accepted || r.Trust < want-0.001 || r.Trust > want+0.001 {
		t.Fatalf("trust %v, want %v", r.Trust, want)
	}
}

func TestOpenMeteoSumsAndCaches(t *testing.T) {
	var calls atomic.Int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		q := r.URL.Query()
		if q.Get("hourly") != "precipitation" || q.Get("latitude") != "13.00" || q.Get("longitude") != "80.20" {
			t.Errorf("unexpected query %s", r.URL.RawQuery)
		}
		w.Write([]byte(`{"hourly":{"time":["2026-10-28T07:00","2026-10-28T08:00","2026-10-28T09:00","2026-10-28T10:00","2026-10-28T11:00","2026-10-26T09:00"],
			"precipitation":[1.5,2.0,null,4.0,9.0,7.0]}}`))
	}))
	defer srv.Close()
	o := NewOpenMeteo(srv.URL, time.Second, nil)
	s, err := o.Rain(context.Background(), 12.99, 80.21, now)
	if err != nil {
		t.Fatal(err)
	}
	// 07:00, 08:00 and 10:00 count; 11:00 is after the report and the
	// 26 October value is outside 48 hours; null is skipped.
	if s.Past48hMM != 7.5 || s.Past6hMM != 7.5 || s.Hours != 3 {
		t.Fatalf("got %+v", s)
	}
	if _, err := o.Rain(context.Background(), 13.01, 80.19, now); err != nil {
		t.Fatal(err)
	}
	if calls.Load() != 1 {
		t.Fatalf("a point in the same 0.05 degree square must hit the cache; %d calls", calls.Load())
	}
}

func TestModelEstimateConfidenceAndSource(t *testing.T) {
	in := baseInput()
	in.Method = "model_estimate"
	in.Confidence = 0.23 // perception keeps estimates low on purpose
	r := verifier(nil, nil).Verify(context.Background(), in)
	if c := rule(t, r, "confidence_floor"); !r.Accepted || c.Outcome != Pass {
		t.Fatalf("an estimate at 0.23 must not be rejected: %+v", c)
	}
	if s := rule(t, r, "source"); s.Outcome != Lower || s.Factor != 0.7 {
		t.Fatalf("source: %+v", s)
	}
	if r.Trust != 0.7 {
		t.Fatalf("trust %v", r.Trust)
	}
	in.Confidence = 0.1
	if r := verifier(nil, nil).Verify(context.Background(), in); r.Accepted {
		t.Fatal("an estimate below 0.15 must be rejected")
	}
	in.Method, in.Confidence = "wheel_geometry", 0.23
	if r := verifier(nil, nil).Verify(context.Background(), in); r.Accepted {
		t.Fatal("a measurement at 0.23 is below the normal floor")
	}
}

func TestLongAgesReadNaturally(t *testing.T) {
	for d, want := range map[time.Duration]string{
		30 * time.Hour:     "30 hours",
		100 * time.Hour:    "4 days",
		157909 * time.Hour: "about 18 years",
		90 * time.Minute:   "1.5 hours",
		20 * time.Second:   "under a minute",
	} {
		if got := roundDur(d); got != want {
			t.Errorf("%v: %q, want %q", d, got, want)
		}
	}
}

// Each method gets its own confidence floor and source weight: a wheel
// counts fully, a person less, a visual estimate less again, and a method
// this gateway does not know is read as an estimate, never a measurement.
func TestMethodWeights(t *testing.T) {
	for _, tc := range []struct {
		name       string
		method     string
		confidence float64
		accepted   bool
		confOut    Outcome
		confKey    string
		srcFactor  float64
		srcKey     string
		trust      float64
	}{
		{"wheel", "wheel_geometry", 0.8, true, Pass, "conf_ok", 1, "src_photo_geo", 1},
		{"wheel, low confidence", "wheel_geometry", 0.23, false, Reject, "conf_low", 1, "src_photo_geo", 1},
		{"person", "person_reference", 0.23, true, Pass, "conf_person", 0.85, "src_person", 0.85},
		{"person at the floor", "person_reference", 0.1, true, Pass, "conf_person", 0.85, "src_person", 0.85},
		{"person below the floor", "person_reference", 0.08, false, Reject, "conf_person_low", 0.85, "src_person", 0.85},
		{"estimate", "model_estimate", 0.22, true, Pass, "conf_estimate", 0.7, "src_estimate", 0.7},
		{"estimate below the floor", "model_estimate", 0.1, false, Reject, "conf_estimate_low", 0.7, "src_estimate", 0.7},
		{"unknown method", "stereo_depth", 0.9, true, Pass, "conf_estimate", 0.7, "src_unknown", 0.7},
		{"unknown method, low confidence", "stereo_depth", 0.12, false, Reject, "conf_estimate_low", 0.7, "src_unknown", 0.7},
		{"no method (older perception)", "", 0.8, true, Pass, "conf_ok", 1, "src_photo", 1},
	} {
		t.Run(tc.name, func(t *testing.T) {
			in := baseInput()
			in.Method, in.Confidence = tc.method, tc.confidence
			r := verifier(nil, nil).Verify(context.Background(), in)
			c := rule(t, r, "confidence_floor")
			if r.Accepted != tc.accepted || c.Outcome != tc.confOut || c.Key != tc.confKey {
				t.Fatalf("accepted %v, confidence %s %s; want %v, %s %s", r.Accepted, c.Outcome, c.Key, tc.accepted, tc.confOut, tc.confKey)
			}
			s := rule(t, r, "source")
			if s.Factor != tc.srcFactor || s.Key != tc.srcKey || s.Reason == "" {
				t.Fatalf("source %+v; want factor %v, key %s", s, tc.srcFactor, tc.srcKey)
			}
			if tc.accepted && r.Trust != tc.trust {
				t.Fatalf("trust %v, want %v", r.Trust, tc.trust)
			}
			if tc.srcKey == "src_unknown" && (len(s.Args) != 1 || s.Args[0] != tc.method || !strings.Contains(s.Reason, tc.method)) {
				t.Fatalf("the reason must name the unknown method: %+v", s)
			}
		})
	}
}

// A person measurement sits between a wheel and an estimate.
func TestPersonWeightIsBetweenWheelAndEstimate(t *testing.T) {
	trust := func(method string, c float64) float64 {
		in := baseInput()
		in.Method, in.Confidence = method, c
		return verifier(nil, nil).Verify(context.Background(), in).Trust
	}
	wheel, person, est := trust("wheel_geometry", 0.8), trust("person_reference", 0.2), trust("model_estimate", 0.2)
	if !(wheel > person && person > est) {
		t.Fatalf("wheel %v, person %v, estimate %v", wheel, person, est)
	}
}

func TestUnknownMethodNameIsCapped(t *testing.T) {
	in := baseInput()
	in.Method = strings.Repeat("x", 500)
	s := rule(t, verifier(nil, nil).Verify(context.Background(), in), "source")
	if s.Key != "src_unknown" || len(s.Args[0].(string)) > 40 {
		t.Fatalf("%+v", s)
	}
}
