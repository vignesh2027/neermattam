package app

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"mime/multipart"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"neermattam/gateway/internal/config"
	"neermattam/gateway/internal/testmedia"
	"neermattam/gateway/internal/verify"
)

const cellJSON = `{"type":"Feature","id":"8a618c4e2d47fff","geometry":{"type":"Polygon","coordinates":[[[80.21821,12.990811],[80.218229,12.991533],[80.217619,12.991902],[80.216992,12.991549],[80.216973,12.990827],[80.217582,12.990458],[80.21821,12.990811]]]},"properties":{"cell":"8a618c4e2d47fff","depth_cm":42.0,"depth_sigma_cm":3.2,"depth_upper_cm":46.7,"velocity_mps":0.4,"velocity_sigma_mps":0.21,"velocity_assumed":false,"hazard_rating":1.38,"hazard_class":"danger_most","hazard_class_upper":"danger_most","debris_factor":1.0,"unknown":false,"n_readings":1,"n_neighbour":0,"updated_at":"2026-10-27T09:11:14Z"}}`

// fakeEngine imitates the engine's HTTP contract (CONTRACTS.md section 2).
type fakeEngine struct {
	srv        *httptest.Server
	cellsCalls atomic.Int32
	mu         sync.Mutex
	readings   []map[string]any
	events     chan string
}

func newFakeEngine(t *testing.T) *fakeEngine {
	e := &fakeEngine{events: make(chan string, 16)}
	mux := http.NewServeMux()
	mux.HandleFunc("GET /readyz", func(w http.ResponseWriter, _ *http.Request) { w.Write([]byte(`{"status":"ready"}`)) })
	mux.HandleFunc("GET /v1/cells", func(w http.ResponseWriter, r *http.Request) {
		e.cellsCalls.Add(1)
		if r.URL.Query().Get("bbox") == "" {
			http.Error(w, `{"error":"bbox is required"}`, 400)
			return
		}
		fmt.Fprintf(w, `{"type":"FeatureCollection","truncated":false,"features":[%s]}`, cellJSON)
	})
	mux.HandleFunc("POST /v1/readings", func(w http.ResponseWriter, r *http.Request) {
		var m map[string]any
		json.NewDecoder(r.Body).Decode(&m)
		e.mu.Lock()
		e.readings = append(e.readings, m)
		e.mu.Unlock()
		w.Write([]byte(`{"reading_id":"r_x","cell":"8a618c4e2d47fff","depth_cm":31.0,"depth_sigma_cm":2.4,"depth_upper_cm":34.6,"velocity_mps":null,"velocity_sigma_mps":null,"unknown":false,"n_readings":2,"updated_at":"2026-10-27T09:11:14Z","hazard":{"rating":1.1,"class":"danger_some","class_upper":"danger_most","debris_factor":1.0,"velocity_mps":1.0,"velocity_assumed":true},"duplicate":false}`))
	})
	mux.HandleFunc("GET /v1/route", func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Query().Get("vehicle") != "pedestrian" && r.URL.Query().Get("vehicle") != "car" {
			http.Error(w, `{"error":"bad vehicle"}`, 400)
			return
		}
		props := `"vehicle":"car","route_kind":"water_aware","distance_m":4171.0,"eta_min":12.5,"max_depth_cm_on_route":null,"max_depth_sigma_cm":null,"cells_on_route":37,"cells_avoided":1,"unknown_cells_crossed":37,"impassable_cells_crossed":0,"passable":true,"advisory":true,"depth_limit_cm":15.0,"pedestrian_block_class":null,"safety_sigmas":1.5,"explanation":"Advisory only. This route avoids 1 map cell."`
		fmt.Fprintf(w, `{"type":"Feature","geometry":{"type":"LineString","coordinates":[[80.2211,12.9791],[80.2206,13.0067]]},"properties":{%s},"baseline":{"type":"Feature","geometry":{"type":"LineString","coordinates":[[80.2211,12.9791],[80.2206,13.0067]]},"properties":{%s}}}`, props, strings.Replace(props, "water_aware", "baseline", 1))
	})
	mux.HandleFunc("GET /v1/stream", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/event-stream")
		w.WriteHeader(200)
		w.(http.Flusher).Flush()
		for {
			select {
			case <-r.Context().Done():
				return
			case ev := <-e.events:
				io.WriteString(w, ev)
				w.(http.Flusher).Flush()
			}
		}
	})
	e.srv = httptest.NewServer(mux)
	t.Cleanup(e.srv.Close)
	return e
}

