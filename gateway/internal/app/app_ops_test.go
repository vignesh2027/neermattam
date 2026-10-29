package app

import (
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
	"testing"
	"time"

	"neermattam/gateway/internal/apikeys"
	"neermattam/gateway/internal/i18n"
)

// addAnalytics gives the fake engine the control-room endpoints.
func addAnalytics(mux *http.ServeMux) {
	mux.HandleFunc("GET /v1/cities", func(w http.ResponseWriter, _ *http.Request) {
		var list []string
		for _, c := range [][2]string{{"chennai", "ready"}, {"mumbai", "ready"}, {"bengaluru", "ready"}, {"hyderabad", "ready"}, {"kolkata", "failed"}, {"delhi", "loading"}} {
			list = append(list, fmt.Sprintf(`{"id":%q,"name":%q,"cells":0,"graph":{"status":%q}}`, c[0], strings.ToUpper(c[0][:1])+c[0][1:], c[1]))
		}
		// Boxes are left out here; the gateway keeps its built-in ones only
		// when the engine sends none, so send the real ones.
		boxes := map[string]string{"chennai": "80.10,12.85,80.33,13.24", "mumbai": "72.77,18.88,73.13,19.32", "bengaluru": "77.44,12.82,77.80,13.15", "hyderabad": "78.24,17.22,78.66,17.62", "kolkata": "88.20,22.42,88.52,22.72", "delhi": "76.84,28.40,77.35,28.89"}
		for i, l := range list {
			for id, b := range boxes {
				if strings.Contains(l, `"id":"`+id+`"`) {
					list[i] = strings.Replace(l, `"cells"`, `"bbox":[`+b+`],"centre":{"lat":0,"lon":0},"cells"`, 1)
				}
			}
		}
		fmt.Fprintf(w, `{"cities":[%s]}`, strings.Join(list, ","))
	})
	mux.HandleFunc("GET /v1/summary", func(w http.ResponseWriter, r *http.Request) {
		fmt.Fprintf(w, `{"city":%q,"cells":12,"by_class":{"low":3,"danger_some":4,"danger_most":2,"danger_all":1,"unknown":2},"unknown":2,"stale":1,"expired":1,"cells_in_conflict":1,"conflicting_readings":3,"blocked":{"pedestrian":7,"two_wheeler":8,"auto":8,"car":8,"bus":3},"needs_fresh_report":1,"needs_fresh_report_cells":[{"cell":"8a618c4e2d47fff","lat":12.99,"lon":80.217,"last_depth_cm":42,"last_reading_age_h":7.5,"expired":false}],"readings":40,"newest_reading_at":"2026-10-29T09:00:00Z","generated_at":"2026-10-29T09:05:00Z"}`, r.URL.Query().Get("city"))
	})
	mux.HandleFunc("GET /v1/hotspots", func(w http.ResponseWriter, r *http.Request) {
		w.Write([]byte(`{"city":"chennai","advisory":true,"score":"depth 40 x ...","count":1,"hotspots":[{"cell":"8a618c4e2d47fff","lat":12.99,"lon":80.217,"score":77.2,"parts":{"depth":35.2,"hazard":20,"freshness":20,"reports":2},"depth_block_cm":52.8,"depth_cm":48,"depth_sigma_cm":3.2,"hazard_class_upper":"danger_most","age_h":0,"independent_reports":1,"unknown":false,"blocks":["pedestrian","two_wheeler","auto","car","bus"],"reasons":["Water may be as deep as 53 cm here (mean 48 cm plus the safety margin): 35.2 of 40 points."]}]}`))
	})
	mux.HandleFunc("GET /v1/recession", func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Query().Get("cell") != "8a618c4e2d47fff" {
			http.Error(w, `{"error":"no reports have touched cell"}`, 404)
			return
		}
		fmt.Fprintf(w, `{"history":[{"at":"2026-10-29T06:00:00Z","reading_cm":50,"reading_sigma_cm":3,"fused_cm":50,"fused_sigma_cm":3},{"at":"2026-10-29T07:00:00Z","reading_cm":40,"reading_sigma_cm":3,"fused_cm":41,"fused_sigma_cm":2.5},{"at":"2026-10-29T08:00:00Z","reading_cm":30,"reading_sigma_cm":3,"fused_cm":31,"fused_sigma_cm":2.2}],"cell":"8a618c4e2d47fff","lat":12.99118,"lon":80.2176,"current":{"cell":"8a618c4e2d47fff","depth_cm":31,"depth_sigma_cm":2.2,"hazard_class":"danger_some","conflicts":1,"updated_at":"2026-10-29T08:00:00Z"},"drain":{"rate_cm_per_h":-10,"rate_sigma_cm_per_h":1.41,"trend":"falling","points_used":3,"from":"2026-10-29T06:00:00Z","to":"2026-10-29T08:00:00Z","hours_to_15_cm":1.6},"drain_note":"A weighted straight-line fit.","advisory":true}`)
	})
	mux.HandleFunc("GET /v1/export", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/csv; charset=utf-8")
		w.Header().Set("Content-Disposition", `attachment; filename="neermattam-`+r.URL.Query().Get("city")+`.csv"`)
		w.Header().Set("X-Neermattam-Truncated", "false")
		w.Header().Set("X-Neermattam-Total-Cells", "1")
		w.Write([]byte("cell,city\n8a618c4e2d47fff,chennai\n"))
	})
}

