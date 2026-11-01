package reasons

import (
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"testing"

	"neermattam/gateway/internal/i18n"
	"neermattam/gateway/internal/upstream"
)

func code(c string, args map[string]any) *upstream.Code {
	if args == nil {
		args = map[string]any{}
	}
	return &upstream.Code{Code: c, Args: args}
}

// contractTables reads the code tables of CONTRACTS.md section 1.3, keyed
// by the line that introduces each table.
func contractTables(t *testing.T) map[string][]string {
	t.Helper()
	b, err := os.ReadFile(filepath.Join("..", "..", "..", "docs", "CONTRACTS.md"))
	if err != nil {
		t.Skip("docs/CONTRACTS.md is not in this build context")
	}
	sec := strings.Split(string(b), "### 1.3 Reason and decision codes")[1]
	sec = strings.Split(sec, "## 2. Engine")[0]
	out := map[string][]string{}
	current := ""
	row := regexp.MustCompile("^\\| `([a-z_0-9]+)` \\|")
	for _, line := range strings.Split(sec, "\n") {
		switch {
		case strings.HasPrefix(line, "Reasons ("):
			current = "rc."
		case strings.HasPrefix(line, "Step decisions"):
			current = "dc."
		case strings.HasPrefix(line, "Wheel geometry failures"):
			current = "rc.wheel."
		case strings.HasPrefix(line, "Person declines"):
			current = "rc.person."
		case strings.HasPrefix(line, "Velocity failures"):
			current = "rc.velocity."
		}
		if m := row.FindStringSubmatch(line); m != nil && current != "" {
			out[current] = append(out[current], m[1])
		}
	}
	return out
}

func TestCatalogueCoversTheContract(t *testing.T) {
	tables := contractTables(t)
	total := 0
	for prefix, codes := range tables {
		for _, c := range codes {
			total++
			if _, ok := i18n.Catalogs["en"][prefix+c]; !ok {
				t.Errorf("no message for %s%s", prefix, c)
			}
		}
	}
	if total < 110 {
		t.Fatalf("read only %d codes from CONTRACTS.md; the parse is broken", total)
	}
}

func TestEveryLanguageKeepsTheNamedPlaceholders(t *testing.T) {
	en := i18n.Catalogs["en"]
	n := 0
	for key, msg := range en {
		if !strings.HasPrefix(key, "rc.") && !strings.HasPrefix(key, "dc.") {
			continue
		}
		n++
		want := Placeholders(msg)
		sort.Strings(want)
		for _, l := range i18n.Languages {
			got := Placeholders(i18n.Catalogs[l.Code][key])
			sort.Strings(got)
			if strings.Join(got, ",") != strings.Join(want, ",") {
				t.Errorf("%s %s: placeholders %v, English has %v", l.Code, key, got, want)
			}
		}
	}
	if n < 130 {
		t.Fatalf("only %d code messages", n)
	}
}

// sample gives a value for every argument a reason template can take.
var sample = map[string]any{
	"kind": "car_wheel", "where": "at that wheel", "landmark": "mid thigh", "people": "1 and 2",
	"target": "two-wheeler wheel 1", "vehicle": "Tata Ace", "label": "Honda Activa 90/100-10",
	"detail": "the outline covers the mudguard", "basis": "water at the knees",
	"person": 2.0, "n": 3.0, "vehicles": 2.0, "wheels": 1.0, "rejected": 1.0, "used": 3.0,
	"box": "[600,570,670,640]", "points": "points [5,6], [7,8]",
}

func TestEveryContractCodeRendersInEveryLanguage(t *testing.T) {
	tables := contractTables(t)
	for _, l := range i18n.Languages {
		for _, c := range tables["rc."] {
			args := map[string]any{}
			for _, p := range Placeholders(i18n.Catalogs["en"]["rc."+c]) {
				v, ok := sample[p]
				if !ok {
					v = 12.5 // every other placeholder is a number
				}
				args[p] = v
			}
			switch c {
			case "wheel_failed":
				args["failure"] = "arc_too_short"
			case "person_declined":
				args["failure"] = "posture"
			case "velocity_unavailable":
				args["failure"] = "tracks_disagree"
			}
			got := Reason(l.Code, "ENGLISH", code(c, args))
			if got.Text == "ENGLISH" || strings.ContainsAny(got.Text, "{}") || strings.TrimSpace(got.Text) == "" {
				t.Errorf("%s %s rendered %q", l.Code, c, got.Text)
			}
		}
		for _, c := range tables["dc."] {
			args := map[string]any{}
			for _, p := range Placeholders(i18n.Catalogs["en"]["dc."+c]) {
				args[p] = sample[p]
			}
			if c == "verify_best" {
				args["n"] = "two"
			}
			d, ok := StepDecision(l.Code, code(c, args))
			if !ok || strings.ContainsAny(d.Text, "{}") || d.Text == "" {
				t.Errorf("%s decision %s rendered %q, %v", l.Code, c, d.Text, ok)
			}
		}
	}
}

