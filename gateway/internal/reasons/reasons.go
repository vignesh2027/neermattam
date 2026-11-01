// Package reasons renders perception's reasons and step decisions in the
// visitor's language from their codes (docs/CONTRACTS.md, section 1.3).
//
// Perception sends every reason as an English sentence and, beside it, a
// code with arguments. The sentence for each code lives in the i18n
// catalogue (messages_codes_<lang>.go) with named placeholders. Arguments
// that are words (a reference kind, a body landmark, a failure sub-code)
// are translated too; numbers are written as perception sent them. Text a
// model wrote (a verifier's verdict, an image issue, the estimate's basis)
// is never translated: it is returned apart, to be shown as quoted model
// output. Anything this gateway does not know (a code, a sub-code or a word
// from a newer perception, or no code at all) falls back to perception's
// English sentence, unchanged.
package reasons

import (
	"regexp"
	"strconv"
	"strings"

	"neermattam/gateway/internal/i18n"
	"neermattam/gateway/internal/upstream"
)

// View is one reason ready to show.
type View struct {
	// Text is the reason in the page language, or perception's English
	// sentence when its code is unknown here.
	Text string
	// Quote is text a model or a tool wrote, shown as it is, in quotes,
	// after QuoteLabel ("In the model's words:", or "Details:" for a
	// tool's error text).
	Quote      string
	QuoteLabel string
}

// Decision is one step decision ready to show. Code is empty when it
// could not be rendered from a code.
type Decision struct {
	Code   string
	Tone   string // ok, refine, stop or neutral
	Text   string // the short label, such as "Accept"
	Detail string // numbers that go with it, such as the new box
	Quote  string // text a model wrote, shown as it is
}

// List renders reasons with their codes. The codes are used only when
// there is one per reason, as the contract says; otherwise every reason is
// shown in English, as perception wrote it.
func List(lang string, english []string, codes []upstream.Code) []View {
	if len(english) == 0 {
		return nil
	}
	out := make([]View, len(english))
	for i, text := range english {
		if len(codes) == len(english) {
			out[i] = Reason(lang, text, &codes[i])
		} else {
			out[i] = View{Text: text}
		}
	}
	return out
}

// Reason renders one reason. english is perception's sentence, the
// fallback; c may be nil.
func Reason(lang, english string, c *upstream.Code) View {
	fallback := View{Text: english}
	if c == nil || c.Code == "" {
		return fallback
	}
	args := strArgs(c.Args)
	if c.Code == "other" {
		if t := args["text"]; t != "" {
			return View{Text: t}
		}
		return fallback
	}
	key := "rc." + c.Code
	quote := ""
	switch c.Code {
	case "image_issue", "scene_answer_rejected", "water_not_confirmed", "wheel_not_confirmed",
		"flow_locator_failed", "flow_tracking_failed":
		quote = args["detail"]
	case "estimate_basis":
		quote = args["basis"]
	case "wheel_failed", "person_declined", "velocity_unavailable":
		table := map[string]string{"wheel_failed": "rc.wheel.", "person_declined": "rc.person.", "velocity_unavailable": "rc.velocity."}[c.Code]
		failure := args["failure"]
		quote = args["detail"]
		if c.Code == "velocity_unavailable" && failure == "other" {
			key = "rc.velocity_unavailable_other"
			break
		}
		w, ok := word(lang, table, failure)
		if !ok {
			return fallback
		}
		args["failure"] = w
	}
	for name, prefix := range map[string]string{"kind": "rc.kind.", "where": "rc.where.", "landmark": "rc.landmark."} {
		if v, present := args[name]; present {
			w, ok := word(lang, prefix, v)
			if !ok {
				return fallback
			}
			args[name] = w
		}
	}
	if p, ok := args["people"]; ok {
		and, _ := i18n.Lookup(lang, "rc.and")
		args["people"] = strings.Join(strings.Split(p, " and "), and)
	}
	if t, ok := args["target"]; ok {
		args["target"] = Target(lang, t)
	}
	text, ok := fill(lang, key, args)
	if !ok {
		return fallback
	}
	v := View{Text: capitalise(text), Quote: quote}
	if d, ok := fixedDetail(lang, quote); ok {
		// The pipeline's own words, not a model's: translated.
		v.Text, v.Quote = v.Text+" ("+d+")", ""
	}
	if v.Quote != "" {
		label := "rc.model_said"
		if toolDetail[c.Code] {
			label = "rc.details"
		}
		v.QuoteLabel, _ = i18n.Lookup(lang, label)
	}
	return v
}

