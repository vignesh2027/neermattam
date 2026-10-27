package upstream

import (
	"strings"
	"testing"
)

func TestParseSSE(t *testing.T) {
	in := "event: cell\ndata: {\"a\":1}\n\n: lagged: 3 events dropped\n\ndata: line1\ndata: line2\n\n"
	var got []Event
	_ = ParseSSE(strings.NewReader(in), func(e Event) { got = append(got, e) })
	if len(got) != 3 || got[0].Name != "cell" || got[0].Data != `{"a":1}` || got[1].Comment == "" || got[2].Data != "line1\nline2" || got[2].Name != "message" {
		t.Fatalf("%+v", got)
	}
}

func TestCentroid(t *testing.T) {
	lat, lon := Centroid([][][2]float64{{{0, 0}, {2, 0}, {2, 2}, {0, 2}, {0, 0}}})
	if lat != 1 || lon != 1 {
		t.Fatalf("%v %v", lat, lon)
	}
}
