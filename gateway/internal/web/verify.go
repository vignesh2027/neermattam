package web

import (
	"fmt"
	"strings"

	"neermattam/gateway/internal/certs"
	"neermattam/gateway/internal/i18n"
	"neermattam/gateway/internal/reasons"
	"neermattam/gateway/internal/upstream"
)

// VerifyView is the "Verify this measurement" page.
type VerifyView struct {
	ID      string
	Missing bool // the gateway holds no certificate with this id

	Status string // verified, invalid, unknown_key
	Reason string // the check's own English sentence, shown as a detail
	KeyID  string

	// Decision is what this gateway did with the measurement: fused,
	// rejected, unusable, or unknown when no decision was recorded. The
	// signature says who made the record; the decision says whether the
	// map uses it.
	Decision        string
	DecisionTrust   string
	DecisionAt      string
	DecisionReasons []reasons.View
	// Archival is set for a measured archival photo (the map's "Measured
	// photos" layer): never added to the live map.
	Archival *ArchivalView

	// Upload is "match", "mismatch" or "unknown": does the certificate's
	// input hash equal the SHA-256 of the file the gateway received?
	Upload     string
	UploadSHA  string
	InputSHA   string
	InputSize  string
	InputKind  string
	IssuedAt   string
	ReceivedAt string

	HasRecord  bool // the record could be read (verified, or shown as unverified claims)
	Usable     bool
	Method     string // geometry, person, estimate or ""
	MethodText string
	Depth      string
	Sigma      string
	Confidence string
	Reasons    []reasons.View

	Models   []VerifyModel
	Prompts  []VerifyPrompt
	Steps    []VerifyStep
	Rules    []string
	Failures []string
	Numbers  []Fact

	RawURL    string
	KeysURL   string
	ReplayCmd string
	Pretty    string
}

// VerifyModel is one model and its exact version.
type VerifyModel struct{ Role, Name, Version string }

// VerifyPrompt is one prompt version and its full hash.
type VerifyPrompt struct{ ID, SHA string }

// VerifyStep is one agent step as the certificate records it.
type VerifyStep struct {
	Index                           int
	Kind, Label, Target, Model      string
	Prompt, Decision, Latency, Tone string
	Detail, Quote                   string
}