func get(t *testing.T, client *http.Client, u string, hdr ...string) (*http.Response, string) {
	t.Helper()
	req, _ := http.NewRequest("GET", u, nil)
	for i := 0; i+1 < len(hdr); i += 2 {
		req.Header.Set(hdr[i], hdr[i+1])
	}
	if client == nil {
		client = http.DefaultClient
	}
	resp, err := client.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	b, _ := io.ReadAll(resp.Body)
	resp.Body.Close()
	return resp, string(b)
}

func waitCities(t *testing.T, h *harness) {
	t.Helper()
	for deadline := time.Now().Add(3 * time.Second); time.Now().Before(deadline); time.Sleep(10 * time.Millisecond) {
		if !h.app.Cities.Fetched().IsZero() {
			return
		}
	}
	t.Fatal("the city list never came from the engine")
}

func TestCitySwitcherScopesEveryPage(t *testing.T) {
	h := newHarness(t, map[string]string{"DEMO": "1"}, true)
	waitCities(t, h)
	resp, body := get(t, nil, h.srv.URL+"/map?city=mumbai")
	if resp.StatusCode != 200 || !strings.Contains(body, "No reports yet in Mumbai.") || strings.Contains(body, `class="sim-banner"`) {
		t.Fatalf("Mumbai map: %d, empty note or a simulated banner wrong", resp.StatusCode)
	}
	if !strings.Contains(resp.Header.Get("Set-Cookie"), "nm_city=mumbai") {
		t.Fatal("?city= is not remembered")
	}
	for _, c := range []string{"chennai", "mumbai", "bengaluru", "hyderabad", "kolkata", "delhi"} {
		if !strings.Contains(body, "city="+c) {
			t.Errorf("the switcher lacks %s", c)
		}
	}
	// The cookie carries the choice to the next page, which routes in Mumbai.
	req, _ := http.NewRequest("GET", h.srv.URL+"/route", nil)
	req.AddCookie(&http.Cookie{Name: CityCookie, Value: "mumbai"})
	r2, _ := http.DefaultClient.Do(req)
	b2, _ := io.ReadAll(r2.Body)
	r2.Body.Close()
	if !strings.Contains(string(b2), "Dadar station") || strings.Contains(string(b2), "Velachery station") {
		t.Fatal("the route page should offer Mumbai's presets")
	}
	// Chennai keeps the labelled simulated scenario.
	_, body = get(t, nil, h.srv.URL+"/map?city=chennai")
	if !strings.Contains(body, `class="sim-banner"`) {
		t.Fatal("Chennai should show the simulated scenario banner")
	}
	// ?at= picks the city holding the point.
	_, body = get(t, nil, h.srv.URL+"/map?at=28.6315,77.2167")
	if !strings.Contains(body, `"id":"delhi"`) {
		t.Fatal("a point in Delhi should open Delhi's map")
	}
	// The API scopes cells and says which city holds the scenario.
	_, cells := get(t, nil, h.srv.URL+"/api/cells?city=mumbai")
	var fc struct {
		City      string `json:"city"`
		Simulated bool   `json:"simulated_scenario"`
		Features  []any  `json:"features"`
	}
	json.Unmarshal([]byte(cells), &fc)
	if fc.City != "mumbai" || fc.Simulated || len(fc.Features) != 0 {
		t.Fatalf("Mumbai cells %+v", fc)
	}
	if r, _ := get(t, nil, h.srv.URL+"/api/cells?city=paris"); r.StatusCode != 400 {
		t.Fatalf("unknown city: %d", r.StatusCode)
	}
	_, list := get(t, nil, h.srv.URL+"/api/cities")
	if !strings.Contains(list, `"routing":"failed"`) || !strings.Contains(list, `"default":"chennai"`) {
		t.Fatalf("cities %s", list)
	}
	// The landing preview follows the city.
	_, body = get(t, nil, h.srv.URL+"/?city=hyderabad")
	if !strings.Contains(body, "No reports yet in Hyderabad.") || !strings.Contains(body, "Secunderabad station") {
		t.Fatal("the landing preview should show Hyderabad, empty, with its places")
	}
}

