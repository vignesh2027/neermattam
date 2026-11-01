package app

import (
	"crypto/ed25519"
	"crypto/rand"
	"encoding/base64"
	"html"
	"strings"
	"testing"
	"time"

	"neermattam/gateway/internal/i18n"
)

// codedEstimate is perception's answer for a photo it could only estimate,
// with reason and decision codes (CONTRACTS.md 1.3). The verifier's
// verdict and the estimate's basis are the model's own words.
const codedEstimate = `{"measurement_id":"m_codes","usable":true,` +
	`"reasons":["standing water visible","two-wheeler wheel 1: the scene model did not confirm the mask (the outline covers the mudguard)","no wheel could be measured; depth is the scene model's visual estimate, not a measurement","estimate based on: the lower third of the tyres is under water","the time budget of 55 s ran out before every check was done; this is the best answer reached in time"],` +
	`"reason_codes":[{"code":"water_visible","args":{}},{"code":"wheel_not_confirmed","args":{"target":"two-wheeler wheel 1","detail":"the outline covers the mudguard"}},{"code":"estimate_only","args":{}},{"code":"estimate_basis","args":{"basis":"the lower third of the tyres is under water"}},{"code":"budget_ran_out","args":{"budget_s":55}}],` +
	`"depth_cm":23.0,"depth_sigma_cm":20.0,"reference":null,"waterline":null,"confidence":0.62,"models":{"scene":"muse-spark-open","masks":"sam-2.1-hiera-small"},"method":"model_estimate",` +
	`"model_estimate":{"depth_cm":23.0,"depth_sigma_cm":20.0,"basis":"the lower third of the tyres is under water","model":"muse-spark-open"},` +
	`"agent_trace":[` +
	`{"step":"scene","target":"photo","model":"muse-spark-open","input":"photo","output":"water visible","decision":"segment and verify 1 wheel and the water","latency_ms":15000,"decision_code":{"code":"scene_verify_wheels","args":{"n":1}}},` +
	`{"step":"verify","target":"two wheeler wheel 1","model":"muse-spark-open","input":"crop","output":"view head_on","decision":"give up: tyre seen head-on","latency_ms":5861,"decision_code":{"code":"give_up_reason","args":{"detail":"tyre seen head-on"}}},` +
	`{"step":"estimate","target":"photo","model":"muse-spark-open","input":"no measurable wheel","output":"23 cm, sigma 20 cm","decision":"report model estimate","latency_ms":0,"decision_code":{"code":"report_estimate","args":{}}}],` +
	`"captured_at":"2026-10-27T10:00:00Z"}`

// The result card and the agent trace show perception's reasons and
// decisions in the visitor's language from their codes, with the model's
// own words quoted as they are.
func TestReasonCodesAreShownInThePageLanguage(t *testing.T) {
	arrived := time.Date(2026, 10, 27, 10, 20, 0, 0, time.UTC)
	for i, lang := range []string{"ta", "te", "en"} {
		h := newHarnessAt(t, nil, true, func() time.Time { return arrived })
		h.perc.body = codedEstimate
		r := h.post(t, "/report?lang="+lang, loc, map[string][]byte{"media": photo(60 + i)}, "text/event-stream")
		_, data := sseEvents(t, r.Body)
		last := data[len(data)-1]
		card := html.UnescapeString(last["html"].(string))
		trace := html.UnescapeString(last["trace_html"].(string))
		wants := []string{
			i18n.T(lang, "rc.water_visible"), i18n.T(lang, "rc.estimate_only"), i18n.T(lang, "rc.model_said"),
			`<q lang="en">the outline covers the mudguard</q>`, `<q lang="en">the lower third of the tyres is under water</q>`, "55",
		}
		for _, w := range wants {
			if !strings.Contains(card, w) {
				t.Errorf("%s card lacks %q", lang, w)
			}
		}
		for _, w := range []string{i18n.T(lang, "dc.report_estimate"), `<q lang="en">tyre seen head-on</q>`, "k-giveup"} {
			if !strings.Contains(trace, w) {
				t.Errorf("%s trace lacks %q", lang, w)
			}
		}
		if lang == "en" {
			continue
		}
		for _, english := range []string{"standing water visible", "did not confirm the mask", "visual estimate, not a measurement", "time budget of 55 s", "report model estimate", "two-wheeler wheel 1"} {
			if strings.Contains(card+trace, english) {
				t.Errorf("%s page still shows the English %q", lang, english)
			}
		}
	}
}

