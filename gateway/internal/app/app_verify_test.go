package app

import (
	"crypto/ed25519"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"html"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"neermattam/gateway/internal/certs"
)

const testMeasurementID = "m_01M660TESTVERIFY0000000000"

// signedBody is the fake perception's usable answer with a certificate
// signed by priv for an upload whose SHA-256 is uploadSHA.
func signedBody(t *testing.T, h *harness, priv ed25519.PrivateKey, uploadSHA string) string {
	t.Helper()
	record := map[string]any{
		"format": certs.Format, "measurement_id": testMeasurementID, "issued_at": "2026-10-30T09:00:00Z",
		"input":   map[string]any{"kind": "image", "sha256": uploadSHA, "width": 320, "height": 240},
		"models":  []map[string]string{{"role": "scene", "name": "muse-spark-open", "version": "meta-models/muse-spark-open on Baseten"}, {"role": "masks", "name": "sam-2.1-hiera-small", "version": "facebook/sam2.1-hiera-small, float32, run locally"}},
		"prompts": []map[string]string{{"id": "verify-wheel@1d51d73f5037", "sha256": strings.Repeat("ab", 32)}},
		"steps": []map[string]any{
			{"step": "scene", "target": "photo", "model": "muse-spark-open", "prompt_version": "scene@cf7a74eb23ee", "decision": "segment and verify 1 wheel and the water", "latency_ms": 15161},
			{"step": "validate", "target": "two wheeler 1", "model": "validator", "prompt_version": nil, "decision": "retry", "latency_ms": 0},
			{"step": "verify", "target": "two wheeler 1", "model": "muse-spark-open", "prompt_version": "verify-wheel@1d51d73f5037", "decision": "accept", "latency_ms": 18000},
		},
		"checks": map[string]any{"rules": []string{"schema", "grid", "inside image", "allowed values"}, "failures": []string{"two wheeler 1: is_target: 'yes' is not true or false (retry)"}},
		"geometry": map[string]any{
			"wheel": map[string]any{
				"target":    "two wheeler 1",
				"reference": map[string]any{"kind": "two_wheeler_wheel", "diameter_cm": 43.4, "diameter_sigma_cm": 2.0, "source": "catalogue:90/100-10", "squash_cm": 0.8, "squash_sigma_cm": 0.4},
				"ellipse":   map[string]any{"cx": 636.2, "cy": 604.1, "a": 24.9, "b": 23.7, "theta": 0.12},
				"waterline": map[string]any{"point": []float64{636, 812.5}, "direction": []float64{1, 0}, "water_normal": []float64{0, 1}, "rms_px": 0.6, "n_points": 41},
				"camera":    nil, "arc_covered_deg": 213.0, "fraction_raw": 0.531, "fraction_bias": -0.0096, "fraction": 0.5406,
				"ambiguity_spread": 0.0, "resample_sigma_cm": 2.1, "model_sigma_share": 0.0631, "depth_cm": 23.46, "depth_sigma_cm": 3.4,
				"second_waterline": nil, "agreement_cm": nil, "estimate_here_cm": 30.0,
			},
			"person":   nil,
			"estimate": map[string]any{"depth_cm": 30.0, "low_cm": 20.0, "high_cm": 40.0, "sigma_cm": 20.0, "basis": "lower half of the wheels"},
		},
		"decision": map[string]any{"usable": true, "method": "wheel_geometry", "depth_cm": 23.4, "depth_sigma_cm": 3.1, "confidence": 0.82, "reasons": []string{"standing water visible", "two-wheeler wheel found"}},
	}
	payload, _ := json.Marshal(record)
	env := certs.Envelope{
		Format: certs.Format, Alg: certs.Alg, KeyID: certs.KeyID(priv.Public().(ed25519.PublicKey)),
		Payload: string(payload), Signature: base64.RawURLEncoding.EncodeToString(ed25519.Sign(priv, payload)),
	}
	envJSON, _ := json.Marshal(env)
	body := strings.Replace(h.perc.body, `"measurement_id":"m_1"`, `"measurement_id":"`+testMeasurementID+`"`, 1)
	return strings.TrimSuffix(body, "}") + `,"input_sha256":"` + uploadSHA + `","certificate":` + string(envJSON) + "}"
}

func sha(b []byte) string { s := sha256.Sum256(b); return hex.EncodeToString(s[:]) }

