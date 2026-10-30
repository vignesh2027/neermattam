package app

import (
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"net/http/httputil"
	"net/url"
	"strings"
	"sync"
	"testing"
	"time"

	"neermattam/gateway/internal/apikeys"
)

const siteJSON = `{"id":"osm:way:1536001837","kind":"data_centre","name":"CtrlS Data Center Chennai","operator":"CtrlS","lat":13.107878,"lon":80.162611,"city":"chennai","cell":"8a618c4e2d47fff","source":"osm","osm":"way/1536001837","status":"over",
 "zones":[{"radius_m":300,"cells":21,"cells_reported":7,"cells_known":7,"coverage":0.33,"status":"over","deepest":{"cell":"8a618c4e2d47fff","depth_block_cm":46.8,"depth_cm":42,"depth_sigma_cm":3.2,"unknown":false,"age_h":0.2},"old_deep_cells":0,"old_deep_max_cm":null,"old_deep_age_h":null,"newest_evidence_age_h":0.2,"trend":"rising","trend_cells":{"rising":1,"falling":0,"steady":0}},
          {"radius_m":1000,"cells":211,"cells_reported":7,"cells_known":7,"coverage":0.03,"status":"over","deepest":{"cell":"8a618c4e2d47fff","depth_block_cm":46.8,"depth_cm":42,"depth_sigma_cm":3.2,"unknown":false,"age_h":0.2},"old_deep_cells":1,"old_deep_max_cm":38,"old_deep_age_h":13,"newest_evidence_age_h":0.2,"trend":null,"trend_cells":{"rising":0,"falling":0,"steady":0}}]}`

const siteRoutes = `"access":{"advisory":true,"computed_at":"2026-10-30T10:00:00Z","routes":[
 {"target":"main_road","vehicle":"truck","status":"open","reason":null,"destination":{"name":null,"lat":13.11,"lon":80.16},"distance_m":420,"baseline_distance_m":420,"detour_m":0,"eta_min":1.7,"cells_on_route":6,"unknown_cells_crossed":4,"impassable_cells_crossed":0,"max_known_depth_cm":22,"explanation":"Advisory only. Nothing reported blocks the shortest path for a diesel tanker or truck.","coordinates":[[80.1626,13.1078],[80.16,13.11]]},
 {"target":"main_road","vehicle":"two_wheeler","status":"open","reason":null,"destination":{"name":null,"lat":13.11,"lon":80.16},"distance_m":1210,"baseline_distance_m":420,"detour_m":790,"eta_min":3.6,"cells_on_route":14,"unknown_cells_crossed":14,"impassable_cells_crossed":0,"max_known_depth_cm":null,"explanation":"Advisory only. This route avoids 1 map cell.","coordinates":[[80.1626,13.1078],[80.16,13.11]]},
 {"target":"main_road","vehicle":"car","status":"no_safe_route","reason":null,"destination":{"name":null,"lat":13.11,"lon":80.16},"distance_m":420,"baseline_distance_m":420,"detour_m":0,"eta_min":1.3,"cells_on_route":6,"unknown_cells_crossed":4,"impassable_cells_crossed":2,"max_known_depth_cm":42,"explanation":"Advisory only. No route avoids water.","coordinates":[[80.1626,13.1078],[80.16,13.11]]},
 {"target":"fuel","vehicle":"truck","status":"unavailable","reason":"no_target_in_range","destination":null,"distance_m":null,"baseline_distance_m":null,"detour_m":null,"eta_min":null,"cells_on_route":null,"unknown_cells_crossed":null,"impassable_cells_crossed":null,"max_known_depth_cm":null,"explanation":null,"coordinates":[]}
]},"risk":{"area_resolution":8,"area":"88618c4e2dfffff","since":"2026-10-28T00:00:00Z","wet_threshold_cm":15,"areas":[{"area":"88618c4e2dfffff","lat":13.1,"lon":80.16,"boundary":[],"readings":9,"wet_readings":6,"days_with_water":2,"peak_cm":48,"peak_sigma_cm":3,"peak_at":"2026-10-29T08:00:00Z","episodes":1,"median_recession_h":9.5,"max_recession_h":9.5,"open_episodes":1,"unresolved_episodes":0,"first_at":"2026-10-28T06:00:00Z","last_at":"2026-10-30T09:00:00Z"}],"note":"This record holds only what people have reported."},"threshold_cm":15`

// siteEngine adds the engine's site endpoints in front of the usual fake.
type siteEngine struct {
	srv  *httptest.Server
	mu   sync.Mutex
	seen []string // method and request URI of every site call
	body []byte   // the last POST body
}

func (s *siteEngine) record(r *http.Request) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.seen = append(s.seen, r.Method+" "+r.URL.RequestURI())
	if r.Method == http.MethodPost {
		s.body, _ = io.ReadAll(r.Body)
	}
}

func (s *siteEngine) calls() []string {
	s.mu.Lock()
	defer s.mu.Unlock()
	return append([]string(nil), s.seen...)
}

