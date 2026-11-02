package app

import (
	"html"
	"io"
	"regexp"
	"strings"
	"testing"

	"neermattam/gateway/internal/i18n"
)

// A sample is the same file every time, so perception would answer its
// scene reading from cache. The gateway asks for a fresh one, so "each tap
// is a fresh run" stays true; an ordinary upload does not.
func TestSampleAsksForAFreshSceneReading(t *testing.T) {
	h := newHarness(t, nil, true)
	if resp := h.post(t, "/api/report", map[string]string{"sample": "nandanam"}, nil, ""); resp.StatusCode != 200 {
		t.Fatalf("sample: status %d", resp.StatusCode)
	}
	if resp := h.post(t, "/api/report", loc, map[string][]byte{"image": photo(21)}, ""); resp.StatusCode != 200 {
		t.Fatalf("upload: status %d", resp.StatusCode)
	}
	h.perc.stream = true
	r := h.post(t, "/report", map[string]string{"sample": "chennai-bike"}, nil, "text/event-stream")
	io.ReadAll(r.Body)
	if got := h.perc.freshSeen(); strings.Join(got, ",") != "true,,true" {
		t.Fatalf("fresh fields %q, want the samples fresh and the upload not", got)
	}
}

// A sample's result card does not call a model estimate "measured", and
// the two stages a sample never enters are marked skipped with a reason
// instead of looking stalled.
func TestSampleResultWordingAndSkippedStages(t *testing.T) {
	h := newHarness(t, nil, true)
	h.perc.body = strings.Replace(h.perc.body, `"method":"wheel_geometry"`, `"method":"model_estimate"`, 1)
	for _, l := range i18n.Languages {
		r := h.post(t, "/report?lang="+l.Code, map[string]string{"sample": "chennai-bike"}, nil, "text/event-stream")
		names, data := sseEvents(t, r.Body)
		if names[len(names)-1] != "done" {
			t.Fatalf("%s: events %v", l.Code, names)
		}
		last := data[len(data)-1]
		card := html.UnescapeString(last["html"].(string))
		for _, want := range []string{i18n.T(l.Code, "result.sample_title"), i18n.T(l.Code, "result.model_estimate")} {
			if !strings.Contains(card, want) {
				t.Errorf("%s: card lacks %q", l.Code, want)
			}
		}
		skipped, _ := last["skipped"].(map[string]any)
		if skipped["verify"] != i18n.T(l.Code, "stage.skip_sample_verify") || skipped["fuse"] != i18n.T(l.Code, "stage.skip_sample_fuse") {
			t.Errorf("%s: skipped %v", l.Code, last["skipped"])
		}
	}
	r := h.post(t, "/report?lang=en", map[string]string{"sample": "chennai-bike"}, nil, "text/event-stream")
	b, _ := io.ReadAll(r.Body)
	if strings.Contains(string(b), "Measured live") || strings.Contains(string(b), "measured just now") {
		t.Error("a sample's model estimate is called measured")
	}
	// A fused upload skips nothing.
	r = h.post(t, "/report", loc, map[string][]byte{"image": photo(22)}, "text/event-stream")
	_, data := sseEvents(t, r.Body)
	if s, ok := data[len(data)-1]["skipped"]; ok {
		t.Errorf("a fused report skipped %v", s)
	}
}

// The replay switch is shared by the map and the route page; whichever
// page shows it must link a stylesheet that sizes its icon, or the icon
// fills the panel (the route page once showed a flask 260 px wide).
func TestReplaySwitchIconIsSizedOnEveryPageThatShowsIt(t *testing.T) {
	h := newHarness(t, map[string]string{"DEMO": "1"}, true)
	sheet := regexp.MustCompile(`<link rel="stylesheet" href="(/static/[^"]+\.css)">`)
	shown := 0
	for _, path := range []string{"/map?city=chennai", "/route?city=chennai", "/map?city=chennai&replay=michaung", "/route?city=chennai&replay=michaung", "/map?city=mumbai"} {
		_, body := get(t, nil, h.srv.URL+path)
		for _, cls := range []string{"nm-replay-link", "nm-replay-cta"} {
			if !strings.Contains(body, `class="`+cls+`"`) {
				continue
			}
			shown++
			sized := false
			for _, m := range sheet.FindAllStringSubmatch(body, -1) {
				_, css := get(t, nil, h.srv.URL+m[1])
				if regexp.MustCompile(`\.`+cls+`[^{]*\bsvg\s*\{[^}]*width:\s*1[0-9]px`).MatchString(css) ||
					(cls == "nm-replay-cta" && strings.Contains(css, ".btn svg { width: 18px")) {
					sized = true
				}
			}
			if !sized {
				t.Errorf("%s shows %s but no linked stylesheet sizes its icon", path, cls)
			}
		}
	}
	if shown < 2 {
		t.Fatalf("the replay switch was found on %d pages", shown)
	}
}