func TestReportLinksAVerifiedCertificate(t *testing.T) {
	pub, priv, _ := ed25519.GenerateKey(rand.Reader)
	h := newHarness(t, map[string]string{"CERTIFICATE_PUBLIC_KEYS": base64.RawURLEncoding.EncodeToString(pub)}, true)
	img := photo(7)
	h.perc.body = signedBody(t, h, priv, sha(img))
	resp := h.post(t, "/api/report", loc, map[string][]byte{"image": img}, "")
	if resp.StatusCode != 200 {
		t.Fatalf("status %d", resp.StatusCode)
	}
	out := decode(t, resp)
	if out["certificate_url"] != "/verify/"+testMeasurementID {
		t.Fatalf("certificate_url %v", out["certificate_url"])
	}
	if _, ok := out["measurement"].(map[string]any)["certificate"]; ok {
		t.Fatal("the certificate was left in the reply; it is served on its own")
	}

	r, body := get(t, nil, h.srv.URL+"/verify/"+testMeasurementID+"?lang=en")
	if r.StatusCode != 200 {
		t.Fatalf("verify page %d", r.StatusCode)
	}
	text := html.UnescapeString(body)
	for _, want := range []string{
		`lang="en"`, "Verify this measurement", "Signature verified", certs.KeyID(pub),
		"The photo's fingerprint matches the file this site received.",
		"23.4<small>cm</small>", "± 3.1 cm", "Measured from a wheel",
		"cx 636.20, cy 604.10, a 24.90, b 23.70 px", "f = 0.5310 +0.0096 = 0.5406", "Tyre squash",
		"facebook/sam2.1-hiera-small, float32, run locally", "verify-wheel@1d51d73f5037",
		"is_target: 'yes' is not true or false", "neermattam_perception.replay",
		"/api/certificates/" + testMeasurementID, "/.well-known/neermattam-keys.json",
	} {
		if !strings.Contains(text, want) {
			t.Errorf("verify page lacks %q", want)
		}
	}
	if strings.ContainsAny(body, "—→") {
		t.Error("em dash or arrow on the page")
	}

	r, raw := get(t, nil, h.srv.URL+"/api/certificates/"+testMeasurementID)
	if r.StatusCode != 200 || r.Header.Get("X-Neermattam-Verified") != "true" {
		t.Fatalf("certificate API: %d %s", r.StatusCode, r.Header.Get("X-Neermattam-Verified"))
	}
	if res := certs.Verify([]byte(raw), certs.KeyMap{certs.KeyID(pub): pub}); !res.Valid() {
		t.Fatalf("served certificate does not verify: %+v", res)
	}

	r, keys := get(t, nil, h.srv.URL+"/.well-known/neermattam-keys.json")
	if r.StatusCode != 200 || !strings.Contains(keys, certs.KeyID(pub)) || !strings.Contains(keys, `"source": "config"`) {
		t.Fatalf("well-known keys: %d %s", r.StatusCode, keys)
	}
}

func TestVerifyPageInTamil(t *testing.T) {
	pub, priv, _ := ed25519.GenerateKey(rand.Reader)
	h := newHarness(t, map[string]string{"CERTIFICATE_PUBLIC_KEYS": base64.RawURLEncoding.EncodeToString(pub)}, true)
	img := photo(8)
	h.perc.body = signedBody(t, h, priv, sha(img))
	if r := h.post(t, "/api/report", loc, map[string][]byte{"image": img}, ""); r.StatusCode != 200 {
		t.Fatalf("status %d", r.StatusCode)
	}
	r, body := get(t, nil, h.srv.URL+"/verify/"+testMeasurementID+"?lang=ta")
	if r.StatusCode != 200 {
		t.Fatalf("status %d", r.StatusCode)
	}
	text := html.UnescapeString(body)
	for _, want := range []string{
		`lang="ta"`, "Noto+Sans+Tamil", "இந்த அளவீட்டைச் சரிபார்க்கவும்", "கையொப்பம் சரிபார்க்கப்பட்டது",
		"புகைப்படத்தின் கைரேகை, இந்தத் தளம் பெற்ற கோப்புடன் பொருந்துகிறது.", "சக்கரத்தை வைத்து அளக்கப்பட்டது",
		"ஆழம் கணக்கிடப்பட்ட எண்கள்", "டயர் அமுக்கம்", "நீங்களே மீண்டும் கணக்கிடுங்கள்",
		"23.4<small>cm</small>", certs.KeyID(pub),
	} {
		if !strings.Contains(text, want) {
			t.Errorf("Tamil verify page lacks %q", want)
		}
	}
	for _, english := range []string{"Signature verified", "Numbers the depth was computed from", "Recompute it yourself"} {
		if strings.Contains(text, english) {
			t.Errorf("Tamil page shows English %q", english)
		}
	}
}