type fakePerception struct {
	srv   *httptest.Server
	calls atomic.Int32
	body  string
}

func newFakePerception(t *testing.T, usable bool) *fakePerception {
	p := &fakePerception{}
	p.body = `{"measurement_id":"m_1","usable":true,"reasons":["standing water visible"],"depth_cm":23.4,"depth_sigma_cm":3.1,"reference":{"kind":"two_wheeler_wheel","diameter_cm":43.4,"diameter_sigma_cm":2.0,"source":"catalogue:90/100-10"},"waterline":{"method":"sam_mask_intersection","y_px":812.5,"agreement_cm":1.2},"velocity_mps":null,"velocity_sigma_mps":null,"confidence":0.82,"overlay_png_b64":"iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==","models":{"scene":"muse-spark-1.3","masks":"sam-3.1"},"timings_ms":{"scene":1400,"masks":900,"geometry":12},"cost_usd":0.0041}`
	if !usable {
		p.body = `{"measurement_id":"m_2","usable":false,"reasons":["no reference object found"],"depth_cm":null,"depth_sigma_cm":null,"confidence":0.1,"models":{}}`
	}
	p.srv = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/readyz" {
			w.Write([]byte(`{}`))
			return
		}
		p.calls.Add(1)
		if err := r.ParseMultipartForm(64 << 20); err != nil || (r.MultipartForm.File["image"] == nil && r.MultipartForm.File["video"] == nil) {
			http.Error(w, `{"detail":"image or video required"}`, 422)
			return
		}
		w.Write([]byte(p.body))
	}))
	t.Cleanup(p.srv.Close)
	return p
}

type noRain struct{}

func (noRain) Rain(context.Context, float64, float64, time.Time) (verify.RainSummary, error) {
	return verify.RainSummary{Past48hMM: 40, Hours: 48}, nil
}

type harness struct {
	app    *App
	srv    *httptest.Server
	engine *fakeEngine
	perc   *fakePerception
}

func newHarness(t *testing.T, env map[string]string, usable bool) *harness {
	t.Helper()
	e := newFakeEngine(t)
	p := newFakePerception(t, usable)
	m := map[string]string{"ENGINE_URL": e.srv.URL, "PERCEPTION_URL": p.srv.URL, "REPORT_BURST": "50", "REPORT_RATE_PER_MIN": "600"}
	for k, v := range env {
		m[k] = v
	}
	cfg, err := config.FromMap(m)
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	t.Cleanup(cancel)
	a, err := New(ctx, cfg, slog.New(slog.NewTextHandler(io.Discard, nil)), Options{Rainfall: noRain{}})
	if err != nil {
		t.Fatal(err)
	}
	a.Run(ctx)
	srv := httptest.NewServer(a.Handler())
	t.Cleanup(func() { a.Shutdown(); srv.Close() })
	return &harness{app: a, srv: srv, engine: e, perc: p}
}

func form(t *testing.T, fields map[string]string, files map[string][]byte) (*bytes.Buffer, string) {
	t.Helper()
	var b bytes.Buffer
	w := multipart.NewWriter(&b)
	for k, v := range fields {
		w.WriteField(k, v)
	}
	for k, v := range files {
		fw, _ := w.CreateFormFile(k, k+".bin")
		fw.Write(v)
	}
	w.Close()
	return &b, w.FormDataContentType()
}

func (h *harness) post(t *testing.T, path string, fields map[string]string, files map[string][]byte, accept string) *http.Response {
	t.Helper()
	body, ct := form(t, fields, files)
	req, _ := http.NewRequest(http.MethodPost, h.srv.URL+path, body)
	req.Header.Set("Content-Type", ct)
	if accept != "" {
		req.Header.Set("Accept", accept)
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { resp.Body.Close() })
	return resp
}

var loc = map[string]string{"lat": "12.9791", "lon": "80.2209"}

func photo(seed int) []byte { return testmedia.JPEG(testmedia.Scene(320, 240, seed), 85) }

func decode(t *testing.T, r *http.Response) map[string]any {
	t.Helper()
	var m map[string]any
	if err := json.NewDecoder(r.Body).Decode(&m); err != nil {
		t.Fatal(err)
	}
	return m
}