func newSiteHarness(t *testing.T) (*harness, *siteEngine) {
	t.Helper()
	base := newFakeEngine(t)
	target, _ := url.Parse(base.srv.URL)
	proxy := httputil.NewSingleHostReverseProxy(target)
	proxy.FlushInterval = -1
	se := &siteEngine{}
	mux := http.NewServeMux()
	mux.HandleFunc("GET /v1/sites", func(w http.ResponseWriter, r *http.Request) {
		se.record(r)
		w.Write([]byte(`{"city":"chennai","kind":"data_centre","threshold_cm":15,"rings_m":[300,1000],"total":1,"count":1,"offset":0,
		 "counts":{"data_centre":{"osm":13,"operator":0},"hospital":{"osm":834,"operator":0}},"osm":{"attribution":"(c) OpenStreetMap contributors","data_until":"2026-10-25T21:21:21Z"},"advisory":true,"sites":[` + siteJSON + `]}`))
	})
	mux.HandleFunc("GET /v1/sites/{id}", func(w http.ResponseWriter, r *http.Request) {
		se.record(r)
		if r.PathValue("id") != "osm:way:1536001837" && !strings.HasPrefix(r.PathValue("id"), "op_") {
			http.Error(w, `{"error":"no site"}`, 404)
			return
		}
		w.Write([]byte(strings.TrimSuffix(siteJSON, "}") + "," + siteRoutes + "}"))
	})
	mux.HandleFunc("POST /v1/sites", func(w http.ResponseWriter, r *http.Request) {
		se.record(r)
		w.WriteHeader(201)
		w.Write([]byte(`{"id":"op_0123456789abcdef","kind":"data_centre","name":"Acme DC","lat":13.05,"lon":80.2,"city":"chennai","source":"operator","owner":"x"}`))
	})
	mux.HandleFunc("DELETE /v1/sites/{id}", func(w http.ResponseWriter, r *http.Request) {
		se.record(r)
		w.WriteHeader(204)
	})
	mux.HandleFunc("GET /v1/siting", func(w http.ResponseWriter, r *http.Request) {
		se.record(r)
		w.Write([]byte(`{"city":"chennai","bucket":"` + r.URL.Query().Get("bucket") + `","since":"2026-10-28T00:00:00Z","history_days":2.4,"wet_threshold_cm":15,"total":1,"note":"n","areas":[{"area":"88618c4e2dfffff","lat":13.1,"lon":80.16,"boundary":[],"readings":9,"wet_readings":6,"days_with_water":2,"peak_cm":48,"peak_sigma_cm":3,"episodes":1,"median_recession_h":9.5,"max_recession_h":9.5,"open_episodes":0,"unresolved_episodes":1}]}`))
	})
	mux.Handle("/", proxy)
	se.srv = httptest.NewServer(mux)
	t.Cleanup(se.srv.Close)
	h := newHarness(t, map[string]string{"ENGINE_URL": se.srv.URL}, true)
	return h, se
}

func TestSitesPageInEnglishAndTamil(t *testing.T) {
	h, se := newSiteHarness(t)
	waitCities(t, h)
	r, page := get(t, nil, h.srv.URL+"/sites?city=chennai&site=osm:way:1536001837")
	if r.StatusCode != 200 {
		t.Fatalf("status %d", r.StatusCode)
	}
	for _, want := range []string{
		`lang="en"`, "Critical sites in Chennai", "CtrlS Data Center Chennai", "Water over 15 cm within 300 m",
		"up to 47 cm (mean 42 ± 3 cm)", "7 of 21 cells with recent reports", "rising",
		"reports over the threshold in 1 cells have aged out (the deepest 38 cm, 13 h ago)", "Diesel tanker", "No safe route",
		"Open, 790 m detour", "None within range.", "Flood record for this area", "median 9.5 h; spells seen to end: 1",
		"OpenStreetMap lists 13", "Advisory only", "https://www.openstreetmap.org/way/1536001837", "/static/sites.",
		"No public map of the 2015 Chennai flood",
	} {
		if !strings.Contains(page, want) {
			t.Errorf("English page lacks %q", want)
		}
	}
	for _, bad := range []string{"—", "→", "safe route exists", "sites.", "%!"} {
		if bad == "sites." {
			// Every key resolved: no raw catalogue key is printed.
			if strings.Contains(page, ">sites.") || strings.Contains(page, " sites.") {
				t.Errorf("a raw key is printed")
			}
			continue
		}
		if strings.Contains(page, bad) {
			t.Errorf("page holds %q", bad)
		}
	}
	csp := r.Header.Get("Content-Security-Policy")
	nonce := csp[strings.Index(csp, "'nonce-")+7:]
	nonce = nonce[:strings.Index(nonce, "'")]
	if strings.Count(page, "<script") != strings.Count(page, `nonce="`+nonce+`"`)+strings.Count(page, "<script src=")+strings.Count(page, `type="application/json"`) {
		t.Error("an inline script lacks the nonce")
	}
	// The public page never asks for an operator's site.
	get(t, nil, h.srv.URL+"/sites?city=chennai&site=op_0123456789abcdef")
	for _, c := range se.calls() {
		if strings.Contains(c, "op_") || strings.Contains(c, "owner=") {
			t.Errorf("public page made an operator call: %s", c)
		}
	}

	_, ta := get(t, nil, h.srv.URL+"/sites?city=chennai&lang=ta&site=osm:way:1536001837")
	for _, want := range []string{`lang="ta"`, "சென்னை நகரின் முக்கியத் தளங்கள்", "டீசல் டேங்கர்", "பாதுகாப்பான வழி இல்லை", "ஆலோசனை மட்டுமே", "CtrlS Data Center Chennai"} {
		if !strings.Contains(ta, want) {
			t.Errorf("Tamil page lacks %q", want)
		}
	}
	if strings.Contains(ta, "Diesel tanker") || strings.Contains(ta, "No safe route") {
		t.Error("Tamil page has English route labels")
	}
}