func TestReasonInTamilKeepsNumbersAndQuotesTheModel(t *testing.T) {
	r := Reason("ta", "tyre squash of 1.9 cm subtracted (sigma 0.9 cm)", code("tyre_squash", map[string]any{"squash_cm": 1.9, "sigma_cm": 0.9}))
	if !strings.Contains(r.Text, "1.9") || !strings.Contains(r.Text, "0.9") || strings.Contains(r.Text, "squash") || r.Quote != "" {
		t.Errorf("tyre squash in Tamil: %+v", r)
	}
	r = Reason("ta", "two-wheeler wheel 1: the scene model did not confirm the mask (this is a shadow)",
		code("wheel_not_confirmed", map[string]any{"target": "two-wheeler wheel 1", "detail": "this is a shadow"}))
	if r.Quote != "this is a shadow" || strings.Contains(r.Text, "shadow") || !strings.Contains(r.Text, "1") {
		t.Errorf("model detail must be quoted apart, not translated: %+v", r)
	}
	if strings.Contains(r.Text, "two-wheeler") {
		t.Errorf("target not translated: %q", r.Text)
	}
	r = Reason("hi", "estimate based on: water at the ankles", code("estimate_basis", map[string]any{"basis": "water at the ankles"}))
	if r.Quote != "water at the ankles" || strings.Contains(r.Text, "ankles") {
		t.Errorf("basis: %+v", r)
	}
	r = Reason("te", "the time budget of 55 s ran out before every check was done; this is the best answer reached in time",
		code("budget_ran_out", map[string]any{"budget_s": 55.0}))
	if !strings.Contains(r.Text, "55") || strings.Contains(r.Text, "55.0") || strings.Contains(r.Text, "budget") {
		t.Errorf("budget in Telugu: %q", r.Text)
	}
}

func TestEnglishComesFromTheCatalogueToo(t *testing.T) {
	r := Reason("en", "car wheel: water does not meet the wheel", code("wheel_failed", map[string]any{"kind": "car_wheel", "failure": "no_water_contact"}))
	if r.Text != "Car wheel: the water does not meet the wheel" {
		t.Errorf("got %q", r.Text)
	}
	r = Reason("en", "person 2: the person is not standing upright (sitting)", code("person_declined", map[string]any{"person": 2.0, "failure": "posture", "detail": "sitting"}))
	if r.Text != "Person 2: the person is not standing upright" || r.Quote != "sitting" {
		t.Errorf("got %+v", r)
	}
	r = Reason("en", "people 1 and 2 agree: 40 cm (sigma 9 cm), their readings combined", code("persons_combined", map[string]any{"people": "1 and 2", "depth_cm": 40.0, "sigma_cm": 9.0}))
	if r.Text != "People 1 and 2 agree: 40 ± 9 cm, their readings combined" {
		t.Errorf("got %q", r.Text)
	}
	r = Reason("en", "velocity unavailable: video tracking by noun phrase needs SAM 3.1",
		code("velocity_unavailable", map[string]any{"failure": "other", "detail": "video tracking by noun phrase needs SAM 3.1"}))
	if r.Text != "Speed unavailable" || r.Quote != "video tracking by noun phrase needs SAM 3.1" {
		t.Errorf("got %+v", r)
	}
}

