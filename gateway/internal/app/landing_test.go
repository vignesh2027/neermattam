package app

import (
	"context"
	"io"
	"net/http"
	"strings"
	"testing"
	"time"
)

// A sample photo goes through the real measuring service every time, is
// never refused as a duplicate, and is never added to the map.
func TestSamplePhotoIsMeasuredLiveButNeverFused(t *testing.T) {
	h := newHarness(t, nil, true)
	for i := 0; i < 2; i++ {
		resp := h.post(t, "/api/report", map[string]string{"sample": "nandanam"}, nil, "")
		if resp.StatusCode != 200 {
			b, _ := io.ReadAll(resp.Body)
			t.Fatalf("run %d: status %d %s", i, resp.StatusCode, b)
		}
		out := decode(t, resp)
		if out["status"] != "sample" || out["place"] != "Nandanam, Chennai" || out["depth_cm"] != 23.4 {
			t.Fatalf("run %d: outcome %v", i, out)
		}
		s, _ := out["sample"].(map[string]any)
		if s["id"] != "nandanam" || s["taken"] != "2015-12-03" {
			t.Fatalf("run %d: sample %v", i, out["sample"])
		}
		if out["verification"] != nil || out["cell"] != nil {
			t.Fatalf("run %d: a sample was checked against today's map or fused: %v", i, out)
		}
	}
	// The fake answers only the plain endpoint, so each run is one call there.
	if n := h.perc.calls.Load(); n != 2 {
		t.Fatalf("measuring service measured %d times, want 2 (no stored answer)", n)
	}
	h.engine.mu.Lock()
	defer h.engine.mu.Unlock()
	if len(h.engine.readings) != 0 {
		t.Fatalf("a sample reached the engine: %v", h.engine.readings)
	}
}

// The sample's own place wins over whatever the form says, so a Kolkata
// sample works from a page set to Chennai.
func TestSampleUsesItsOwnCity(t *testing.T) {
	h := newHarness(t, nil, true)
	resp := h.post(t, "/api/report", map[string]string{"sample": "kolkata-car", "city": "chennai", "lat": "12.9791", "lon": "80.2209"}, nil, "")
	if resp.StatusCode != 200 {
		b, _ := io.ReadAll(resp.Body)
		t.Fatalf("status %d %s", resp.StatusCode, b)
	}
	out := decode(t, resp)
	if out["status"] != "sample" || out["place"] != "Kolkata" || out["lat"] != 22.5726 {
		t.Fatalf("outcome %v", out)
	}
}

func TestUnknownSampleIsRefused(t *testing.T) {
	h := newHarness(t, nil, true)
	resp := h.post(t, "/api/report", map[string]string{"sample": "../../etc/passwd"}, nil, "")
	if resp.StatusCode != http.StatusBadRequest {
		t.Fatalf("status %d", resp.StatusCode)
	}
	if out := decode(t, resp); out["code"] != "unknown_sample" {
		t.Fatalf("outcome %v", out)
	}
	if n := h.perc.calls.Load() + h.perc.streamCalls.Load(); n != 0 {
		t.Fatalf("measuring service called %d times", n)
	}
}

// The report page streams a sample like any upload and renders the sample
// card in the page language.
func TestSampleOnTheReportPage(t *testing.T) {
	h := newHarness(t, nil, true)
	resp := h.post(t, "/report?lang=hi", map[string]string{"sample": "chennai-bike"}, nil, "text/event-stream")
	b, _ := io.ReadAll(resp.Body)
	body := string(b)
	for _, w := range []string{"event: done", `\"status\":\"sample\"`, "chip-sample", "dfig"} {
		if !strings.Contains(body, w) && !strings.Contains(body, strings.ReplaceAll(w, `\"`, `"`)) {
			t.Errorf("stream lacks %q", w)
		}
	}
	page, err := http.Get(h.srv.URL + "/report?sample=chennai-bike")
	if err != nil {
		t.Fatal(err)
	}
	pb, _ := io.ReadAll(page.Body)
	page.Body.Close()
	if !strings.Contains(string(pb), `class="try-card is-chosen" method="post" action="/report" enctype="multipart/form-data" data-sample="chennai-bike"`) {
		t.Error("the asked-for sample is not marked chosen")
	}
}

// Without GET /api/india the landing page still names every city with what
// the engine's city list knows, and says the live numbers are missing.
func TestLandingWithoutIndiaNumbers(t *testing.T) {
	h := newHarness(t, map[string]string{"DEMO": "1"}, true)
	if d, err := h.app.fetchIndia(context.Background()); err == nil && d != nil {
		t.Skip("this gateway serves /api/india; the fallback is covered by the view tests")
	}
	resp, err := http.Get(h.srv.URL + "/")
	if err != nil {
		t.Fatal(err)
	}
	b, _ := io.ReadAll(resp.Body)
	resp.Body.Close()
	body := string(b)
	for _, w := range []string{"Live rainfall and report counts could not be read just now", "Map cells with evidence", "Visakhapatnam", "Kolkata", "Delhi"} {
		if !strings.Contains(body, w) {
			t.Errorf("landing lacks %q", w)
		}
	}
	if strings.Contains(body, "Rain, last 24 h") {
		t.Error("rain row shown without live numbers")
	}
}

// Without a registered /api/india route the mux falls through to "/", the
// landing page, which asks for the same cache again. That must fail fast,
// not wait on the cache's lock forever (it took the live site down once).
func TestIndiaWithoutTheRouteDoesNotDeadlock(t *testing.T) {
	a := &App{mux: http.NewServeMux()}
	a.mux.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
		_, _ = a.india(r.Context())
	})
	done := make(chan error, 1)
	go func() { _, err := a.india(context.Background()); done <- err }()
	select {
	case err := <-done:
		if err == nil {
			t.Fatal("no /api/india route, yet the landing got numbers")
		}
	case <-time.After(3 * time.Second):
		t.Fatal("india() deadlocked on its own cache")
	}
}