func TestRouteErrorsAreInPlainWords(t *testing.T) {
	h := newHarness(t, nil, true)
	waitCities(t, h)
	for _, c := range []struct {
		q, code, words string
		status         int
	}{
		{"from=19.0186,72.8424&to=22.5839,88.3426&vehicle=car", "different_cities", "different cities", 400},
		{"from=15.3,75.1&to=19.1197,72.8464&vehicle=car", "outside_area", "outside every city", 400},
		{"from=19.0186,72.8424&to=19.1197,72.8464&vehicle=car&city=chennai", "outside_area", "outside Chennai", 400},
		{"from=28.6427,77.2197&to=28.6315,77.2167&vehicle=car", "routing_unavailable", "Routes in Delhi are not available yet", 503},
		{"from=12.9791,80.2209&to=13.0418&vehicle=car", "bad_point", "lat,lon", 400},
	} {
		resp, body := get(t, nil, h.srv.URL+"/api/route?"+c.q)
		var e map[string]string
		json.Unmarshal([]byte(body), &e)
		if resp.StatusCode != c.status || e["code"] != c.code || !strings.Contains(e["error"], c.words) {
			t.Errorf("%s: %d %v", c.q, resp.StatusCode, e)
		}
	}
	// The route page says it in the page's language.
	_, body := get(t, nil, h.srv.URL+"/route?city=mumbai&from=19.0186,72.8424&to=22.5839,88.3426&vehicle=car&lang=ta")
	if !strings.Contains(body, i18n.T("ta", "err.route_different_cities")) {
		t.Fatal("the Tamil route page should explain the error in Tamil")
	}
}

