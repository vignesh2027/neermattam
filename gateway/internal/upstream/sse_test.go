package upstream

import (
	"context"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
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

func TestMeasureStream(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/event-stream")
		// An unnamed step is recognised by its shape.
		io.WriteString(w, "data: {\"step\":\"scene\",\"latency_ms\":5}\n\nevent: step\ndata: {\"step\":\"segment\",\"latency_ms\":7}\n\nevent: result\ndata: {\"usable\":true,\"depth_cm\":10,\"depth_sigma_cm\":2,\"method\":\"wheel_geometry\"}\n\n")
	}))
	defer srv.Close()
	img := &Upload{Field: "image", Filename: "a.jpg", MIME: "image/jpeg", Data: []byte{1}}
	p := NewPerception(srv.URL, 5*time.Second, nil)
	var steps []string
	m, err := p.MeasureStream(context.Background(), MeasureRequest{Image: img}, func(s AgentStep) { steps = append(steps, s.Step) })
	if err != nil || !m.MethodIs(MethodWheelGeometry) || strings.Join(steps, ",") != "scene,segment" {
		t.Fatalf("%v %v %v", m, err, steps)
	}
}

func TestMeasureStreamErrorAndUnsupported(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if strings.Contains(r.Header.Get("X-Mode"), "missing") {
			http.NotFound(w, r)
			return
		}
		w.Header().Set("Content-Type", "text/event-stream")
		io.WriteString(w, "event: error\ndata: {\"detail\":\"model unavailable\",\"status\":503}\n\n")
	}))
	defer srv.Close()
	img := &Upload{Field: "image", Filename: "a.jpg", MIME: "image/jpeg", Data: []byte{1}}
	p := NewPerception(srv.URL, 5*time.Second, nil)
	_, err := p.MeasureStream(context.Background(), MeasureRequest{Image: img}, nil)
	if !IsStatus(err, 503) {
		t.Fatalf("error event: %v", err)
	}
	gone := httptest.NewServer(http.NotFoundHandler())
	defer gone.Close()
	p = NewPerception(gone.URL, 5*time.Second, nil)
	if _, err := p.MeasureStream(context.Background(), MeasureRequest{Image: img}, nil); err != ErrStreamUnsupported {
		t.Fatalf("404: %v", err)
	}
}