func TestReportHappyPath(t *testing.T) {
	h := newHarness(t, nil, true)
	resp := h.post(t, "/api/report", loc, map[string][]byte{"image": photo(1)}, "")
	if resp.StatusCode != 200 {
		t.Fatalf("status %d", resp.StatusCode)
	}
	out := decode(t, resp)
	if out["status"] != "fused" || out["cell"].(map[string]any)["cell"] != "8a618c4e2d47fff" {
		t.Fatalf("outcome %v", out)
	}
	h.engine.mu.Lock()
	defer h.engine.mu.Unlock()
	if len(h.engine.readings) != 1 {
		t.Fatalf("%d readings posted", len(h.engine.readings))
	}
	r := h.engine.readings[0]
	if r["depth_cm"] != 23.4 || r["source"] != "photo" || r["lat"] != 12.9791 {
		t.Fatalf("reading %v", r)
	}
	// No EXIF in the test photo, so trust is 0.9 from the freshness rule.
	if r["trust"] != 0.9 {
		t.Fatalf("trust %v", r["trust"])
	}
	if _, ok := r["velocity_mps"]; ok {
		t.Fatal("velocity sent without a measurement")
	}
}

func TestUploadLimits(t *testing.T) {
	h := newHarness(t, map[string]string{"MAX_IMAGE_BYTES": "20000", "MAX_VIDEO_BYTES": "200000", "MAX_VIDEO_SECONDS": "30"}, true)
	big := testmedia.JPEG(testmedia.Scene(1200, 900, 2), 95)
	cases := []struct {
		name   string
		fields map[string]string
		files  map[string][]byte
		status int
		code   string
	}{
		{"image too large", loc, map[string][]byte{"image": big}, 413, "too_large"},
		{"body over every limit", loc, map[string][]byte{"video": testmedia.MP4("isom", 5, 300000)}, 413, "too_large"},
		{"video too long", loc, map[string][]byte{"video": testmedia.MP4("isom", 45, 1000)}, 413, "too_long"},
		{"video without header", loc, map[string][]byte{"video": append(testmedia.MP4("isom", 0, 0)[:20], make([]byte, 50)...)}, 400, "bad_video"},
		{"html disguised as a photo", loc, map[string][]byte{"image": []byte("<html><script>x</script></html>")}, 415, "unsupported_media"},
		{"no location", nil, map[string][]byte{"image": photo(3)}, 400, "location_required"},
		{"outside Chennai", map[string]string{"lat": "28.61", "lon": "77.21"}, map[string][]byte{"image": photo(3)}, 400, "outside_area"},
		{"no file", loc, nil, 400, "media_required"},
		{"bad hint", map[string]string{"lat": "12.98", "lon": "80.22", "hint_reference": "boat"}, map[string][]byte{"image": photo(3)}, 400, "bad_hint"},
	}
	for _, c := range cases {
		resp := h.post(t, "/api/report", c.fields, c.files, "")
		if resp.StatusCode != c.status {
			t.Errorf("%s: status %d, want %d", c.name, resp.StatusCode, c.status)
			continue
		}
		if got := decode(t, resp)["code"]; got != c.code {
			t.Errorf("%s: code %v, want %s", c.name, got, c.code)
		}
	}
	if h.perc.calls.Load() != 0 {
		t.Fatalf("perception was called %d times for invalid uploads", h.perc.calls.Load())
	}
	// A short video within limits goes through.
	resp := h.post(t, "/api/report", loc, map[string][]byte{"video": testmedia.MP4("isom", 12, 5000)}, "")
	if resp.StatusCode != 200 {
		t.Fatalf("valid video: %d", resp.StatusCode)
	}
}

func TestDuplicatePhotosAreNotFusedTwice(t *testing.T) {
	h := newHarness(t, nil, true)
	img := testmedia.Scene(320, 240, 4)
	if r := h.post(t, "/api/report", loc, map[string][]byte{"image": testmedia.JPEG(img, 90)}, ""); r.StatusCode != 200 {
		t.Fatalf("first: %d", r.StatusCode)
	}
	r := h.post(t, "/api/report", loc, map[string][]byte{"image": testmedia.JPEG(img, 90)}, "")
	if r.StatusCode != 409 || decode(t, r)["code"] != "duplicate" {
		t.Fatalf("exact copy: %d", r.StatusCode)
	}
	r = h.post(t, "/api/report", loc, map[string][]byte{"image": testmedia.JPEG(img, 40)}, "")
	if r.StatusCode != 409 || decode(t, r)["code"] != "near_duplicate" {
		t.Fatalf("re-encoded copy: %d", r.StatusCode)
	}
	if r := h.post(t, "/api/report", loc, map[string][]byte{"image": photo(2)}, ""); r.StatusCode != 200 {
		t.Fatalf("different photo: %d", r.StatusCode)
	}
}