func TestUnknownFallsBackToTheEnglishSentence(t *testing.T) {
	const en = "something a newer perception says"
	cases := []*upstream.Code{
		nil,
		code("a_code_from_the_future", map[string]any{"x": 1.0}),
		code("wheel_failed", map[string]any{"kind": "car_wheel", "failure": "a_new_failure"}),
		code("reference_found", map[string]any{"kind": "boat_hull"}),
		code("person_landmark", map[string]any{"landmark": "elbow", "depth_cm": 50.0, "apart_cm": 3.0}),
		code("tyre_squash", map[string]any{"squash_cm": 1.0}), // an argument missing
	}
	for _, c := range cases {
		for _, l := range []string{"en", "ta", "mr"} {
			if got := Reason(l, en, c); got.Text != en || got.Quote != "" {
				t.Errorf("%s %+v: got %+v, want the English sentence", l, c, got)
			}
		}
	}
	if got := Reason("ta", en, code("other", map[string]any{"text": "free text"})); got.Text != "free text" {
		t.Errorf("other: %+v", got)
	}
	if _, ok := StepDecision("ta", code("dance", nil)); ok {
		t.Error("unknown decision code rendered")
	}
	if _, ok := StepDecision("ta", nil); ok {
		t.Error("nil decision code rendered")
	}
}

func TestListNeedsOneCodePerReason(t *testing.T) {
	english := []string{"standing water visible", "car wheel found"}
	codes := []upstream.Code{{Code: "water_visible"}, {Code: "reference_found", Args: map[string]any{"kind": "car_wheel"}}}
	got := List("ta", english, codes)
	if len(got) != 2 || got[0].Text == english[0] || got[1].Text == english[1] {
		t.Errorf("aligned codes not used: %+v", got)
	}
	got = List("ta", english, codes[:1])
	if got[0].Text != english[0] || got[1].Text != english[1] {
		t.Errorf("misaligned codes must fall back to English: %+v", got)
	}
	if List("ta", nil, nil) != nil {
		t.Error("no reasons should give nil")
	}
}

func TestStepDecisions(t *testing.T) {
	d, ok := StepDecision("ta", code("refine", map[string]any{"box": "[1,2,3,4]", "points": "points [5,6], [7,8]"}))
	if !ok || d.Tone != "refine" || !strings.HasPrefix(d.Detail, "[1,2,3,4] px + ") || !strings.Contains(d.Detail, "[5,6], [7,8]") || strings.Contains(d.Detail, "points") {
		t.Errorf("refine: %+v", d)
	}
	d, _ = StepDecision("en", code("give_up_reason", map[string]any{"detail": "tyre seen head-on"}))
	if d.Tone != "stop" || d.Text != "Give up" || d.Quote != "tyre seen head-on" {
		t.Errorf("give up: %+v", d)
	}
	d, _ = StepDecision("en", code("verify_best", map[string]any{"n": "two"}))
	if d.Text != "Verify the best 2" {
		t.Errorf("verify best: %+v", d)
	}
	d, _ = StepDecision("en", code("scene_search_vehicles", map[string]any{"vehicles": 2.0, "wheels": 1.0}))
	if d.Text != "Search 2 vehicle box(es) for wheels, check 1 wheel(s) and outline the water" {
		t.Errorf("search: %q", d.Text)
	}
	d, _ = StepDecision("en", code("scene_search_vehicles", map[string]any{"vehicles": 1.0}))
	if d.Text != "Search 1 vehicle box(es) for wheels and outline the water" {
		t.Errorf("search without wheels: %q", d.Text)
	}
	d, _ = StepDecision("en", code("accept_partial", nil))
	if d.Tone != "ok" {
		t.Errorf("accept partial: %+v", d)
	}
}

func TestTarget(t *testing.T) {
	if got := Target("en", "two wheeler wheel 1"); got != "two-wheeler wheel 1" {
		t.Errorf("got %q", got)
	}
	if got := Target("ta", "person 2"); got == "person 2" || !strings.Contains(got, "2") {
		t.Errorf("got %q", got)
	}
	if got := Target("en", "two wheeler 1 wheel"); got != "two-wheeler wheel 1" {
		t.Errorf("vehicle wheel: got %q", got)
	}
	if got := Target("te", "car 2 wheel"); got == "car 2 wheel" || !strings.Contains(got, "2") {
		t.Errorf("vehicle wheel in Telugu: got %q", got)
	}
	for _, raw := range []string{"truck 1 wheel", "water", "photo", ""} {
		if got := Target("ta", raw); got != raw {
			t.Errorf("%q became %q", raw, got)
		}
	}
}

