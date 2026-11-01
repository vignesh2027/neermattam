package app

import (
	"crypto/ed25519"
	"crypto/rand"
	"encoding/base64"
	"html"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"neermattam/gateway/internal/i18n"
)

// A measurement the checks refuse must not look like a verified one: the
// verify page says the signature is valid and that the gateway did not add
// it to the map, and why, in every language.
func TestVerifyPageShowsTheGatewayRefusedTheMeasurement(t *testing.T) {
	pub, priv, _ := ed25519.GenerateKey(rand.Reader)
	h := newHarness(t, map[string]string{"CERTIFICATE_PUBLIC_KEYS": base64.RawURLEncoding.EncodeToString(pub)}, true)
	img := photo(11)
	// Confidence 0.2 is under the wheel floor of 0.35: the confidence rule
	// rejects it.
	h.perc.body = strings.Replace(signedBody(t, h, priv, sha(img)), `"confidence":0.82`, `"confidence":0.2`, 1)
	resp := h.post(t, "/api/report", loc, map[string][]byte{"image": img}, "")
	out := decode(t, resp)
	if out["status"] != "rejected" {
		t.Fatalf("status %v", out["status"])
	}
	r, body := get(t, nil, h.srv.URL+"/verify/"+testMeasurementID+"?lang=en")
	if r.StatusCode != 200 {
		t.Fatalf("verify page %d", r.StatusCode)
	}
	text := html.UnescapeString(body)
	for _, want := range []string{
		"Signature verified, but not added to the map",
		"What this site did with it", "Not on the map",
		"This site's checks then refused the measurement, so it was not added to the map.",
		"Why it was refused", "confidence", "not used for the map",
		`class="card panel vfy-verdict v-verified not-used"`,
	} {
		if !strings.Contains(text, want) {
			t.Errorf("verify page lacks %q", want)
		}
	}
	if strings.Contains(text, `<h2 id="vfy-v">Signature verified</h2>`) {
		t.Error("a refused measurement is titled as a plain verified one")
	}
	// Every language says it, with the refusing check in that language.
	for _, l := range i18n.Languages {
		_, body := get(t, nil, h.srv.URL+"/verify/"+testMeasurementID+"?lang="+l.Code)
		text := html.UnescapeString(body)
		for _, key := range []string{"vfy.ok_title_rejected", "vfy.decision_title", "vfy.dec_rejected", "vfy.dec_why", "vfy.dec_chip_rejected"} {
			if !strings.Contains(text, i18n.T(l.Code, key)) {
				t.Errorf("%s verify page lacks %s", l.Code, key)
			}
		}
	}
	// The report page's result card labels the trust as unused.
	if !strings.Contains(i18n.T("en", "result.trust_rejected", "0.20"), "not used for the map") {
		t.Fatal("result.trust_rejected wording")
	}

	// A fused measurement says it reached the map.
	pub2, priv2, _ := ed25519.GenerateKey(rand.Reader)
	h2 := newHarness(t, map[string]string{"CERTIFICATE_PUBLIC_KEYS": base64.RawURLEncoding.EncodeToString(pub2)}, true)
	img2 := photo(12)
	h2.perc.body = signedBody(t, h2, priv2, sha(img2))
	if out := decode(t, h2.post(t, "/api/report", loc, map[string][]byte{"image": img2}, "")); out["status"] != "fused" {
		t.Fatalf("status %v", out["status"])
	}
	_, body = get(t, nil, h2.srv.URL+"/verify/"+testMeasurementID+"?lang=en")
	text = html.UnescapeString(body)
	if !strings.Contains(text, "Added to the map with trust") || !strings.Contains(text, "On the map") || strings.Contains(text, "not added to the map") {
		t.Error("a fused measurement's page should say it is on the map")
	}
}

// The decision is kept on disk beside the certificate, so it survives a
// restart; a certificate kept before decisions were recorded says so.
func TestDecisionIsKeptWithTheCertificate(t *testing.T) {
	pub, priv, _ := ed25519.GenerateKey(rand.Reader)
	h := newHarness(t, map[string]string{"CERTIFICATE_PUBLIC_KEYS": base64.RawURLEncoding.EncodeToString(pub)}, true)
	img := photo(13)
	h.perc.body = strings.Replace(signedBody(t, h, priv, sha(img)), `"confidence":0.82`, `"confidence":0.2`, 1)
	h.post(t, "/api/report", loc, map[string][]byte{"image": img}, "")
	st, ok := h.app.Certs.Get(testMeasurementID)
	if !ok || st.Decision == nil || st.Decision.Status != "rejected" || len(st.Decision.Reasons) == 0 {
		t.Fatalf("decision not kept: %+v", st.Decision)
	}
	if st.Decision.Reasons[0].Text["ta"] == "" || st.Decision.Reasons[0].Text["en"] == "" {
		t.Fatalf("reason texts %+v", st.Decision.Reasons[0].Text)
	}
	if h.app.Cfg.DataDir != "" {
		b, err := os.ReadFile(filepath.Join(h.app.Cfg.DataDir, "certificates", testMeasurementID+".json"))
		if err != nil || !strings.Contains(string(b), `"decision"`) {
			t.Fatalf("decision not on disk: %v", err)
		}
	}
	st.Decision = nil
	if err := h.app.Certs.Put(testMeasurementID, st); err != nil {
		t.Fatal(err)
	}
	_, body := get(t, nil, h.srv.URL+"/verify/"+testMeasurementID+"?lang=en")
	if !strings.Contains(html.UnescapeString(body), "This site did not record what it decided") {
		t.Error("an old certificate should say no decision was recorded")
	}
}