func TestControlRoomNeedsItsKeyAndTheDemoIsLabelled(t *testing.T) {
	const key = "ward-officer-key-for-tests"
	h := newHarness(t, map[string]string{"DEMO": "1", "CONTROL_ROOM_KEY": key}, true)
	noRedirect := &http.Client{CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}
	_, body := get(t, nil, h.srv.URL+"/control")
	if !strings.Contains(body, `name="key"`) || !strings.Contains(body, "/control/demo") {
		t.Fatal("without a session /control shows the key page and the demo link")
	}
	for _, p := range []string{"/control/panel?city=chennai", "/control/export?city=chennai"} {
		if r, _ := get(t, nil, h.srv.URL+p); r.StatusCode != 401 {
			t.Errorf("%s without a session: %d", p, r.StatusCode)
		}
	}
	resp, _ := noRedirect.PostForm(h.srv.URL+"/control/login", url.Values{"key": {"wrong"}})
	if resp.StatusCode != 401 {
		t.Fatalf("wrong key: %d", resp.StatusCode)
	}
	resp, _ = noRedirect.PostForm(h.srv.URL+"/control/login", url.Values{"key": {key}})
	var session *http.Cookie
	for _, c := range resp.Cookies() {
		if c.Name == ControlCookie {
			session = c
		}
	}
	if resp.StatusCode != 303 || session == nil || !session.HttpOnly || session.SameSite != http.SameSiteStrictMode || strings.Contains(session.Value, key) {
		t.Fatalf("login: %d %+v", resp.StatusCode, session)
	}
	withSession := func(p string) (*http.Response, string) {
		req, _ := http.NewRequest("GET", h.srv.URL+p, nil)
		req.AddCookie(session)
		r, err := http.DefaultClient.Do(req)
		if err != nil {
			t.Fatal(err)
		}
		b, _ := io.ReadAll(r.Body)
		r.Body.Close()
		return r, string(b)
	}
	r, page := withSession("/control?city=chennai&cell=8a618c4e2d47fff")
	for _, want := range []string{"Hotspots", "Needs a fresh report", "Cells closed to", "Up to 53 cm", `class="spark"`, "Falling", "about 1.6 h to 15 cm", "Export CSV", "Routing by city", "31 ± 2 cm", "Danger for some", "1 conflicting readings", "at=12.99118%2C80.2176"} {
		if !strings.Contains(page, want) {
			t.Errorf("control room lacks %q", want)
		}
	}
	if r.StatusCode != 200 || strings.Contains(page, "ZgotmplZ") {
		t.Fatalf("control room: %d", r.StatusCode)
	}
	_, panel := withSession("/control/panel?city=chennai")
	var pj map[string]string
	if json.Unmarshal([]byte(panel), &pj) != nil || !strings.Contains(pj["html"], "hot-list") {
		t.Fatalf("panel %s", panel)
	}
	r, csv := withSession("/control/export?city=chennai&format=csv")
	if r.Header.Get("X-Neermattam-Total-Cells") != "1" || !strings.Contains(r.Header.Get("Content-Disposition"), "attachment") || !strings.HasPrefix(csv, "cell,city") {
		t.Fatalf("export: %v %q", r.Header, csv)
	}
	// The public demo: no key, Chennai only, labelled.
	r, demo := get(t, nil, h.srv.URL+"/control/demo?city=mumbai")
	if r.StatusCode != 200 || !strings.Contains(demo, "Public demo with simulated data.") || !strings.Contains(demo, "Chennai, flood response") {
		t.Fatalf("demo: %d", r.StatusCode)
	}
	// Other languages build the reasons themselves.
	_, ta := withSession("/control?city=chennai&lang=ta")
	if strings.Contains(ta, "Water may be as deep as") {
		t.Fatal("the Tamil control room should not show the engine's English reasons")
	}
}

func TestDemoControlRoomNeedsTheScenario(t *testing.T) {
	h := newHarness(t, nil, true)
	if r, _ := get(t, nil, h.srv.URL+"/control/demo"); r.StatusCode != 404 {
		t.Fatalf("demo without DEMO=1: %d", r.StatusCode)
	}
	_, body := get(t, nil, h.srv.URL+"/control")
	if !strings.Contains(body, "no control room key set") {
		t.Fatal("without CONTROL_ROOM_KEY the page should say the room is off")
	}
}

