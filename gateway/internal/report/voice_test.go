package report

import (
	"testing"
	"time"

	"neermattam/gateway/internal/upstream"
)

func TestParseDepthWords(t *testing.T) {
	cases := []struct {
		in    string
		depth float64
		ok    bool
	}{
		{"knee deep", 45, true},
		{"water up to my ankle", 10, true},
		{"muzhangal alavu thanni", 45, true},
		{"about 30 cm", 30, true},
		{"two feet", 0, false},
		{"2 feet of water", 61, true},
		{"10 inches", 25.4, true},
		{"waist", 95, true},
		{"", 0, false},
		{"it is raining", 0, false},
	}
	for _, c := range cases {
		got, ok := ParseDepthWords(c.in)
		if ok != c.ok || (ok && (got.DepthCm < c.depth-0.5 || got.DepthCm > c.depth+0.5)) {
			t.Errorf("%q: got %+v %v", c.in, got, ok)
		}
		if ok && got.SigmaCm < 5 {
			t.Errorf("%q: spoken depth sigma %v is too tight", c.in, got.SigmaCm)
		}
	}
}

func TestFromTranscriptPrefersTheModelsDepth(t *testing.T) {
	d, s := 45.0, 5.0
	est, ok := FromTranscript(&upstream.Transcript{Text: "knee deep", DepthWords: "knee deep", DepthCm: &d, DepthSigmaCm: &s, Model: "muse-spark-open"})
	if !ok || est.DepthCm != 45 || est.SigmaCm != 13.5 {
		t.Fatalf("sigma must be at least 30 percent: %+v", est)
	}
	est, ok = FromTranscript(&upstream.Transcript{Text: "water up to the waist"})
	if !ok || est.DepthCm != 95 {
		t.Fatalf("fallback to words: %+v %v", est, ok)
	}
	if _, ok := FromTranscript(&upstream.Transcript{Text: "it is raining"}); ok {
		t.Fatal("no depth spoken")
	}
}

func TestParseCapturedAt(t *testing.T) {
	tm, known, ok := ParseCapturedAt("2015-12-02T07:56:12")
	if !ok || known || tm.Format(time.RFC3339) != "2015-12-02T02:26:12Z" {
		t.Fatalf("local time is read as IST: %v %v %v", tm, known, ok)
	}
	tm, known, ok = ParseCapturedAt("2015-12-02T07:56:12Z")
	if !ok || !known || tm.Hour() != 7 {
		t.Fatalf("UTC: %v %v %v", tm, known, ok)
	}
	if _, _, ok := ParseCapturedAt("yesterday"); ok {
		t.Fatal("garbage accepted")
	}
}