func TestSitesDeveloperAPINeedsAKeyAndScopesItToTheOwner(t *testing.T) {
	h, se := newSiteHarness(t)
	dir := h.app.Cfg.DataDir
	secret, k, _ := apikeys.New("Acme DC Ops", 600, time.Now())
	if err := apikeys.WriteKeys(dir, []*apikeys.Key{k}); err != nil {
		t.Fatal(err)
	}
	list := h.srv.URL + "/api/v1/sites?city=chennai&owner=someone_else"
	if r, b := get(t, nil, list); r.StatusCode != 401 || !strings.Contains(b, "missing_key") {
		t.Fatalf("no key: %d %s", r.StatusCode, b)
	}
	if r, b := get(t, nil, list, "Authorization", "Bearer nm_live_"+strings.Repeat("b", 32)); r.StatusCode != 401 || !strings.Contains(b, "invalid_key") {
		t.Fatalf("bad key: %d %s", r.StatusCode, b)
	}
	if len(se.calls()) != 0 {
		t.Fatalf("the engine was asked without a key: %v", se.calls())
	}
	time.Sleep(2100 * time.Millisecond) // the key file is re-read within 2 s
	r, b := get(t, nil, list, "Authorization", "Bearer "+secret)
	if r.StatusCode != 200 || !strings.HasPrefix(b, `{"advisory":true,"terms":"`) {
		t.Fatalf("list: %d %.120s", r.StatusCode, b)
	}
	calls := se.calls()
	if len(calls) != 1 || !strings.Contains(calls[0], "owner="+k.ID) || strings.Contains(calls[0], "someone_else") {
		t.Fatalf("the owner must be the key, never the query: %v", calls)
	}
	req, _ := http.NewRequest("POST", h.srv.URL+"/api/v1/sites", strings.NewReader(`{"kind":"data_centre","name":"Acme DC","lat":13.05,"lon":80.2,"owner":"forged"}`))
	req.Header.Set("X-API-Key", secret)
	resp, err := http.DefaultClient.Do(req)
	if err != nil || resp.StatusCode != 201 {
		t.Fatalf("add: %v %v", err, resp)
	}
	resp.Body.Close()
	var sent map[string]any
	json.Unmarshal(se.body, &sent)
	if sent["owner"] != k.ID {
		t.Fatalf("the engine got owner %v, want the key %s", sent["owner"], k.ID)
	}
	req, _ = http.NewRequest("DELETE", h.srv.URL+"/api/v1/sites/op_0123456789abcdef", nil)
	req.Header.Set("Authorization", "Bearer "+secret)
	if resp, err := http.DefaultClient.Do(req); err != nil || resp.StatusCode != 204 {
		t.Fatalf("delete: %v %v", err, resp)
	}
	if c := se.calls(); !strings.Contains(c[len(c)-1], "DELETE /v1/sites/op_0123456789abcdef?owner="+k.ID) {
		t.Fatalf("delete call %v", c)
	}
	if r, b := get(t, nil, h.srv.URL+"/api/v1/siting?city=chennai&bucket=unattributed", "X-API-Key", secret); r.StatusCode != 200 || !strings.Contains(b, `"bucket":"reported"`) {
		t.Fatalf("siting must be real reports only: %d %.200s", r.StatusCode, b)
	}
	if r, _ := get(t, nil, h.srv.URL+"/api/v1/sites?city=atlantis", "X-API-Key", secret); r.StatusCode != 400 {
		t.Fatalf("unknown city: %d", r.StatusCode)
	}
	_, u := get(t, nil, h.srv.URL+"/api/v1/usage", "X-API-Key", secret)
	var usage struct {
		Total apikeys.Counts `json:"total"`
	}
	json.Unmarshal([]byte(u), &usage)
	if usage.Total.Sites != 2 {
		t.Fatalf("site reads counted %d, want 2: %s", usage.Total.Sites, u)
	}
	// The public site JSON refuses operator ids.
	if r, _ := get(t, nil, h.srv.URL+"/api/sites/op_0123456789abcdef"); r.StatusCode != 404 {
		t.Fatalf("public operator site: %d", r.StatusCode)
	}
}