// The pipeline's own fixed details are translated, never attributed to the
// model; a model's verdict is quoted as its words; a tool's error gets a
// neutral label.
func TestDetailsAreAttributedCorrectly(t *testing.T) {
	r := Reason("ta", "two wheeler 2 wheel: the scene model did not confirm the mask (time budget reached)",
		code("wheel_not_confirmed", map[string]any{"target": "two wheeler 2 wheel", "detail": "time budget reached"}))
	if r.Quote != "" || !strings.Contains(r.Text, i18n.T("ta", "rc.detail.time_budget_reached")) || strings.Contains(r.Text, "wheeler") {
		t.Errorf("fixed detail: %+v", r)
	}
	r = Reason("en", "x", code("wheel_not_confirmed", map[string]any{"target": "car wheel 1", "detail": "no wheel-like proposal in the vehicle box"}))
	if r.Text != "Car wheel 1: the scene model did not confirm the outline (no wheel-like outline was found in the vehicle's box)" {
		t.Errorf("got %q", r.Text)
	}
	r = Reason("en", "x", code("wheel_not_confirmed", map[string]any{"target": "car wheel 1", "detail": "this is the mudguard"}))
	if r.Quote != "this is the mudguard" || r.QuoteLabel != "In the model's words:" {
		t.Errorf("model verdict: %+v", r)
	}
	r = Reason("en", "x", code("flow_tracking_failed", map[string]any{"detail": "CUDA out of memory"}))
	if r.Quote != "CUDA out of memory" || r.QuoteLabel != "Details:" {
		t.Errorf("tool error: %+v", r)
	}
	d, _ := StepDecision("hi", code("give_up_reason", map[string]any{"detail": "time budget reached"}))
	if d.Quote != "" || d.Detail != i18n.T("hi", "rc.detail.time_budget_reached") {
		t.Errorf("give up on the budget: %+v", d)
	}
	for _, l := range i18n.Languages {
		for _, id := range fixedDetails {
			if _, ok := i18n.Catalogs[l.Code]["rc.detail."+id]; !ok {
				t.Errorf("%s lacks rc.detail.%s", l.Code, id)
			}
		}
	}
}

func TestDecisionFromEnglishMatchesPerceptionsCatalogue(t *testing.T) {
	cases := map[string]string{
		"segment and verify 2 wheels and the water":                                   "scene_verify_wheels",
		"search 2 vehicle boxes for wheels, check 1 wheel and segment the water":      "scene_search_vehicles",
		"refine with box [1,2,3,4] px + points [5,6], [7,8]":                          "refine",
		"give up: tyre seen head-on":                                                  "give_up_reason",
		"verify the best two":                                                         "verify_best",
		"no speed (3 tracks rejected)":                                                "no_speed_rejected",
		"report model estimate":                                                       "report_estimate",
		"no wheel: measure 2 persons against the stature prior and segment the water": "scene_measure_persons",
	}
	for text, want := range cases {
		c := DecisionFromEnglish(text)
		if c == nil || c.Code != want {
			t.Errorf("%q: got %+v, want %s", text, c, want)
			continue
		}
		if _, ok := StepDecision("ta", c); !ok {
			t.Errorf("%q does not render", text)
		}
	}
	c := DecisionFromEnglish("search 2 vehicle boxes for wheels, check 1 wheel and segment the water")
	if c.Args["vehicles"] != 2.0 || c.Args["wheels"] != 1.0 {
		t.Errorf("args %v", c.Args)
	}
	if c := DecisionFromEnglish("refine with box [1,2,3,4] px"); c.Args["box"] != "[1,2,3,4]" {
		t.Errorf("box %v", c.Args)
	}
	if DecisionFromEnglish("report measurement") != nil || DecisionFromEnglish("") != nil {
		t.Error("text outside the catalogue must give nil")
	}
	// Every decision code in the contract has a pattern here.
	tables := contractTables(t)
	have := map[string]bool{}
	for _, p := range decisionPatterns {
		have[p.code] = true
	}
	for _, c := range tables["dc."] {
		if !have[c] {
			t.Errorf("no English pattern for decision %s", c)
		}
	}
}

func TestCheckNames(t *testing.T) {
	if got := Check("en", "inside image"); got != "inside the image" {
		t.Errorf("got %q", got)
	}
	if got := Check("ta", "allowed values"); got == "allowed values" {
		t.Error("check name not translated")
	}
	if got := Check("ta", "a new check"); got != "a new check" {
		t.Errorf("unknown check: %q", got)
	}
}
