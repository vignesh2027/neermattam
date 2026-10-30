package app

import (
	"encoding/json"
	"io"
	"os"
	"strings"
	"testing"
	"time"
)

// A person_reference answer from perception goes through the whole report
// path: labelled as measured against a person on the page, in English and
// Tamil, and fused with the person weight.
func TestPersonReferenceEndToEnd(t *testing.T) {
	raw, err := os.ReadFile("../web/testdata/person_reference.json")
	if err != nil {
		t.Fatal(err)
	}
	var body map[string]any
	if err := json.Unmarshal(raw, &body); err != nil {
		t.Fatal(err)
	}
	body["captured_at"] = "2026-10-30T10:00:00Z"
	arrived := time.Date(2026, 10, 30, 10, 20, 0, 0, time.UTC)
	for i, tc := range []struct{ lang, chip, note string }{
		{"en", "Measured from a person", "The depth assumes an Indian adult of average height, 159 cm ± 9 cm"},
		{"ta", "நபரை வைத்து அளக்கப்பட்டது", "சராசரி உயரமுள்ள இந்திய வயதுவந்தவர் (159 cm ± 9 cm)"},
	} {
		h := newHarnessAt(t, nil, true, func() time.Time { return arrived })
		body["measurement_id"] = "m_person_" + tc.lang
		b, _ := json.Marshal(body)
		h.perc.body = string(b)
		r := h.post(t, "/report?lang="+tc.lang, loc, map[string][]byte{"media": photo(60 + i)}, "")
		page, _ := io.ReadAll(r.Body)
		html := string(page)
		if r.StatusCode != 200 {
			t.Fatalf("%s: status %d", tc.lang, r.StatusCode)
		}
		for _, w := range []string{tc.chip, tc.note, "person-note", "49<small>cm</small>", "± 12.7 cm"} {
			if !strings.Contains(html, w) {
				t.Errorf("%s page lacks %q", tc.lang, w)
			}
		}
		h.engine.mu.Lock()
		rd := h.engine.readings[0]
		h.engine.mu.Unlock()
		// Confidence 0.23 passes the person floor, the photo is fresh, and a
		// person counts 0.85.
		if rd["trust"] != 0.85 || rd["depth_sigma_cm"] != 12.7 {
			t.Fatalf("%s: reading %v", tc.lang, rd)
		}
	}
}

// Perception sets aside a shallow wheel reading the scene model contradicts
// and answers with its estimate, naming the wheel's reading in reasons; the
// time budget adds a "budget" step. The page shows the reasons, draws the
// budget step in the language asked for, and a step kind nobody knows yet
// still streams and renders.
func TestShallowWheelSetAsideBudgetAndUnknownSteps(t *testing.T) {
	body := `{"measurement_id":"m_budget","usable":true,"reasons":["standing water visible","two-wheeler wheel found","the wheel reads 1.5 cm (sigma 3.1 cm) but the scene model's estimate (30 cm) is above its two-sigma bound; a shallow reading that deep water contradicts is not published","the time budget of 55 s ran out before every check was done; this is the best answer reached in time"],"depth_cm":30.0,"depth_sigma_cm":20.0,"reference":null,"waterline":null,"confidence":0.22,"models":{"scene":"muse-spark-open","masks":"sam-2.1-hiera-small"},"method":"model_estimate","model_estimate":{"depth_cm":30.0,"depth_sigma_cm":20.0,"basis":"water at mid-tyre","model":"muse-spark-open"},` +
		`"agent_trace":[{"step":"scene","target":"photo","model":"muse-spark-open","input":"photo","output":"water visible","decision":"segment and verify 1 wheel","latency_ms":9000},` +
		`{"step":"budget","target":"photo","model":"pipeline","input":"time budget 55 s","output":"51 s used; checks still open were stopped","decision":"answer with what was verified in time","latency_ms":0},` +
		`{"step":"ponder","target":"photo","model":"future-model","input":"x","output":"y","decision":"carry on","latency_ms":3},` +
		`{"step":"estimate","target":"photo","model":"geometry","input":"measurement 1.5 cm and model estimate 30 cm","output":"estimate above the two-sigma bound","decision":"shallow reading not trusted: report the estimate","latency_ms":0}],"captured_at":null}`
	for i, tc := range []struct{ lang, budget, notes string }{
		{"en", "Time budget", "What the models noted"},
		{"ta", "நேர வரம்பு", "மாதிரிகள் குறிப்பிட்டவை"},
	} {
		h := newHarness(t, nil, true)
		h.perc.stream = true
		h.perc.body = body
		r := h.post(t, "/report?lang="+tc.lang, loc, map[string][]byte{"media": photo(70 + i)}, "text/event-stream")
		names, data := sseEvents(t, r.Body)
		var steps []string
		for j, n := range names {
			if n == "step" {
				steps = append(steps, data[j]["html"].(string))
			}
		}
		if len(steps) != 4 || names[len(names)-1] != "done" {
			t.Fatalf("%s: events %v", tc.lang, names)
		}
		if !strings.Contains(steps[1], "k-budget") || !strings.Contains(steps[1], tc.budget) {
			t.Errorf("%s: budget step %s", tc.lang, steps[1])
		}
		if !strings.Contains(steps[2], "k-other") || !strings.Contains(steps[2], "Ponder") {
			t.Errorf("%s: unknown step %s", tc.lang, steps[2])
		}
		card := data[len(data)-1]["html"].(string)
		for _, w := range []string{tc.notes, "the wheel reads 1.5 cm", "the time budget of 55 s ran out", "is-est"} {
			if !strings.Contains(card, w) {
				t.Errorf("%s: card lacks %q", tc.lang, w)
			}
		}
		trace := data[len(data)-1]["trace_html"].(string)
		if !strings.Contains(trace, "k-budget") || !strings.Contains(trace, "k-other") {
			t.Errorf("%s: trace panel lacks the budget or unknown step", tc.lang)
		}
	}
}
