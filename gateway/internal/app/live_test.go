package app

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"sort"
	"strings"
	"testing"
	"time"

	"neermattam/gateway/internal/verify"
)

// fakeRain gives Chennai rainfall and fails for every other city.
type fakeRain struct{ at time.Time }

func (f fakeRain) Recent(_ context.Context, lat, _ float64, _ time.Duration) (verify.RecentRain, error) {
	if lat > 12.5 && lat < 13.5 {
		return verify.RecentRain{Last1hMM: 1.2, Last24hMM: 34.0, Hours: 24, ReadAt: f.at}, nil
	}
	return verify.RecentRain{}, errors.New("open-meteo returned 502")
}

func withRain(h *harness) {
	h.app.Rain = newCityRain(fakeRain{at: time.Now()}, h.app.Cities)
	h.app.Rain.Refresh(context.Background())
}

// The key the deployed perception signs with; the archival photos were
// measured with it.
const perceptionKeys = `{"keys":[{"key_id":"ed25519:5cadd6dd4ddca908","alg":"Ed25519","public_key":"qDIt4GySkBKhqcUkwvFmuh0AoEM5A41Fm5e9SNRG9MA"}]}`

func keysOf(m map[string]any) []string {
	var out []string
	for k := range m {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}

func TestIndiaFollowsTheContractWithRealDataOnly(t *testing.T) {
	h := newHarness(t, map[string]string{"DEMO": "1"}, true)
	withRain(h)
	if r := h.post(t, "/api/report", loc, map[string][]byte{"image": photo(61)}, ""); r.StatusCode != 200 {
		t.Fatalf("report: %d", r.StatusCode)
	}
	resp, err := http.Get(h.srv.URL + "/api/india")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 || !strings.HasPrefix(resp.Header.Get("Content-Type"), "application/json") {
		t.Fatalf("status %d, %s", resp.StatusCode, resp.Header.Get("Content-Type"))
	}
	var doc map[string]any
	if err := json.NewDecoder(resp.Body).Decode(&doc); err != nil {
		t.Fatal(err)
	}
	if got := strings.Join(keysOf(doc), ","); got != "cities,generated_at,measured_photos" {
		t.Fatalf("top-level keys %s", got)
	}
	if _, err := time.Parse(time.RFC3339, doc["generated_at"].(string)); err != nil {
		t.Fatalf("generated_at: %v", err)
	}
	cityKeys := "cells_with_evidence,critical_sites,deepest_cm,id,lat,lon,measured_photos,name,rain_at,rain_mm_1h,rain_mm_24h,rain_source,real_reports_24h"
	byID := map[string]map[string]any{}
	for _, x := range doc["cities"].([]any) {
		c := x.(map[string]any)
		if got := strings.Join(keysOf(c), ","); got != cityKeys {
			t.Fatalf("city keys %s", got)
		}
		byID[c["id"].(string)] = c
	}
	if len(byID) != 7 {
		t.Fatalf("%d cities", len(byID))
	}
	ch, mu := byID["chennai"], byID["mumbai"]
	if ch["name"] != "Chennai" || ch["rain_mm_1h"] != 1.2 || ch["rain_mm_24h"] != 34.0 || ch["rain_source"] != "Open-Meteo" {
		t.Fatalf("chennai %v", ch)
	}
	if _, err := time.Parse(time.RFC3339, ch["rain_at"].(string)); err != nil {
		t.Fatalf("rain_at: %v", err)
	}
	// Rain that could not be read is null, never zero.
	if mu["rain_mm_1h"] != nil || mu["rain_mm_24h"] != nil || mu["rain_at"] != nil || mu["rain_source"] != "Open-Meteo" {
		t.Fatalf("mumbai rain should be null: %v", mu)
	}
	if ch["real_reports_24h"] != 1.0 || mu["real_reports_24h"] != 0.0 {
		t.Fatalf("reports: chennai %v, mumbai %v", ch["real_reports_24h"], mu["real_reports_24h"])
	}
	if ch["cells_with_evidence"] != 1.0 || ch["deepest_cm"] != 42.0 || mu["cells_with_evidence"] != 0.0 || mu["deepest_cm"] != nil {
		t.Fatalf("cells: chennai %v %v, mumbai %v %v", ch["cells_with_evidence"], ch["deepest_cm"], mu["cells_with_evidence"], mu["deepest_cm"])
	}
	if ch["critical_sites"] != 862.0 || mu["critical_sites"] != nil {
		t.Fatalf("sites: chennai %v, mumbai %v", ch["critical_sites"], mu["critical_sites"])
	}
	photos := doc["measured_photos"].([]any)
	if len(photos) != len(h.app.Photos.List) || len(photos) == 0 {
		t.Fatalf("%d photos, %d embedded", len(photos), len(h.app.Photos.List))
	}
	perCity := map[string]float64{}
	photoKeys := "city,credit,depth_cm,id,method,photo_date,place,sigma_cm,source_url,thumb_url,verify_url"
	for _, x := range photos {
		p := x.(map[string]any)
		if got := strings.Join(keysOf(p), ","); got != photoKeys {
			t.Fatalf("photo keys %s", got)
		}
		id := p["id"].(string)
		if !strings.HasPrefix(id, "m_") || p["verify_url"] != "/verify/"+id || !strings.HasPrefix(p["thumb_url"].(string), "/static/photos/") {
			t.Fatalf("photo %v", p)
		}
		switch p["method"] {
		case "wheel_geometry", "person_reference", "model_estimate":
			if p["depth_cm"] == nil || p["sigma_cm"] == nil {
				t.Fatalf("a measured photo needs depth and sigma: %v", p)
			}
		case "not_measured":
		default:
			t.Fatalf("method %v", p["method"])
		}
		if !strings.Contains(p["credit"].(string), ", CC") || !strings.HasPrefix(p["source_url"].(string), "https://commons.wikimedia.org/") {
			t.Fatalf("credit %v", p)
		}
		if _, err := time.Parse("2006-01-02", p["photo_date"].(string)); err != nil {
			t.Fatalf("photo_date %v", p["photo_date"])
		}
		perCity[p["city"].(string)]++
	}
	for id, c := range byID {
		if c["measured_photos"] != perCity[id] {
			t.Fatalf("%s: measured_photos %v, list has %v", id, c["measured_photos"], perCity[id])
		}
	}
	// Nothing simulated reaches /api/india, and it never reads the replay.
	if body := string(mustGet(t, h.srv.URL+"/api/india")); strings.Contains(body, "simulated") || strings.Contains(body, "michaung") {
		t.Fatal("simulated data reached /api/india")
	}
	for _, l := range h.engine.sawLayers() {
		if strings.HasSuffix(l, ":replay") && strings.HasPrefix(l, "/v1/cells") {
			t.Fatalf("/api/india read the replay: %v", l)
		}
	}
}

func mustGet(t *testing.T, url string) []byte {
	t.Helper()
	_, body := get(t, nil, url)
	return []byte(body)
}

func TestIndiaLeavesSimulatedCellsOut(t *testing.T) {
	h := newHarness(t, map[string]string{"DEMO": "1"}, true)
	// A cell an older deployment filled with simulated water.
	h.app.Origins.Mark("8a618c4e2d47fff", true)
	doc := h.app.India(context.Background())
	for _, c := range doc.Cities {
		if c.ID != "chennai" {
			continue
		}
		if c.CellsWithEvidence == nil || *c.CellsWithEvidence != 0 || c.DeepestCm != nil {
			t.Fatalf("simulated water counted as real: %+v", c)
		}
		if c.RainMM24h != nil || c.RainAt != nil {
			t.Fatalf("rain without a source should be null: %+v", c)
		}
	}
	_, cells := get(t, nil, h.srv.URL+"/api/cells?city=chennai")
	if strings.Contains(cells, "8a618c4e2d47fff") {
		t.Fatal("the live map served a simulated cell")
	}
}

func TestReplayIsOptInAndKeptApart(t *testing.T) {
	h := newHarness(t, map[string]string{"DEMO": "1"}, true)
	_, live := get(t, nil, h.srv.URL+"/api/cells?city=chennai")
	if strings.Contains(live, `"simulated_scenario":true`) || strings.Contains(live, `"origin":"simulated"`) {
		t.Fatalf("the default cells are simulated: %s", live)
	}
	_, rep := get(t, nil, h.srv.URL+"/api/cells?city=chennai&replay=michaung")
	if !strings.Contains(rep, `"simulated_scenario":true`) || !strings.Contains(rep, `"origin":"simulated"`) {
		t.Fatalf("the replay is not labelled: %s", rep)
	}
	if r, _ := get(t, nil, h.srv.URL+"/api/cells?city=mumbai&replay=michaung"); r.StatusCode != 400 {
		t.Fatalf("replay outside Chennai: %d", r.StatusCode)
	}
	_, route := get(t, nil, h.srv.URL+"/api/route?from=12.9791,80.2209&to=13.0067,80.2206&vehicle=car")
	if strings.Contains(route, "simulated_scenario") {
		t.Fatal("a live route is labelled simulated")
	}
	_, route = get(t, nil, h.srv.URL+"/api/route?from=12.9791,80.2209&to=13.0067,80.2206&vehicle=car&replay=michaung")
	if !strings.Contains(route, `"simulated_scenario":true`) {
		t.Fatal("a replay route is not labelled simulated")
	}
	_, page := get(t, nil, h.srv.URL+"/route?city=chennai&replay=michaung")
	if !strings.Contains(page, `name="replay" value="michaung"`) || !strings.Contains(page, "Back to the live map") {
		t.Fatal("the route page should keep the replay on and offer the way back")
	}
	var live1, replay1 int
	for _, l := range h.engine.sawLayers() {
		switch {
		case strings.HasSuffix(l, ":replay"):
			replay1++
		case strings.HasSuffix(l, ":"):
			live1++
		}
	}
	if live1 == 0 || replay1 < 2 {
		t.Fatalf("live %d, replay %d engine calls: %v", live1, replay1, h.engine.sawLayers())
	}
	// Without DEMO there is no replay at all.
	h2 := newHarness(t, nil, true)
	if r, _ := get(t, nil, h2.srv.URL+"/api/cells?city=chennai&replay=michaung"); r.StatusCode != 400 {
		t.Fatalf("replay without DEMO: %d", r.StatusCode)
	}
	if _, body := get(t, nil, h2.srv.URL+"/map?city=chennai"); strings.Contains(body, "Replay a past flood") {
		t.Fatal("the replay link shows on a gateway without the replay")
	}
}

func TestMapIsNeverEmptyWithoutAnExplanation(t *testing.T) {
	h := newHarness(t, map[string]string{"DEMO": "1"}, true)
	withRain(h)
	_, body := get(t, nil, h.srv.URL+"/map?city=mumbai")
	for _, want := range []string{
		`id="st-max">–</b>`,            // a dash, not "None"
		"No reports yet in Mumbai.",    // the explanation
		"Real reports or rain now in:", // where to go instead
		`href="/map?city=chennai"`,     // one tap
		"Rain data unavailable.",       // Mumbai's rain failed: never zero
		"All cities: real reports and rain",
		`rel="preload" href="https://tiles.openfreemap.org/styles/positron"`,
	} {
		if !strings.Contains(body, want) {
			t.Errorf("Mumbai's map lacks %q", want)
		}
	}
	if strings.Contains(body, ">None<") {
		t.Error("the map says None")
	}
	// The panel comes before the inline cells, so it paints first.
	if i, j := strings.Index(body, "status-card"), strings.Index(body, `id="cells-data"`); i < 0 || (j >= 0 && j < i) {
		t.Error("the inline cells come before the panel")
	}
	_, body = get(t, nil, h.srv.URL+"/map?city=chennai")
	for _, want := range []string{"Rain in the last 24 hours: 34.0 mm (last hour 1.2 mm). Open-Meteo model, read at", `id="photos-data"`, "Measured photos (", "Photo from 2008, measured by Neermattam on", "/verify/m_"} {
		if !strings.Contains(body, want) {
			t.Errorf("Chennai's map lacks %q", want)
		}
	}
	// Tamil, for the new strings.
	_, body = get(t, nil, h.srv.URL+"/map?city=mumbai&lang=ta")
	if !strings.Contains(body, "மழைத் தரவு கிடைக்கவில்லை.") {
		t.Error("the Tamil map lacks the rain-unavailable line")
	}
}

func TestArchivalPhotoVerifiesAndIsNeverOnTheMap(t *testing.T) {
	h := newHarness(t, nil, true)
	h.perc.keys.Store(perceptionKeys)
	if len(h.app.Photos.List) == 0 {
		t.Fatal("no embedded photos")
	}
	p := h.app.Photos.List[0]
	r, body := get(t, nil, h.srv.URL+"/verify/"+p.MeasurementID())
	if r.StatusCode != 200 {
		t.Fatalf("verify page: %d", r.StatusCode)
	}
	for _, want := range []string{"Signature verified: an archival photo", "Archival photo, not on the live map", "Photo from " + p.Year() + ", measured by Neermattam on", "never added to the live map"} {
		if !strings.Contains(body, want) {
			t.Errorf("verify page lacks %q", want)
		}
	}
	resp, err := http.Get(h.srv.URL + "/api/certificates/" + p.MeasurementID())
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != 200 || resp.Header.Get("X-Neermattam-Verified") != "true" {
		t.Fatalf("certificate: %d verified=%q", resp.StatusCode, resp.Header.Get("X-Neermattam-Verified"))
	}
	// Measuring the photos posted nothing to the engine.
	h.engine.mu.Lock()
	n := len(h.engine.readings)
	h.engine.mu.Unlock()
	if n != 0 {
		t.Fatalf("%d readings reached the engine", n)
	}
	for _, ph := range h.app.Photos.List {
		if ph.FileSHA256 == "" || ph.Measurement.InputSHA256 == nil || *ph.Measurement.InputSHA256 != ph.FileSHA256 {
			t.Errorf("%s: the file hash does not match the certificate's input hash", ph.ID)
		}
	}
}

func TestOldRainIsNotShown(t *testing.T) {
	h := newHarness(t, nil, true)
	read := time.Now()
	c := newCityRain(fakeRain{at: read}, h.app.Cities)
	c.Refresh(context.Background())
	if r, ok := c.Get("chennai"); !ok || r.Last24hMM != 34.0 {
		t.Fatalf("fresh rain: %+v %v", r, ok)
	}
	if _, ok := c.Get("mumbai"); ok {
		t.Fatal("a failed read must show as unavailable")
	}
	c.now = func() time.Time { return read.Add(rainStale + time.Minute) }
	if _, ok := c.Get("chennai"); ok {
		t.Fatal("rain read more than 90 minutes ago must show as unavailable, never as current")
	}
}