func TestDeveloperAPIKeysLimitsAndUsage(t *testing.T) {
	h := newHarness(t, nil, true)
	dir := h.app.Cfg.DataDir
	secret, k, _ := apikeys.New("Acme Fleet", 6, time.Now())
	off, k2, _ := apikeys.New("Old trial", 60, time.Now())
	k2.Disabled = true
	if err := apikeys.WriteKeys(dir, []*apikeys.Key{k, k2}); err != nil {
		t.Fatal(err)
	}
	route := h.srv.URL + "/api/v1/route?from=12.9791,80.2209&to=13.0067,80.2206&vehicle=car"
	if r, b := get(t, nil, route); r.StatusCode != 401 || !strings.Contains(b, "missing_key") {
		t.Fatalf("no key: %d %s", r.StatusCode, b)
	}
	if r, b := get(t, nil, route, "Authorization", "Bearer nm_live_"+strings.Repeat("a", 32)); r.StatusCode != 401 || !strings.Contains(b, "invalid_key") {
		t.Fatalf("bad key: %d %s", r.StatusCode, b)
	}
	// The key file was written after the store loaded: it is picked up.
	time.Sleep(2100 * time.Millisecond)
	if r, b := get(t, nil, route, "Authorization", "Bearer "+off); r.StatusCode != 403 || !strings.Contains(b, "key_disabled") {
		t.Fatalf("disabled key: %d %s", r.StatusCode, b)
	}
	r, b := get(t, nil, route, "Authorization", "Bearer "+secret)
	if r.StatusCode != 200 || r.Header.Get("X-RateLimit-Limit") != "6" || !strings.HasPrefix(b, `{"advisory":true,"terms":"`) {
		t.Fatalf("route: %d %v %.80s", r.StatusCode, r.Header, b)
	}
	r, b = get(t, nil, h.srv.URL+"/api/v1/cells?city=chennai", "X-API-Key", secret)
	if r.StatusCode != 200 || !strings.Contains(b, `"advisory":true`) || !strings.Contains(b, "8a618c4e2d47fff") {
		t.Fatalf("cells: %d %.80s", r.StatusCode, b)
	}
	if r2, _ := get(t, nil, h.srv.URL+"/api/v1/cells?city=chennai", "X-API-Key", secret, "If-None-Match", r.Header.Get("ETag")); r2.StatusCode != 304 {
		t.Fatalf("cells revalidation: %d", r2.StatusCode)
	}
	// A failed route is not counted.
	get(t, nil, h.srv.URL+"/api/v1/route?from=1,1&to=2,2&vehicle=car", "Authorization", "Bearer "+secret)
	_, u := get(t, nil, h.srv.URL+"/api/v1/usage", "Authorization", "Bearer "+secret)
	var usage struct {
		Key   string         `json:"key"`
		Total apikeys.Counts `json:"total"`
		Days  []apikeys.DayCount
	}
	json.Unmarshal([]byte(u), &usage)
	if usage.Key != k.ID || usage.Total.Routes != 1 || usage.Total.Cells != 2 || len(usage.Days) != 1 {
		t.Fatalf("usage %s", u)
	}
	// Six a minute with a burst of five: the next few are refused.
	refused := 0
	for i := 0; i < 4; i++ {
		if r, _ := get(t, nil, h.srv.URL+"/api/v1/usage", "Authorization", "Bearer "+secret); r.StatusCode == 429 && r.Header.Get("Retry-After") != "" {
			refused++
		}
	}
	if refused == 0 {
		t.Fatal("the per-key limit never refused")
	}
	// Usage survives in its own file.
	h.app.saveKeyUsage()
	saved, _ := apikeys.ReadUsage(dir)
	if saved[k.ID] == nil || saved[k.ID].Total.Routes != 1 {
		t.Fatalf("saved usage %+v", saved)
	}
	_, page := get(t, nil, h.srv.URL+"/developers")
	for _, want := range []string{"/api/v1/route", "Authorization: Bearer", "Advisory terms", "X-RateLimit-Limit", "rate_limited"} {
		if !strings.Contains(page, want) {
			t.Errorf("developers page lacks %q", want)
		}
	}
}

