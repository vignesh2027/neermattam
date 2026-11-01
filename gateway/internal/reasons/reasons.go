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
	// Quote is text a model or a tool wrote, shown as it is, in quotes.
	Quote string
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
	return View{Text: capitalise(text), Quote: quote}
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
		d.Quote = args["detail"]
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
// "two wheeler wheel 1" (the agent writes the kind with spaces).
var targetRe = regexp.MustCompile(`^(two[ -]wheeler wheel|car wheel|auto(?:-rickshaw)? wheel|bus wheel|kerb|person) (\d+)$`)

var targetKinds = map[string]string{
	"two-wheeler wheel": "two_wheeler_wheel", "two wheeler wheel": "two_wheeler_wheel",
	"car wheel": "car_wheel", "auto wheel": "auto_wheel", "auto-rickshaw wheel": "auto_wheel",
	"bus wheel": "bus_wheel", "kerb": "kerb", "person": "person",
}

// Target translates a trace target of the form "<kind> <n>"; any other
// target (such as "car 1 wheel" or "water") is returned as it is.
func Target(lang, target string) string {
	m := targetRe.FindStringSubmatch(target)
	if m == nil {
		return target
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