// /readyz does not flip on one slow probe: within READY_GRACE of the last
// successful probe it stays 200 and says the latest probe timed out; after
// the grace, or for a refused connection, it is 503.
func TestReadyzRidesOutOneSlowProbe(t *testing.T) {
	var slow atomic.Bool
	perc := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if slow.Load() {
			time.Sleep(400 * time.Millisecond)
		}
		w.Write([]byte(`{"status":"ready"}`))
	}))
	defer perc.Close()
	h := newHarness(t, map[string]string{"PERCEPTION_URL": perc.URL, "READY_PROBE_TIMEOUT": "150ms", "READY_GRACE": "1s"}, true)
	if r, _ := http.Get(h.srv.URL + "/readyz"); r.StatusCode != 200 {
		t.Fatalf("both up: %d", r.StatusCode)
	}
	slow.Store(true)
	resp, _ := http.Get(h.srv.URL + "/readyz")
	m := decode(t, resp)
	note, _ := m["perception_note"].(string)
	if resp.StatusCode != 200 || m["perception"] != "ready" || !strings.Contains(note, "timed out") {
		t.Fatalf("one slow probe flipped readiness: %d %v", resp.StatusCode, m)
	}
	time.Sleep(1100 * time.Millisecond)
	resp, _ = http.Get(h.srv.URL + "/readyz")
	m = decode(t, resp)
	if resp.StatusCode != 503 || m["perception"] == "ready" {
		t.Fatalf("still slow after the grace should be 503: %d %v", resp.StatusCode, m)
	}
	slow.Store(false)
	if r, _ := http.Get(h.srv.URL + "/readyz"); r.StatusCode != 200 {
		t.Fatalf("recovered: %d", r.StatusCode)
	}
	if h.app.Cfg.ReadyProbeTimeout != 150*time.Millisecond {
		t.Fatalf("READY_PROBE_TIMEOUT not read: %v", h.app.Cfg.ReadyProbeTimeout)
	}
}

func TestNotFoundPageIsBrandedAndTranslated(t *testing.T) {
	h := newHarness(t, nil, true)
	for _, l := range i18n.Languages {
		req, _ := http.NewRequest("GET", h.srv.URL+"/no-such-page?lang="+l.Code, nil)
		// No Accept header, as curl sends: still the page, not JSON.
		resp, err := http.DefaultClient.Do(req)
		if err != nil {
			t.Fatal(err)
		}
		b := readAll(t, resp)
		if resp.StatusCode != 404 || !strings.Contains(resp.Header.Get("Content-Type"), "text/html") {
			t.Fatalf("%s: %d %s", l.Code, resp.StatusCode, resp.Header.Get("Content-Type"))
		}
		text := html.UnescapeString(b)
		for _, want := range []string{`lang="` + l.Code + `"`, i18n.T(l.Code, "nf.title"), i18n.T(l.Code, "nf.body"), `href="/map"`, `class="site-header"`, "/no-such-page"} {
			if !strings.Contains(text, want) {
				t.Errorf("%s 404 page lacks %q", l.Code, want)
			}
		}
	}
	// API paths keep the JSON error.
	resp, _ := http.Get(h.srv.URL + "/api/nope")
	if m := decode(t, resp); resp.StatusCode != 404 || m["code"] != "not_found" {
		t.Fatalf("API 404: %d %v", resp.StatusCode, m)
	}
	// GET /control/login goes to the key page.
	client := &http.Client{CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}
	resp, _ = client.Get(h.srv.URL + "/control/login?lang=ta")
	if resp.StatusCode != http.StatusSeeOther || resp.Header.Get("Location") != "/control?lang=ta" {
		t.Fatalf("GET /control/login: %d %q", resp.StatusCode, resp.Header.Get("Location"))
	}
}

func TestAboutSaysWhenAndWhereItWasBuilt(t *testing.T) {
	info := filepath.Join(t.TempDir(), "BUILD_INFO")
	os.WriteFile(info, []byte("source commit: 2a872fc0d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6\nservices (engine, gateway, perception): clean at this commit\n"), 0o644)
	h := newHarness(t, map[string]string{"BUILD_INFO_PATH": info}, true)
	_, body := get(t, nil, h.srv.URL+"/about?lang=en")
	text := html.UnescapeString(body)
	for _, want := range []string{
		"Built during the Meta Global AI Developer Hackathon, 26 Oct to 4 Nov 2026",
		"The first commit was made on 26 October 2026.",
		`href="https://github.com/vignesh2027/neermattam"`,
		`href="https://github.com/vignesh2027/neermattam/commit/2a872fc0d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6"`,
		"<code>2a872fc</code>",
		// WhatsApp is off in the harness, so the page says so.
		"WhatsApp reports are supported in the code but not switched on in this deployment.",
		"only when the clip allows it",
	} {
		if !strings.Contains(text, want) {
			t.Errorf("about page lacks %q", want)
		}
	}
	for _, claim := range []string{"from the web or WhatsApp", "and how fast it moves, fuses"} {
		if strings.Contains(text, claim) {
			t.Errorf("about page still claims %q", claim)
		}
	}
	// The footer links the repository on every page that has one.
	_, home := get(t, nil, h.srv.URL+"/?lang=ta")
	if !strings.Contains(home, `<a href="https://github.com/vignesh2027/neermattam" rel="noopener">`+i18n.T("ta", "footer.source")+`</a>`) {
		t.Error("footer lacks the repository link")
	}
}

func readAll(t *testing.T, r *http.Response) string {
	t.Helper()
	defer r.Body.Close()
	var b strings.Builder
	buf := make([]byte, 32<<10)
	for {
		n, err := r.Body.Read(buf)
		b.Write(buf[:n])
		if err != nil {
			break
		}
	}
	return b.String()
}