// A perception that sends no codes, or a code this gateway does not know,
// gets its English sentence shown unchanged.
func TestUnknownReasonCodesFallBackToEnglish(t *testing.T) {
	arrived := time.Date(2026, 10, 27, 10, 20, 0, 0, time.UTC)
	h := newHarnessAt(t, nil, true, func() time.Time { return arrived })
	h.perc.body = strings.Replace(codedEstimate, `{"code":"water_visible","args":{}}`, `{"code":"water_glowing","args":{}}`, 1)
	r := h.post(t, "/report?lang=ta", loc, map[string][]byte{"media": photo(70)}, "text/event-stream")
	_, data := sseEvents(t, r.Body)
	card := html.UnescapeString(data[len(data)-1]["html"].(string))
	if !strings.Contains(card, "standing water visible") || !strings.Contains(card, i18n.T("ta", "rc.estimate_only")) {
		t.Errorf("unknown code: want its English sentence and the other reasons in Tamil:\n%s", card)
	}
}

// The verify page shows the certificate's reasons and step decisions in
// the page language, from the codes kept beside the certificate.
func TestVerifyPageTranslatesCodedReasonsAndSteps(t *testing.T) {
	pub, priv, _ := ed25519.GenerateKey(rand.Reader)
	h := newHarness(t, map[string]string{"CERTIFICATE_PUBLIC_KEYS": base64.RawURLEncoding.EncodeToString(pub)}, true)
	img := photo(71)
	body := signedBody(t, h, priv, sha(img))
	// The certificate records two reasons and three steps; perception's
	// answer carries their codes.
	body = strings.Replace(body, `"reasons":["standing water visible"]`,
		`"reasons":["standing water visible","two-wheeler wheel found"],"reason_codes":[{"code":"water_visible","args":{}},{"code":"reference_found","args":{"kind":"two_wheeler_wheel"}}]`, 1)
	body = strings.Replace(body, `"agent_trace":[`, `"agent_trace":[{"step":"scene","target":"photo","model":"muse-spark-open","input":"","output":"","decision":"segment and verify 1 wheel and the water","latency_ms":15161,"decision_code":{"code":"scene_verify_wheels","args":{"n":1}}},{"step":"validate","target":"two wheeler 1","model":"validator","input":"","output":"","decision":"retry","latency_ms":0,"decision_code":{"code":"retry","args":{}}},{"step":"verify","target":"two wheeler 1","model":"muse-spark-open","input":"","output":"","decision":"accept","latency_ms":18000,"decision_code":{"code":"accept","args":{}}}],"unused_trace":[`, 1)
	h.perc.body = body
	if r := h.post(t, "/api/report", loc, map[string][]byte{"image": img}, ""); r.StatusCode != 200 {
		t.Fatalf("status %d", r.StatusCode)
	}
	st, ok := h.app.Certs.Get(testMeasurementID)
	if !ok || len(st.ReasonCodes) != 2 || len(st.StepCodes) != 3 {
		t.Fatalf("codes not kept with the certificate: %d reasons, %d steps", len(st.ReasonCodes), len(st.StepCodes))
	}
	_, page := get(t, nil, h.srv.URL+"/verify/"+testMeasurementID+"?lang=ta")
	text := html.UnescapeString(page)
	for _, w := range []string{i18n.T("ta", "rc.water_visible"), i18n.T("ta", "dc.retry"), i18n.T("ta", "dc.accept")} {
		if !strings.Contains(text, w) {
			t.Errorf("Tamil verify page lacks %q", w)
		}
	}
	for _, english := range []string{"<li>standing water visible</li>", `<p class="vfy-dec">retry`, `<p class="vfy-dec">segment and verify`} {
		if strings.Contains(text, english) {
			t.Errorf("Tamil verify page shows the English %q", english) // the raw certificate below stays English
		}
	}
}