// NewVerifyView builds the page for a stored certificate and its check.
func NewVerifyView(lang, id string, st certs.Stored, res certs.Result, publicURL string) *VerifyView {
	tr := func(key string, args ...any) string { return i18n.T(lang, key, args...) }
	base := strings.TrimRight(publicURL, "/")
	v := &VerifyView{
		ID: id, Status: res.Status, Reason: res.Reason, KeyID: res.KeyID,
		UploadSHA: st.UploadSHA256, RawURL: "/api/certificates/" + id,
		KeysURL:    "/.well-known/neermattam-keys.json",
		ReceivedAt: When(lang, st.ReceivedAt),
	}
	if st.ReceivedDateOnly {
		// Only the date is known: midnight UTC is not a time anything happened.
		v.ReceivedAt = Day(lang, st.ReceivedAt)
	}
	v.ReplayCmd = fmt.Sprintf("curl -4 -s %s/api/certificates/%s -o %s.json\n"+
		"cd perception && uv run python -m neermattam_perception.replay ../%s.json \\\n"+
		"    --keys %s/.well-known/neermattam-keys.json", base, id, id, id, base)
	v.Decision = "unknown"
	if d := st.Decision; d != nil {
		switch d.Status {
		case certs.DecisionFused, certs.DecisionRejected, certs.DecisionUnusable, certs.DecisionSample, certs.DecisionArchival:
			v.Decision = d.Status
		}
		v.DecisionTrust = fmt.Sprintf("%.2f", d.Trust)
		if !d.DecidedAt.IsZero() {
			v.DecisionAt = When(lang, d.DecidedAt)
		}
		for _, r := range d.Reasons {
			if r.Code != nil {
				v.DecisionReasons = append(v.DecisionReasons, reasons.Reason(lang, r.Text[i18n.Default], r.Code))
				continue
			}
			text := r.Text[lang]
			if text == "" {
				text = r.Text[i18n.Default]
			}
			if text != "" {
				v.DecisionReasons = append(v.DecisionReasons, reasons.View{Text: text})
			}
		}
	}
	rec, pretty := res.Record, res.Pretty
	if rec == nil {
		rec, pretty = certs.ReadUnverified(st.Certificate)
	}
	v.Pretty = pretty
	v.Upload = "unknown"
	if rec == nil {
		return v
	}
	v.HasRecord = true
	if rec.Input.SHA256 != nil {
		v.InputSHA = *rec.Input.SHA256
		if st.UploadSHA256 != "" {
			v.Upload = "mismatch"
			if strings.EqualFold(v.InputSHA, st.UploadSHA256) {
				v.Upload = "match"
			}
		}
	}
	v.InputKind = rec.Input.Kind
	if rec.Input.Width > 0 {
		v.InputSize = fmt.Sprintf("%d x %d px", rec.Input.Width, rec.Input.Height)
	}
	v.IssuedAt = rec.IssuedAt
	d := rec.Decision
	v.Usable = d.Usable
	method := ""
	if d.Method != nil {
		method = upstream.NormalizeMethod(*d.Method)
	}
	switch method {
	case upstream.MethodWheelGeometry:
		v.Method, v.MethodText = "geometry", tr("vfy.method_wheel")
	case upstream.MethodPersonReference:
		v.Method, v.MethodText = "person", tr("result.person_chip")
	case upstream.MethodModelEstimate:
		v.Method, v.MethodText = "estimate", tr("result.model_estimate")
	}
	if d.DepthCm != nil && d.DepthSigmaCm != nil {
		v.Depth, v.Sigma = fmt.Sprintf("%.1f", *d.DepthCm), fmt.Sprintf("%.1f", *d.DepthSigmaCm)
	}
	v.Confidence = fmt.Sprintf("%.2f", d.Confidence)
	v.Reasons = reasons.List(lang, d.Reasons, st.ReasonCodes)
	for _, m := range rec.Models {
		role := m.Role
		switch m.Role {
		case "scene":
			role = tr("vfy.role_scene")
		case "masks":
			role = tr("vfy.role_masks")
		}
		v.Models = append(v.Models, VerifyModel{role, m.Name, m.Version})
	}
	for _, p := range rec.Prompts {
		sha := ""
		if p.SHA256 != nil {
			sha = *p.SHA256
		}
		v.Prompts = append(v.Prompts, VerifyPrompt{p.ID, sha})
	}
	for i, s := range rec.Steps {
		step := upstream.AgentStep{Step: s.Step, Target: s.Target, Model: s.Model, Decision: s.Decision, LatencyMs: s.LatencyMs}
		if len(st.StepCodes) == len(rec.Steps) {
			step.DecisionCode = st.StepCodes[i]
		}
		ts := NewTraceStep(lang, i, step)
		vs := VerifyStep{Index: i + 1, Kind: ts.Kind, Label: ts.Label, Target: ts.Target, Model: s.Model, Decision: s.Decision, Latency: ts.Latency, Tone: ts.Tone}
		code := step.DecisionCode
		if code == nil {
			code = reasons.DecisionFromEnglish(s.Decision)
		}
		if code != nil {
			if d, ok := reasons.StepDecision(lang, code); ok {
				vs.Decision, vs.Detail, vs.Quote = d.Text, d.Detail, d.Quote
			}
		}
		if s.PromptVersion != nil {
			vs.Prompt = *s.PromptVersion
		}
		v.Steps = append(v.Steps, vs)
	}
	for _, r := range rec.Checks.Rules {
		v.Rules = append(v.Rules, reasons.Check(lang, r))
	}
	v.Failures = rec.Checks.Failures
	v.Numbers = verifyNumbers(lang, rec)
	return v
}