// The live dot and "live" belong to live data only: not on the simulated
// replay, and not on a city nobody has reported in, which offers the
// labelled replay instead.
func TestMapSaysLiveOnlyWhenItIs(t *testing.T) {
	h := newHarness(t, map[string]string{"DEMO": "1"}, true)
	_, body := get(t, nil, h.srv.URL+"/map?city=chennai&replay=michaung&lang=en")
	for _, bad := range []string{`id="live-dot"`, `id="feed-state"`, "flood depth, live"} {
		if strings.Contains(body, bad) {
			t.Errorf("the replay shows %q", bad)
		}
	}
	for _, want := range []string{"Replay readings", `<span class="chip chip-violet">Simulated</span>`} {
		if !strings.Contains(body, want) {
			t.Errorf("the replay lacks %q", want)
		}
	}
	_, body = get(t, nil, h.srv.URL+"/map?city=mumbai&lang=en")
	for _, bad := range []string{`id="live-dot"`, "flood depth, live"} {
		if strings.Contains(body, bad) {
			t.Errorf("an empty city shows %q", bad)
		}
	}
	for _, want := range []string{"Mumbai flood depth</h1>", "No reports yet in Mumbai.", `href="/map?city=chennai&amp;replay=michaung"`, "Replay a past flood</span>", "Simulated, and never mixed with real reports."} {
		if !strings.Contains(body, want) {
			t.Errorf("an empty city lacks %q", want)
		}
	}
}

// An archival photo's record holds a date; its verify page shows the real
// signing time from the certificate in IST, never midnight UTC as 05:30, and
// a valid signature on it is shown as verified, not as a warning.
func TestArchivalVerifyTimeAndStyle(t *testing.T) {
	h := newHarness(t, nil, true)
	h.perc.keys.Store(perceptionKeys)
	var id string
	for _, p := range h.app.Photos.List {
		if p.ID == "blr1" {
			id = p.MeasurementID()
		}
	}
	if id == "" {
		t.Fatal("no blr1 record")
	}
	_, body := get(t, nil, h.srv.URL+"/verify/"+id+"?lang=en")
	// Signed at 17:15:48Z on 1 November: 22:45 IST.
	if !strings.Contains(body, "22:45 IST, 1 Nov") || strings.Contains(body, "05:30") {
		t.Error("the archival photo's received time is not its signing time in IST")
	}
	if !strings.Contains(body, `class="card panel vfy-verdict v-verified"`) {
		t.Error("a verified archival photo is not in the verified style")
	}
	if strings.Contains(body, "not-used") || strings.Contains(body, `class="c-warn"`) {
		t.Error("a verified archival photo is shown as a warning")
	}
}

// Archival photos are named with their city, so Tatanagar is not read as
// the Jamshedpur station.
func TestArchivalPlacesNameTheirCity(t *testing.T) {
	h := newHarness(t, nil, true)
	_, body := get(t, nil, h.srv.URL+"/map?city=bengaluru&lang=en")
	if !strings.Contains(body, "<b>Tatanagar, Bengaluru</b>") {
		t.Error("the Bengaluru photos do not name the city")
	}
	_, body = get(t, nil, h.srv.URL+"/map?city=mumbai&lang=en")
	if strings.Contains(body, "Navi Mumbai, Mumbai") {
		t.Error("a place that names its city got the city twice")
	}
}

func TestRobotsAllowsEverything(t *testing.T) {
	h := newHarness(t, nil, true)
	r, body := get(t, nil, h.srv.URL+"/robots.txt")
	if r.StatusCode != 200 || body != "User-agent: *\nAllow: /\n" || !strings.HasPrefix(r.Header.Get("Content-Type"), "text/plain") {
		t.Fatalf("robots.txt %d %q %q", r.StatusCode, r.Header.Get("Content-Type"), body)
	}
}

// The About page says what is true today, in every language: India-wide,
// a wheel or a person, real data by default, no assumed recession, and the
// real-photo counts from both runs.
func TestAboutPageIsCurrent(t *testing.T) {
	h := newHarness(t, map[string]string{"DEMO": "1"}, true)
	_, body := get(t, nil, h.srv.URL+"/about?lang=en")
	text := html.UnescapeString(body)
	for _, want := range []string{"Forecasts say where India will flood.", "a wheel or a person standing in the water", "Water is never assumed to drain away",
		"Of the 33 real flood photos tested so far, 3 were measured from a person standing in the water and 3 gave a wheel reading, but only one of those wheel outlines followed the tyre",
		"The live map shows real reports only.", "tested on 36 written phrases in twelve languages"} {
		if !strings.Contains(text, want) {
			t.Errorf("About lacks %q", want)
		}
	}
	for _, stale := range []string{"Chennai will flood", "photo of a tyre", "ages old water", "assumed to recede", "runs with a simulated flood", "Of the 11 real"} {
		if strings.Contains(text, stale) {
			t.Errorf("About still says %q", stale)
		}
	}
	for _, l := range i18n.Languages {
		_, body := get(t, nil, h.srv.URL+"/about?lang="+l.Code)
		text := html.UnescapeString(body)
		for _, key := range []string{"hero.line1", "about.lead", "about.how3", "about.lim5", "about.sim_p"} {
			if !strings.Contains(text, i18n.T(l.Code, key)) {
				t.Errorf("%s About lacks %s", l.Code, key)
			}
		}
	}
}

// No visitor-facing English says "dry route" or calls an archival model
// estimate a measured photo.
func TestNoDryRouteOrMeasuredPhotoWording(t *testing.T) {
	for k, v := range i18n.Catalogs["en"] {
		low := strings.ToLower(v)
		if strings.Contains(low, "dry route") || strings.Contains(low, "measured photos") {
			t.Errorf("%s: %q", k, v)
		}
	}
}