// toolDetail lists the codes whose detail is an error from a tool or a
// check, not a model's words.
var toolDetail = map[string]bool{
	"scene_answer_rejected": true, "flow_locator_failed": true, "flow_tracking_failed": true, "velocity_unavailable": true,
}

// fixedDetails are the texts perception's pipeline itself writes into a
// detail (agent.py), with their catalogue keys. Anything else in a detail
// was written by a model, or is an error, and is shown as it is.
var fixedDetails = map[string]string{
	"time budget reached":                                      "time_budget_reached",
	"partial (time budget reached)":                            "partial_time_budget_reached",
	"the scene model's answers failed their checks":            "answers_failed_checks",
	"verification failed":                                      "verification_failed",
	"confirmation failed":                                      "confirmation_failed",
	"no wheel-like proposal in the vehicle box":                "no_wheel_proposal",
	"no prompt on the water":                                   "no_prompt_on_water",
	"outline and the tyre's box disagree":                      "outline_box_disagree",
	"outline follows the rim, inside the tyre":                 "outline_inside_rim",
	"outline is not the tyre's outer edge":                     "outline_not_outer_edge",
	"outline accepted by the scene model but not wheel-shaped": "accepted_not_wheel_shaped",
	"not the named object":                                     "not_the_named_object",
	"not water":                                                "not_water",
	"no mask":                                                  "no_mask",
}

func fixedDetail(lang, detail string) (string, bool) {
	id, ok := fixedDetails[detail]
	if !ok {
		return "", false
	}
	return i18n.Lookup(lang, "rc.detail."+id)
}

// StepDecision renders one step decision from its code. ok is false when
// there is no code, or one this gateway cannot render; the caller then
// shows perception's English decision as before.
func StepDecision(lang string, c *upstream.Code) (Decision, bool) {
	if c == nil || c.Code == "" || c.Code == "other" {
		return Decision{}, false
	}
	args := strArgs(c.Args)
	d := Decision{Code: c.Code, Tone: "neutral"}
	key := "dc." + c.Code
	switch c.Code {
	case "accept", "accept_partial":
		d.Tone = "ok"
	case "refine":
		d.Tone = "refine"
		if box := args["box"]; box != "" {
			d.Detail = box + " px"
			if pts := args["points"]; pts != "" {
				label, _ := i18n.Lookup(lang, "dc.points")
				d.Detail += " + " + label + " " + strings.TrimSpace(pointWord.ReplaceAllString(pts, ""))
			}
		}
	case "give_up":
		d.Tone = "stop"
	case "give_up_reason":
		d.Tone = "stop"
		if t, ok := fixedDetail(lang, args["detail"]); ok {
			d.Detail = t
		} else {
			d.Quote = args["detail"]
		}
	case "verify_best":
		switch args["n"] {
		case "one":
			args["n"] = "1"
		case "two":
			args["n"] = "2"
		}
	case "scene_search_vehicles":
		if args["wheels"] != "" {
			key = "dc.scene_search_vehicles_and"
		}
	}
	text, ok := fill(lang, key, args)
	if !ok {
		return Decision{}, false
	}
	d.Text = text
	return d, true
}