// verifyNumbers lists the numbers the depth was computed from. Labels are
// translated; values are numbers and symbols, the same in every language.
func verifyNumbers(lang string, rec *certs.Record) []Fact {
	tr := func(key string, args ...any) string { return i18n.T(lang, key, args...) }
	var out []Fact
	add := func(key, value string) { out = append(out, Fact{Label: tr(key), Value: value}) }
	if w := rec.Geometry.Wheel; w != nil {
		r := w.Reference
		add("vfy.n_reference", fmt.Sprintf("%s, D = %.1f ± %.2f cm (%s)", r.Kind, r.DiameterCm, r.DiameterSigmaCm, r.Source))
		e := w.Ellipse
		add("vfy.n_ellipse", fmt.Sprintf("cx %.2f, cy %.2f, a %.2f, b %.2f px, θ %.4f rad", e.Cx, e.Cy, e.A, e.B, e.Theta))
		wl := w.Waterline
		add("vfy.n_waterline", fmt.Sprintf("(%.2f, %.2f) px, (%.4f, %.4f), rms %.2f px, n = %d", wl.Point[0], wl.Point[1], wl.Direction[0], wl.Direction[1], wl.RmsPx, wl.NPoints))
		if w.Camera != nil {
			add("vfy.n_focal", fmt.Sprintf("%.1f px", w.Camera.FocalPx))
		}
		add("vfy.n_arc", fmt.Sprintf("%.0f°", w.ArcCoveredDeg))
		add("vfy.n_fraction", fmt.Sprintf("f = %.4f %+.4f = %.4f", w.FractionRaw, -w.FractionBias, w.Fraction))
		add("vfy.n_sigma_parts", fmt.Sprintf("%.2f cm, %.3f, %.1f%% D", w.ResampleSigmaCm, w.AmbiguitySpread, 100*w.ModelSigmaShare))
		add("vfy.n_wheel_depth", fmt.Sprintf("f × D = %.2f ± %.2f cm", w.DepthCm, w.DepthSigmaCm))
		if r.SquashCm > 0 {
			add("vfy.n_squash", fmt.Sprintf("%.1f ± %.1f cm", r.SquashCm, r.SquashSigmaCm))
		}
		if w.AgreementCm != nil {
			add("vfy.n_second", fmt.Sprintf("Δ %.2f cm", *w.AgreementCm))
		}
		if w.EstimateHereCm != nil {
			add("vfy.n_estimate_here", fmt.Sprintf("%.0f cm", *w.EstimateHereCm))
		}
	}
	if p := rec.Geometry.Person; p != nil {
		add("vfy.n_rows", fmt.Sprintf("y = %.0f; %.0f, %.0f; %.0f px", p.TopY, p.ShoulderFirstY, p.ShoulderFullY, p.BottomY))
		add("vfy.n_stature", fmt.Sprintf("%.1f ± %.1f cm", p.HeightCm, p.HeightSigmaCm))
		add("vfy.n_visible", fmt.Sprintf("%.1f%%", 100*p.VisibleShare))
		add("vfy.n_person_depth", fmt.Sprintf("%.2f ± %.2f cm", p.DepthCm, p.DepthSigmaCm))
		if p.Landmark != nil && p.LandmarkDepthCm != nil {
			add("vfy.n_landmark", fmt.Sprintf("%s, %.1f cm", *p.Landmark, *p.LandmarkDepthCm))
		}
	}
	if e := rec.Geometry.Estimate; e != nil {
		rng := ""
		if e.LowCm != nil && e.HighCm != nil {
			rng = fmt.Sprintf(" [%.0f, %.0f]", *e.LowCm, *e.HighCm)
		}
		add("vfy.n_estimate", fmt.Sprintf("%.0f cm%s, σ %.1f cm", e.DepthCm, rng, e.SigmaCm))
	}
	return out
}

// ArchivalView describes a measured archival photo on its verify page.
type ArchivalView struct {
	Year, MeasuredOn, Place, City, Credit, SourceURL, LicenceURL string
}
