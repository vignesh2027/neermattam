package metrics

import (
	"bytes"
	"strings"
	"testing"
)

func TestExpositionFormat(t *testing.T) {
	r := NewRegistry()
	c := r.NewCounterVec("t_requests_total", "Requests.", "route", "code")
	c.Inc("GET /", "200")
	c.Add(2, `GET "x"`, "500")
	h := r.NewHistogramVec("t_seconds", "Latency.", []float64{0.1, 1}, "route")
	h.Observe(0.05, "a")
	h.Observe(0.5, "a")
	h.Observe(5, "a")
	r.NewGaugeFunc("t_up", "Up.", func() float64 { return 1 })
	var b bytes.Buffer
	r.Write(&b)
	out := b.String()
	for _, want := range []string{
		"# TYPE t_requests_total counter",
		`t_requests_total{route="GET /",code="200"} 1`,
		`t_requests_total{route="GET \"x\"",code="500"} 2`,
		`t_seconds_bucket{route="a",le="0.1"} 1`,
		`t_seconds_bucket{route="a",le="1"} 2`,
		`t_seconds_bucket{route="a",le="+Inf"} 3`,
		`t_seconds_count{route="a"} 3`,
		"t_up 1",
	} {
		if !strings.Contains(out, want) {
			t.Errorf("missing %q in\n%s", want, out)
		}
	}
	if c.Value("GET /", "200") != 1 {
		t.Error("Value")
	}
}