// targetRe matches a trace target such as "two-wheeler wheel 1" or
// "two wheeler wheel 1" (the agent writes the kind with spaces), and
// vehicleWheelRe a wheel found in a vehicle's box, "two wheeler 1 wheel".
var (
	targetRe       = regexp.MustCompile(`^(two[ -]wheeler wheel|car wheel|auto(?:-rickshaw)? wheel|bus wheel|kerb|person) (\d+)$`)
	vehicleWheelRe = regexp.MustCompile(`^(two[ -]wheeler|car|auto(?:-rickshaw)?|bus) (\d+) wheel$`)
)

var targetKinds = map[string]string{
	"two-wheeler wheel": "two_wheeler_wheel", "two wheeler wheel": "two_wheeler_wheel",
	"car wheel": "car_wheel", "auto wheel": "auto_wheel", "auto-rickshaw wheel": "auto_wheel",
	"bus wheel": "bus_wheel", "kerb": "kerb", "person": "person",
}

// Target translates a trace target of the form "<kind> <n>" or
// "<vehicle> <n> wheel"; any other target (such as "water") is returned as
// it is.
func Target(lang, target string) string {
	m := targetRe.FindStringSubmatch(target)
	if m == nil {
		if m = vehicleWheelRe.FindStringSubmatch(target); m == nil {
			return target
		}
		m[1] += " wheel"
	}
	kind, ok := word(lang, "rc.kind.", targetKinds[m[1]])
	if !ok {
		return target
	}
	text, ok := fill(lang, "rc.target", map[string]string{"kind": kind, "n": m[2]})
	if !ok {
		return target
	}
	return text
}

var (
	placeholder = regexp.MustCompile(`\{([a-z_]+)\}`)
	pointWord   = regexp.MustCompile(`\bpoints?\b`)
)

// Placeholders lists the named placeholders in a message, in order.
func Placeholders(msg string) []string {
	var out []string
	for _, m := range placeholder.FindAllStringSubmatch(msg, -1) {
		out = append(out, m[1])
	}
	return out
}

// fill looks key up and replaces each {name} with args[name]. It fails
// when the key is unknown or an argument the message needs is missing.
func fill(lang, key string, args map[string]string) (string, bool) {
	msg, ok := i18n.Lookup(lang, key)
	if !ok {
		return "", false
	}
	missing := false
	out := placeholder.ReplaceAllStringFunc(msg, func(m string) string {
		v, ok := args[m[1:len(m)-1]]
		if !ok {
			missing = true
		}
		return v
	})
	return out, !missing
}

// word translates a word argument: prefix + value, with spaces and dashes
// in the value read as underscores ("mid thigh" is rc.landmark.mid_thigh).
func word(lang, prefix, value string) (string, bool) {
	if value == "" {
		return "", false
	}
	id := strings.NewReplacer(" ", "_", "-", "_").Replace(value)
	return i18n.Lookup(lang, prefix+id)
}

// strArgs writes every argument as text: numbers as perception wrote them
// (23.4 stays 23.4, 55 stays 55), strings as they are.
func strArgs(in map[string]any) map[string]string {
	out := make(map[string]string, len(in))
	for k, v := range in {
		switch x := v.(type) {
		case string:
			out[k] = x
		case float64:
			out[k] = strconv.FormatFloat(x, 'f', -1, 64)
		case int:
			out[k] = strconv.Itoa(x)
		case bool:
			out[k] = strconv.FormatBool(x)
		case nil:
		default:
			// A nested value no template uses; leave it out so a message
			// that needs it falls back to English.
		}
	}
	return out
}

func capitalise(s string) string {
	if s == "" {
		return s
	}
	r := []rune(s)
	return strings.ToUpper(string(r[:1])) + string(r[1:])
}

// Check names a certificate lists (CONTRACTS.md 1.1), in the page language;
// a name this gateway does not know is returned as it is.
func Check(lang, name string) string {
	if t, ok := word(lang, "rc.check.", name); ok {
		return t
	}
	return name
}