func TestUnusablePhotoIsNotPosted(t *testing.T) {
	h := newHarness(t, nil, false)
	out := decode(t, h.post(t, "/api/report", loc, map[string][]byte{"image": photo(5)}, ""))
	if out["status"] != "unusable" {
		t.Fatalf("got %v", out)
	}
	if len(h.engine.readings) != 0 {
		t.Fatal("an unusable measurement reached the engine")
	}
}

func TestReportRateLimit(t *testing.T) {
	h := newHarness(t, map[string]string{"REPORT_BURST": "2", "REPORT_RATE_PER_MIN": "1"}, true)
	for i := 0; i < 2; i++ {
		if r := h.post(t, "/api/report", loc, map[string][]byte{"image": photo(10 + i)}, ""); r.StatusCode != 200 {
			t.Fatalf("request %d: %d", i, r.StatusCode)
		}
	}
	r := h.post(t, "/api/report", loc, map[string][]byte{"image": photo(20)}, "")
	if r.StatusCode != http.StatusTooManyRequests {
		t.Fatalf("third request: %d", r.StatusCode)
	}
	if ra := r.Header.Get("Retry-After"); ra == "" || ra == "0" {
		t.Fatalf("Retry-After %q", ra)
	}
	// The form endpoint shares the bucket.
	if r := h.post(t, "/report", loc, map[string][]byte{"image": photo(21)}, "application/x-ndjson"); r.StatusCode != 429 {
		t.Fatalf("form: %d", r.StatusCode)
	}
	if h.app.Metrics.RateLimited.Value("report") != 2 {
		t.Fatalf("rate limit metric %d", h.app.Metrics.RateLimited.Value("report"))
	}
}

func TestFormStreamsRealStages(t *testing.T) {
	h := newHarness(t, nil, true)
	r := h.post(t, "/report", loc, map[string][]byte{"media": photo(30)}, "application/x-ndjson")
	if r.StatusCode != 200 || !strings.HasPrefix(r.Header.Get("Content-Type"), "application/x-ndjson") {
		t.Fatalf("status %d, type %s", r.StatusCode, r.Header.Get("Content-Type"))
	}
	var stages []string
	var last map[string]any
	sc := bufio.NewScanner(r.Body)
	sc.Buffer(nil, 1<<20)
	for sc.Scan() {
		var m map[string]any
		json.Unmarshal(sc.Bytes(), &m)
		stages = append(stages, m["stage"].(string))
		last = m
	}
	if got := strings.Join(stages, ","); got != "received,scene,verify,fuse,done" {
		t.Fatalf("stages %s", got)
	}
	html := last["html"].(string)
	for _, want := range []string{"23<small>cm</small>", "± 3.1 cm", "Two-wheeler wheel", "Trust 0.90", "data:image/png;base64,", "Advisory only"} {
		if !strings.Contains(html, want) {
			t.Errorf("result card lacks %q", want)
		}
	}
	// The page without script renders the same card.
	r = h.post(t, "/report", loc, map[string][]byte{"media": photo(31)}, "text/html")
	b, _ := io.ReadAll(r.Body)
	if r.StatusCode != 200 || !strings.Contains(string(b), "Added to the live map") {
		t.Fatalf("no-script form: %d", r.StatusCode)
	}
}

func TestPagesRender(t *testing.T) {
	h := newHarness(t, map[string]string{"DEMO": "1"}, true)
	h.app.Origins.Mark("8a618c4e2d47fff", true)
	pages := map[string][]string{
		"/":       {"Chennai flood depth, live", "Simulated scenario", "Report water here", "Advisory only", "maplibre-gl-5.6.0/maplibre-gl.js"},
		"/report": {"Measure the water from one photo", "Use my location", "12 MB", "30 seconds"},
		"/route":  {"Route around the water", "Velachery station", "Chennai Central", "Two-wheeler"},
		"/route?from=12.9791,80.2209&to=13.0067,80.2206&vehicle=car": {"Water-aware route for a Car", "4.2 km", "37 of 37", "Simulated scenario", `id="route-data"`},
		"/about": {"Muse Spark 1.3", "SAM 3.1", "Muse Voice Transcribe", "ODbL", "OpenFreeMap", "Limitations"},
	}
	for path, wants := range pages {
		resp, err := http.Get(h.srv.URL + path)
		if err != nil {
			t.Fatal(err)
		}
		b, _ := io.ReadAll(resp.Body)
		resp.Body.Close()
		body := string(b)
		if resp.StatusCode != 200 {
			t.Errorf("%s: %d", path, resp.StatusCode)
		}
		for _, w := range wants {
			if !strings.Contains(body, w) {
				t.Errorf("%s lacks %q", path, w)
			}
		}
		if strings.Contains(body, "ZgotmplZ") {
			t.Errorf("%s: html/template rejected a value", path)
		}
		if strings.ContainsAny(body, "—→←") {
			t.Errorf("%s contains an em dash or arrow", path)
		}
		csp := resp.Header.Get("Content-Security-Policy")
		i := strings.Index(csp, "'nonce-")
		if i < 0 {
			t.Fatalf("%s: no nonce in CSP %q", path, csp)
		}
		nonce := csp[i+7 : i+7+strings.Index(csp[i+7:], "'")]
		if strings.Count(body, "<script") != strings.Count(body, `nonce="`+nonce+`"`)+strings.Count(body, "<script src=")+strings.Count(body, `type="application/json"`) {
			t.Errorf("%s: an inline script lacks the nonce", path)
		}
	}
}