func TestAlertsAPI(t *testing.T) {
	h := newHarness(t, nil, true)
	waitCities(t, h)
	_, kj := get(t, nil, h.srv.URL+"/api/alerts/key")
	if !strings.Contains(kj, "vapid_public_key") {
		t.Fatalf("key %s", kj)
	}
	post := func(body string) (*http.Response, map[string]any) {
		resp, err := http.Post(h.srv.URL+"/api/alerts", "application/json", strings.NewReader(body))
		if err != nil {
			t.Fatal(err)
		}
		var m map[string]any
		json.NewDecoder(resp.Body).Decode(&m)
		resp.Body.Close()
		return resp, m
	}
	keys := `"keys":{"p256dh":"BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4","auth":"BTBZMqHH6r4Tts7J_aSIgg"}`
	if r, m := post(`{"subscription":{"endpoint":"https://evil.example/x",` + keys + `},"city":"chennai","vehicle":"car","point":{"lat":12.98,"lon":80.22}}`); r.StatusCode != 400 || m["code"] != "bad_endpoint" {
		t.Fatalf("foreign endpoint: %d %v", r.StatusCode, m)
	}
	if r, m := post(`{"subscription":{"endpoint":"https://fcm.googleapis.com/fcm/send/abc",` + keys + `},"city":"chennai","vehicle":"car","point":{"lat":19.0,"lon":72.85}}`); r.StatusCode != 400 || m["code"] != "bad_watch" {
		t.Fatalf("point outside the city: %d %v", r.StatusCode, m)
	}
	r, m := post(`{"subscription":{"endpoint":"https://fcm.googleapis.com/fcm/send/abc",` + keys + `},"city":"chennai","vehicle":"car","lang":"ta","point":{"lat":12.9912,"lon":80.2176}}`)
	if r.StatusCode != 201 || m["token"] == nil || m["status"] != "over" {
		t.Fatalf("create: %d %v", r.StatusCode, m)
	}
	id, token := m["id"].(string), m["token"].(string)
	if g, _ := get(t, nil, h.srv.URL+"/api/alerts/"+id, "X-Alert-Token", "nope"); g.StatusCode != 404 {
		t.Fatalf("wrong token: %d", g.StatusCode)
	}
	g, body := get(t, nil, h.srv.URL+"/api/alerts/"+id, "X-Alert-Token", token)
	if g.StatusCode != 200 || !strings.Contains(body, `"status":"over"`) {
		t.Fatalf("get %d %s", g.StatusCode, body)
	}
	// A route is planned on the server and watched along its length.
	r, m = post(`{"subscription":{"endpoint":"https://fcm.googleapis.com/fcm/send/def",` + keys + `},"city":"chennai","vehicle":"car","route":{"from":"12.9791,80.2209","to":"13.0067,80.2206"}}`)
	if r.StatusCode != 201 || !strings.Contains(fmt.Sprint(m["where"]), "route") {
		t.Fatalf("route watch: %d %v", r.StatusCode, m)
	}
	req, _ := http.NewRequest("DELETE", h.srv.URL+"/api/alerts/"+id, nil)
	req.Header.Set("X-Alert-Token", token)
	d, _ := http.DefaultClient.Do(req)
	if d.StatusCode != 204 || h.app.AlertStore.Len() != 1 {
		t.Fatalf("delete: %d, %d left", d.StatusCode, h.app.AlertStore.Len())
	}
	_, page := get(t, nil, h.srv.URL+"/alerts?city=chennai&from=12.9791,80.2209&to=13.0067,80.2206&vehicle=car")
	if !strings.Contains(page, `"key":`) || !strings.Contains(page, "Turn on alerts") {
		t.Fatal("alerts page")
	}
	_, sw := get(t, nil, h.srv.URL+"/sw.js")
	if !strings.Contains(sw, "showNotification") {
		t.Fatal("service worker")
	}
}