func TestTamperedCertificateIsShownAsNotVerifying(t *testing.T) {
	pub, priv, _ := ed25519.GenerateKey(rand.Reader)
	h := newHarness(t, map[string]string{"CERTIFICATE_PUBLIC_KEYS": base64.RawURLEncoding.EncodeToString(pub)}, true)
	img := photo(9)
	h.perc.body = signedBody(t, h, priv, sha(img))
	if r := h.post(t, "/api/report", loc, map[string][]byte{"image": img}, ""); r.StatusCode != 200 {
		t.Fatalf("status %d", r.StatusCode)
	}
	// Someone edits the stored certificate to claim shallower water.
	st, ok := h.app.Certs.Get(testMeasurementID)
	if !ok {
		t.Fatal("not stored")
	}
	forged := strings.Replace(string(st.Certificate), `\"depth_cm\":23.4`, `\"depth_cm\":3.4`, 1)
	if forged == string(st.Certificate) {
		t.Fatal("the test did not change the certificate")
	}
	st.Certificate = json.RawMessage(forged)
	if err := h.app.Certs.Put(testMeasurementID, st); err != nil {
		t.Fatal(err)
	}
	r, body := get(t, nil, h.srv.URL+"/verify/"+testMeasurementID+"?lang=en")
	text := html.UnescapeString(body)
	if r.StatusCode != 200 || !strings.Contains(text, "The signature does not verify") || !strings.Contains(text, "for inspection only") || strings.Contains(text, "Signature verified") {
		t.Fatalf("tampered certificate page: %d", r.StatusCode)
	}
	if !strings.Contains(text, "the signature does not match the certificate") {
		t.Error("the reason is not shown")
	}
	if r, _ := get(t, nil, h.srv.URL+"/api/certificates/"+testMeasurementID); r.Header.Get("X-Neermattam-Verified") != "false" {
		t.Fatal("API says a tampered certificate verifies")
	}
	r, body = get(t, nil, h.srv.URL+"/verify/"+testMeasurementID+"?lang=ta")
	if !strings.Contains(html.UnescapeString(body), "கையொப்பம் பொருந்தவில்லை") {
		t.Error("Tamil page does not say the signature fails")
	}
}

func TestPythonSignedCertificateVerifiesWithTheKeyFetchedFromPerception(t *testing.T) {
	h := newHarness(t, nil, true)
	keys, err := os.ReadFile(filepath.Join("..", "certs", "testdata", "certificate_keys.json"))
	if err != nil {
		t.Fatal(err)
	}
	raw, _ := os.ReadFile(filepath.Join("..", "certs", "testdata", "certificate_person.json"))
	var env certs.Envelope
	_ = json.Unmarshal(raw, &env)
	var rec certs.Record
	_ = json.Unmarshal([]byte(env.Payload), &rec)
	// The gateway does not know the key yet; perception publishes it.
	h.perc.keys.Store(string(keys))
	if err := h.app.Certs.Put(rec.MeasurementID, certs.Stored{Certificate: raw, UploadSHA256: strings.Repeat("0", 64)}); err != nil {
		t.Fatal(err)
	}
	r, body := get(t, nil, h.srv.URL+"/verify/"+rec.MeasurementID+"?lang=en")
	text := html.UnescapeString(body)
	if r.StatusCode != 200 || !strings.Contains(text, "Signature verified") {
		t.Fatalf("Python certificate: %d, verified=%v", r.StatusCode, strings.Contains(text, "Signature verified"))
	}
	for _, want := range []string{"Measured from a person", "Outline rows: top; shoulders; bottom", "Adult stature prior", "does not match the file this site received"} {
		if !strings.Contains(text, want) {
			t.Errorf("page lacks %q", want)
		}
	}
	_, wk := get(t, nil, h.srv.URL+"/.well-known/neermattam-keys.json")
	if !strings.Contains(wk, env.KeyID) || !strings.Contains(wk, `"source": "perception"`) {
		t.Fatalf("well-known keys: %s", wk)
	}
	// The fetched key was kept, so it survives a restart.
	if _, err := os.Stat(filepath.Join(h.app.Cfg.DataDir, certs.KeyFileName)); err != nil {
		t.Fatalf("fetched keys not saved: %v", err)
	}
}

func TestOlderPerceptionWithoutCertificates(t *testing.T) {
	h := newHarness(t, nil, true) // the fake's body has no certificate, like an older perception
	resp := h.post(t, "/api/report", loc, map[string][]byte{"image": photo(10)}, "")
	out := decode(t, resp)
	if resp.StatusCode != 200 || out["status"] != "fused" {
		t.Fatalf("report failed without a certificate: %d %v", resp.StatusCode, out["status"])
	}
	if _, ok := out["certificate_url"]; ok {
		t.Fatal("a certificate link without a certificate")
	}
	r, body := get(t, nil, h.srv.URL+"/verify/m_01NOSUCHMEASUREMENT?lang=en")
	if r.StatusCode != 404 || !strings.Contains(body, "No certificate for this measurement") {
		t.Fatalf("missing certificate page: %d", r.StatusCode)
	}
	for _, bad := range []string{"/verify/..%2f..%2fetc", "/api/certificates/m_01NOSUCHMEASUREMENT", "/api/certificates/x"} {
		if r, _ := get(t, nil, h.srv.URL+bad); r.StatusCode != 404 {
			t.Errorf("%s: %d", bad, r.StatusCode)
		}
	}
}