func TestCellsProxyCachesAndLabels(t *testing.T) {
	h := newHarness(t, nil, true)
	h.app.Origins.Mark("8a618c4e2d47fff", true)
	for i := 0; i < 3; i++ {
		resp, _ := http.Get(h.srv.URL + "/api/cells")
		var fc struct {
			Features []struct {
				Properties map[string]any `json:"properties"`
			} `json:"features"`
			Simulated bool `json:"simulated_scenario"`
		}
		json.NewDecoder(resp.Body).Decode(&fc)
		resp.Body.Close()
		if len(fc.Features) != 1 || fc.Features[0].Properties["origin"] != "simulated" || !fc.Simulated {
			t.Fatalf("cells %+v", fc)
		}
	}
	if n := h.engine.cellsCalls.Load(); n != 1 {
		t.Fatalf("engine queried %d times, want 1 (cached)", n)
	}
	resp, _ := http.Get(h.srv.URL + "/api/cells?bbox=1,2,3")
	if resp.StatusCode != 400 {
		t.Fatalf("bad bbox: %d", resp.StatusCode)
	}
}

func TestRouteProxy(t *testing.T) {
	h := newHarness(t, nil, true)
	resp, _ := http.Get(h.srv.URL + "/api/route?from=12.9791,80.2209&to=13.0067,80.2206&vehicle=walking")
	if resp.StatusCode != 200 {
		t.Fatalf("walking alias: %d", resp.StatusCode)
	}
	var f map[string]any
	json.NewDecoder(resp.Body).Decode(&f)
	if f["baseline"] == nil || f["properties"].(map[string]any)["advisory"] != true {
		t.Fatalf("route %v", f)
	}
	for _, q := range []string{"from=1,2&to=13.0067,80.2206&vehicle=car", "from=12.97,80.22&to=13.0,80.22&vehicle=boat", "to=13,80.2&vehicle=car"} {
		resp, _ := http.Get(h.srv.URL + "/api/route?" + q)
		if resp.StatusCode != 400 {
			t.Errorf("%s: %d", q, resp.StatusCode)
		}
	}
}