// decisionPatterns is perception's decision catalogue (codes.py DECISIONS),
// for traces recorded without codes: the landing page's sample run and
// certificates kept before codes were. Named groups become arguments; the
// first full match wins.
var decisionPatterns = func() []struct {
	code string
	re   *regexp.Regexp
} {
	table := [][2]string{
		{"accept", `accept`},
		{"accept_partial", `accept \(coverage partial\)`},
		{"refine", `refine with box (?P<box>\[[-\d, ]+\]) px(?: \+ (?P<points>.+))?`},
		{"give_up", `give up`},
		{"give_up_reason", `give up: (?P<detail>.+)`},
		{"verify", `verify`},
		{"verify_best", `verify the best (?P<n>one|two)`},
		{"measure", `measure`},
		{"candidate", `candidate`},
		{"declined", `declined`},
		{"rejected", `rejected`},
		{"retry", `retry`},
		{"drop", `drop`},
		{"drop_fields", `drop (?P<n>\d+) fields? that failed, use the rest`},
		{"snap", `snap`},
		{"scene_stop_no_water", `stop: no water`},
		{"scene_stop_no_wheel", `stop: no wheel`},
		{"scene_stop_unchecked", `stop: no checked scene reading`},
		{"scene_report_estimate", `no wheel: report own estimate`},
		{"scene_measure_persons", `no wheel: measure (?P<n>\d+) persons? against the stature prior and segment the water`},
		{"scene_verify_wheels", `segment and verify (?P<n>\d+) wheels? and the water`},
		{"scene_search_vehicles", `search (?P<vehicles>\d+) vehicle box(?:es)? for wheels(?:, check (?P<wheels>\d+) wheels?)? and segment the water`},
		{"join_water", `join to the verified water`},
		{"check_water_below", `check that each outline ends at water`},
		{"measure_people", `measure each against the stature prior`},
		{"use_measurement", `use the measurement`},
		{"use_wheel", `use the wheel`},
		{"use_person", `use the person`},
		{"use_person_n", `use person (?P<person>\d+)`},
		{"combine_people", `combine the people who agree`},
		{"report_estimate", `report model estimate`},
		{"shallow_not_trusted", `shallow reading not trusted: report the estimate`},
		{"person_not_published", `do not publish a person measurement`},
		{"budget_answer", `answer with what was verified in time`},
		{"budget_skip_people", `skip the people`},
		{"budget_keep_water", `keep the verified water as it is`},
		{"budget_verified_water_only", `use the verified water only`},
		{"track", `track`},
		{"flow_stop_nothing_floating", `stop: nothing floating to track`},
		{"check_tracks", `check the tracks`},
		{"subtract_camera_motion", `subtract its motion from every track`},
		{"camera_motion_kept", `camera motion not removed`},
		{"report_velocity", `report velocity`},
		{"no_speed", `no speed`},
		{"no_speed_rejected", `no speed \((?P<rejected>\d+) tracks rejected\)`},
	}
	out := make([]struct {
		code string
		re   *regexp.Regexp
	}, len(table))
	for i, t := range table {
		out[i].code, out[i].re = t[0], regexp.MustCompile("^(?:"+t[1]+")$")
	}
	return out
}()

// DecisionFromEnglish reads a step decision perception wrote in English
// into its code, as perception's own catalogue would; nil when it matches
// none (the decision is then shown as it is).
func DecisionFromEnglish(text string) *upstream.Code {
	text = strings.TrimSpace(text)
	for _, p := range decisionPatterns {
		m := p.re.FindStringSubmatch(text)
		if m == nil {
			continue
		}
		args := map[string]any{}
		for i, name := range p.re.SubexpNames() {
			if name == "" || m[i] == "" {
				continue
			}
			if f, err := strconv.ParseFloat(m[i], 64); err == nil && !strings.HasPrefix(m[i], "[") {
				args[name] = f
			} else {
				args[name] = m[i]
			}
		}
		return &upstream.Code{Code: p.code, Args: args}
	}
	return nil
}
