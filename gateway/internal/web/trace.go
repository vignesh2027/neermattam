package web

import (
	"fmt"
	"sort"
	"strings"

	"neermattam/gateway/internal/i18n"
	"neermattam/gateway/internal/upstream"
)

// TraceStepView is one step of perception's agent, ready to draw.
type TraceStepView struct {
	Index    int
	Kind     string // scene, segment, verify, confirm, refine, giveup, geometry, estimate, budget, validate, other
	Label    string
	Target   string
	Model    string
	Input    string
	Output   string
	Decision string // short label: Accept, Refine, Give up, ...
	Detail   string // the rest of the decision, such as the new box
	Tone     string // ok, refine, stop, neutral
	Latency  string
	Prompt   string // the prompt version a language-model step was sent
	Slow     bool   // over 10 seconds
	DelayMs  int    // reveal stagger when the trace arrives all at once
}

// TraceView is the whole agent trace.
type TraceView struct {
	Steps       []TraceStepView
	Live        bool
	Reveal      bool // stagger the steps in, when they arrived all at once
	Count       int
	Refines     int
	GiveUps     int
	Accepts     int
	TotalTime   string
	ModelsUsed  []string
	TargetsSeen int
}

var stepKinds = map[string]bool{
	"scene": true, "segment": true, "verify": true, "confirm": true,
	"refine": true, "giveup": true, "geometry": true, "estimate": true,
	"budget":   true, // perception's time budget stopped the checks still open
	"validate": true, // a model answer failed a deterministic check
}

// NewTraceStep classifies one step. A verify whose decision is a refinement
// or a give-up is shown as that, because that is what the agent did.
func NewTraceStep(lang string, i int, s upstream.AgentStep) TraceStepView {
	v := TraceStepView{
		Index: i + 1, Target: s.Target, Model: s.Model, Input: s.Input, Output: s.Output,
		Latency: latency(s.LatencyMs), Slow: s.LatencyMs >= 10_000, DelayMs: min(i*70, 1400),
		Prompt: s.PromptVersion,
	}
	kind := strings.ToLower(strings.TrimSpace(s.Step))
	dec := strings.TrimSpace(s.Decision)
	low := strings.ToLower(dec)
	switch {
	case strings.HasPrefix(low, "give up"):
		v.Tone = "stop"
		v.Decision = i18n.T(lang, "decision.giveup")
		v.Detail = strings.TrimSpace(strings.TrimPrefix(strings.TrimPrefix(dec[len("give up"):], ":"), " "))
		if kind == "verify" || kind == "confirm" {
			kind = "giveup"
		}
	case strings.HasPrefix(low, "refine"):
		v.Tone = "refine"
		v.Decision = i18n.T(lang, "decision.refine")
		v.Detail = strings.TrimSpace(dec[len("refine"):])
		if kind == "verify" || kind == "confirm" {
			kind = "refine"
		}
	case strings.HasPrefix(low, "accept"):
		v.Tone = "ok"
		v.Decision = i18n.T(lang, "decision.accept")
		v.Detail = strings.Trim(strings.TrimSpace(dec[len("accept"):]), "()")
	default:
		v.Tone = "neutral"
		v.Decision = capitalise(dec)
		if dec != "" && (kind == "scene" || kind == "segment") {
			v.Decision = i18n.T(lang, "decision.next", dec)
		}
	}
	if !stepKinds[kind] {
		kind = "other"
	}
	v.Kind = kind
	if kind == "other" {
		v.Label = capitalise(s.Step)
	} else {
		v.Label = i18n.T(lang, "step."+kind)
	}
	return v
}

// NewTraceView builds the panel for a complete trace.
func NewTraceView(lang string, steps []upstream.AgentStep, live bool) *TraceView {
	t := &TraceView{Live: live, Reveal: true, Count: len(steps)}
	models := map[string]bool{}
	targets := map[string]bool{}
	total := 0
	for i, s := range steps {
		v := NewTraceStep(lang, i, s)
		switch v.Tone {
		case "refine":
			t.Refines++
		case "stop":
			t.GiveUps++
		case "ok":
			t.Accepts++
		}
		if s.Model != "" {
			models[s.Model] = true
		}
		if s.Target != "" && s.Target != "photo" && s.Target != "scene" {
			targets[s.Target] = true
		}
		total += s.LatencyMs
		t.Steps = append(t.Steps, v)
	}
	for m := range models {
		t.ModelsUsed = append(t.ModelsUsed, m)
	}
	sort.Strings(t.ModelsUsed)
	t.TargetsSeen = len(targets)
	t.TotalTime = latency(total)
	return t
}

func latency(ms int) string {
	switch {
	case ms <= 0:
		return "0 ms"
	case ms < 1000:
		return fmt.Sprintf("%d ms", ms)
	case ms < 60_000:
		return fmt.Sprintf("%.1f s", float64(ms)/1000)
	default:
		return fmt.Sprintf("%d min %d s", ms/60_000, (ms%60_000)/1000)
	}
}