func TestSSEProxyFansOutLabelledCells(t *testing.T) {
	h := newHarness(t, map[string]string{"MAX_SSE_CLIENTS": "2"}, true)
	h.app.Origins.Mark("8a618c4e2d47fff", true)
	deadline := time.Now().Add(3 * time.Second)
	for !h.app.Hub.UpstreamConnected() && time.Now().Before(deadline) {
		time.Sleep(10 * time.Millisecond)
	}
	open := func() (*http.Response, *bufio.Reader) {
		resp, err := http.Get(h.srv.URL + "/api/stream")
		if err != nil {
			t.Fatal(err)
		}
		t.Cleanup(func() { resp.Body.Close() })
		return resp, bufio.NewReader(resp.Body)
	}
	r1, b1 := open()
	r2, b2 := open()
	if r1.StatusCode != 200 || r1.Header.Get("Content-Type") != "text/event-stream" || r2.StatusCode != 200 {
		t.Fatalf("stream status %d %d", r1.StatusCode, r2.StatusCode)
	}
	r3, _ := open()
	if r3.StatusCode != http.StatusServiceUnavailable || r3.Header.Get("Retry-After") == "" {
		t.Fatalf("third client: %d, want 503 with Retry-After", r3.StatusCode)
	}
	readEvent := func(b *bufio.Reader, name string) string {
		done := make(chan string, 1)
		go func() {
			var ev string
			for {
				line, err := b.ReadString('\n')
				if err != nil {
					done <- ""
					return
				}
				line = strings.TrimRight(line, "\n")
				if strings.HasPrefix(line, "event: ") {
					ev = strings.TrimPrefix(line, "event: ")
				}
				if strings.HasPrefix(line, "data: ") && ev == name {
					done <- strings.TrimPrefix(line, "data: ")
					return
				}
			}
		}()
		select {
		case d := <-done:
			return d
		case <-time.After(3 * time.Second):
			t.Fatalf("no %s event", name)
			return ""
		}
	}
	readEvent(b1, "hello")
	readEvent(b2, "hello")
	h.engine.events <- "event: cell\ndata: " + cellJSON + "\n\n"
	for _, b := range []*bufio.Reader{b1, b2} {
		var f map[string]any
		if err := json.Unmarshal([]byte(readEvent(b, "cell")), &f); err != nil {
			t.Fatal(err)
		}
		if f["properties"].(map[string]any)["origin"] != "simulated" {
			t.Fatalf("event not labelled: %v", f["properties"])
		}
	}
	// The engine reports a lag; browsers are told to refetch.
	h.engine.events <- ": lagged: 12 events dropped, refetch /v1/cells\n\n"
	if d := readEvent(b1, "resync"); !strings.Contains(d, "lagged") {
		t.Fatalf("resync %q", d)
	}
}

func TestReadyzAndMetrics(t *testing.T) {
	h := newHarness(t, nil, true)
	resp, _ := http.Get(h.srv.URL + "/readyz")
	if resp.StatusCode != 200 {
		t.Fatalf("readyz with both services up: %d", resp.StatusCode)
	}
	h.perc.srv.Close()
	resp, _ = http.Get(h.srv.URL + "/readyz")
	m := decode(t, resp)
	if resp.StatusCode != 503 || m["engine"] != "ready" || m["perception"] == "ready" {
		t.Fatalf("readyz with perception down: %d %v", resp.StatusCode, m)
	}
	http.Get(h.srv.URL + "/about")
	resp, _ = http.Get(h.srv.URL + "/metrics")
	b, _ := io.ReadAll(resp.Body)
	for _, want := range []string{
		`neermattam_http_requests_total{route="GET /about",method="GET",code="200"} 1`,
		"# TYPE neermattam_http_request_duration_seconds histogram",
		"neermattam_sse_clients 0",
	} {
		if !strings.Contains(string(b), want) {
			t.Errorf("metrics lack %q", want)
		}
	}
}

func TestWhatsAppDisabledWithoutSecrets(t *testing.T) {
	h := newHarness(t, nil, true)
	resp, _ := http.Post(h.srv.URL+"/webhooks/whatsapp", "application/json", strings.NewReader("{}"))
	if resp.StatusCode != 404 {
		t.Fatalf("got %d", resp.StatusCode)
	}
}

func TestWhatsAppWebhookRejectsBadSignature(t *testing.T) {
	h := newHarness(t, map[string]string{"WHATSAPP_VERIFY_TOKEN": "v", "WHATSAPP_APP_SECRET": "s", "WHATSAPP_TOKEN": "t"}, true)
	req, _ := http.NewRequest("POST", h.srv.URL+"/webhooks/whatsapp", strings.NewReader(`{"object":"x"}`))
	req.Header.Set("X-Hub-Signature-256", "sha256=00")
	resp, _ := http.DefaultClient.Do(req)
	if resp.StatusCode != 401 {
		t.Fatalf("got %d", resp.StatusCode)
	}
}

func TestStaticIsGzippedAndCached(t *testing.T) {
	h := newHarness(t, nil, true)
	req, _ := http.NewRequest("GET", h.srv.URL+"/static/vendor/maplibre-gl-5.6.0/maplibre-gl.js", nil)
	req.Header.Set("Accept-Encoding", "gzip")
	tr := &http.Transport{DisableCompression: true}
	resp, err := tr.RoundTrip(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	b, _ := io.ReadAll(resp.Body)
	if resp.Header.Get("Content-Encoding") != "gzip" || len(b) > 400_000 || !strings.Contains(resp.Header.Get("Cache-Control"), "immutable") {
		t.Fatalf("encoding %q, %d bytes, cache %q", resp.Header.Get("Content-Encoding"), len(b), resp.Header.Get("Cache-Control"))
	}
}
