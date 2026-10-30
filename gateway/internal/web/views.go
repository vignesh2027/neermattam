package web

import (
	"encoding/json"
	"fmt"
	"html/template"
	"math"
	"strings"

	"neermattam/gateway/internal/i18n"
	"neermattam/gateway/internal/report"
	"neermattam/gateway/internal/upstream"
	"neermattam/gateway/internal/verify"
)

// Fact is one labelled value in the result card.
type Fact struct {
	Label, Value, Note string
	Meter              string // CSS width for a 0 to 1 meter, empty for none
}

// Tick marks a reference depth on the gauge.
type Tick struct {
	Pct   string
	Label string
	Row2  bool
}

// Gauge places a depth and its uncertainty on a 0 to Max cm bar.
type Gauge struct {
	Max                       int
	MarkPct, LowPct, WidthPct string
	Ticks                     []Tick
	// The scene model's own estimate, drawn as a second band when it
	// accompanies a geometry measurement.
	HasEstimate                     bool
	EstMarkPct, EstLowPct, EstWidth string
}

// RuleView is one verification rule for display.
type RuleView struct {
	Name, Outcome, Reason, Factor string
}

// CellView is the engine's fused cell after the report.
type CellView struct {
	Depth, Sigma, Upper string
	Hazard, HazardChip  string
	Readings            int
	Unknown             bool
	Simulated           bool
}

// ResultView is everything the result card shows.
type ResultView struct {
	Status     string // fused, unusable, rejected, error
	Title      string
	Message    string
	HasDepth   bool
	Depth      string
	Sigma      string
	DepthColor string
	Place      string
	When       string
	Source     string
	Gauge      Gauge
	Overlay    template.URL
	Facts      []Fact
	Reasons    []string
	Rules      []RuleView
	Trust      string
	TrustPct   string
	Cell       *CellView
	MapLink    string
	Advisory   string
	Transcript string
	ModelsLine string

	// Method badge: "geometry", "person", "estimate" or "" (older
	// perception). A method the gateway does not know shows as "estimate".
	Method      string
	MethodLabel string
	// EstimateNote explains a model estimate in plain words.
	EstimateNote string
	// PersonNote explains a measurement against a person: the stature it
	// assumes and why the band is wider. PersonFaces says faces are blurred.
	PersonNote  string
	PersonFaces string
	// Notes are perception's own reasons for a usable measurement, such as
	// a wheel and a person in the same photo disagreeing.
	Notes []string
	// OverlayAlt and OverlayCaption describe the overlay image.
	OverlayAlt, OverlayCaption string
	// Readings lists the measured and the estimated depth when both exist.
	Readings []ReadingRow
	Voice    *VoiceView
	Trace    *TraceView
}

// ReadingRow is one way the depth was obtained.
type ReadingRow struct {
	Label, Value, Note string
	Primary            bool
}

// VoiceView is what was heard in a voice note.
type VoiceView struct {
	Transcript, Language, Place, DepthWords, Basis, Model string
}

// NewResultView builds the card from a pipeline outcome, in a language.
func NewResultView(o *report.Outcome, lang string) *ResultView {
	tr := func(key string, args ...any) string { return i18n.T(lang, key, args...) }
	v := &ResultView{
		Status:   o.Status,
		Place:    Place(lang, o.Place),
		When:     When(lang, o.ReceivedAt),
		Advisory: tr("advisory.long"),
		Source:   o.Source,
		MapLink:  fmt.Sprintf("/map?at=%.5f,%.5f", o.Lat, o.Lon),
	}
	m := o.Measurement
	if strings.HasPrefix(o.OverlayURL, "/api/overlay/") {
		v.Overlay = template.URL(o.OverlayURL)
	}
	if o.DepthCm != nil && o.DepthSigmaCm != nil {
		v.HasDepth = true
		v.Depth = fmt.Sprintf("%.0f", *o.DepthCm)
		v.Sigma = fmt.Sprintf("%.1f", *o.DepthSigmaCm)
		v.DepthColor = DepthColor(*o.DepthCm)
		v.Gauge = newGauge(lang, *o.DepthCm, *o.DepthSigmaCm)
	}
	describeMethod(v, o, lang)
	switch v.Method {
	case "estimate":
		v.OverlayAlt, v.OverlayCaption = tr("result.overlay_alt"), tr("result.overlay_estimate")
	case "person":
		v.OverlayAlt, v.OverlayCaption = tr("result.overlay_alt_person"), tr("result.overlay_person")
	default:
		v.OverlayAlt, v.OverlayCaption = tr("result.overlay_alt"), tr("result.overlay_measured")
	}
	switch o.Status {
	case report.StatusFused:
		v.Title = tr("result.title_fused")
	case report.StatusUnusable:
		v.Title = tr("result.title_unusable")
		if o.Source == "voice" {
			v.Message = tr("result.voice_no_depth")
		} else {
			v.Message = tr("result.photo_unusable")
		}
		if m != nil {
			v.Reasons = m.Reasons // perception's own words
		}
	case report.StatusRejected:
		v.Title = tr("result.title_rejected")
		if m != nil && m.MethodIs(upstream.MethodModelEstimate) || o.Source == "voice" {
			v.Title = tr("result.title_rejected_estimate")
		}
	}
	if m != nil {
		if m.Usable && o.Status != report.StatusUnusable && len(m.Reasons) > 0 {
			v.Notes = m.Reasons // perception's own words
		}
		if r := m.Reference; r != nil {
			val := tr("ref." + r.Kind)
			if r.DiameterCm != nil {
				val += fmt.Sprintf(", %.1f cm", *r.DiameterCm)
				if r.DiameterSigmaCm != nil {
					val += fmt.Sprintf(" ± %.1f", *r.DiameterSigmaCm)
				}
			}
			if r.IsPerson() && r.HeightCm != nil {
				sigma := "?"
				if r.HeightSigmaCm != nil {
					sigma = fmt.Sprintf("%.0f", *r.HeightSigmaCm)
				}
				// Keep "159 cm ± 9" on one line.
				val += ", " + strings.ReplaceAll(tr("fact.assumed_height", fmt.Sprintf("%.0f", *r.HeightCm), sigma), " ± ", "\u00a0±\u00a0")
				val = strings.ReplaceAll(val, " cm", "\u00a0cm")
			}
			v.Facts = append(v.Facts, Fact{Label: tr("fact.ruler"), Value: val, Note: r.Source})
		}
		if w := m.Waterline; w != nil {
			note := ""
			if w.AgreementCm != nil {
				if w.Method == "person_mask_bottom" {
					note = tr("fact.landmark_apart", fmt.Sprintf("%.0f", *w.AgreementCm))
				} else {
					note = tr("fact.waterline_agree", fmt.Sprintf("%.1f", *w.AgreementCm))
				}
			}
			v.Facts = append(v.Facts, Fact{Label: tr("fact.waterline"), Value: methodName(lang, w.Method), Note: note})
		}
		if m.Usable || m.Confidence > 0 {
			v.Facts = append(v.Facts, Fact{Label: tr("fact.confidence"), Value: fmt.Sprintf("%.2f", m.Confidence), Meter: pct(m.Confidence)})
		}
		if m.VelocityMps != nil {
			val := fmt.Sprintf("%.2f m/s", *m.VelocityMps)
			if m.VelocitySigmaMps != nil {
				val = fmt.Sprintf("%.2f ± %.2f m/s", *m.VelocityMps, *m.VelocitySigmaMps)
			}
			v.Facts = append(v.Facts, Fact{Label: tr("fact.speed"), Value: val, Note: tr("fact.speed_note")})
		}
		v.ModelsLine = modelsLine(lang, m)
	}
	if t := o.Transcript; t != nil {
		vv := &VoiceView{Transcript: t.Text, Language: languageName(lang, t.Language), Place: t.PlaceText, DepthWords: t.DepthWords, Model: t.Model}
		if o.Voice != nil {
			if strings.HasPrefix(o.Voice.Basis, "the voice model") {
				vv.Basis = tr("voice.basis_model")
			} else {
				vv.Basis = tr("voice.basis_words")
			}
		}
		vv.Basis = strings.TrimSpace(vv.Basis + " " + tr("voice.pin_note"))
		v.Voice = vv
	}
	if m != nil && len(m.AgentTrace) > 0 {
		v.Trace = NewTraceView(lang, m.AgentTrace, false)
	}
	if vr := o.Verification; vr != nil {
		v.Trust = fmt.Sprintf("%.2f", vr.Trust)
		v.TrustPct = pct(vr.Trust)
		for _, r := range vr.Rules {
			factor := ""
			if r.Outcome == verify.Lower {
				factor = fmt.Sprintf("× %.2f", r.Factor)
			}
			v.Rules = append(v.Rules, RuleView{Name: tr("rule." + r.Rule), Outcome: string(r.Outcome), Reason: ReasonText(lang, r), Factor: factor})
		}
	}
	if c := o.Cell; c != nil {
		v.Cell = &CellView{
			Depth: fmt.Sprintf("%.0f", c.DepthCm), Sigma: fmt.Sprintf("%.1f", c.DepthSigmaCm), Upper: fmt.Sprintf("%.0f", c.DepthUpperCm),
			Hazard: HazardLabel(lang, c.Hazard.Class), HazardChip: HazardChip(c.Hazard.Class), Readings: c.NReadings, Unknown: c.Unknown,
			Simulated: o.CellHasSimulated,
		}
	}
	return v
}

// ErrorView is the card for a report that could not be processed.
func ErrorView(lang string, e *report.Error) *ResultView {
	return &ResultView{Status: "error", Title: i18n.T(lang, "result.title_error"), Message: ErrorText(lang, e), Advisory: i18n.T(lang, "advisory.long")}
}

// describeMethod sets the method badge, the plain-words note for a model
// estimate, and both readings when perception gave both.
func describeMethod(v *ResultView, o *report.Outcome, lang string) {
	tr := func(key string, args ...any) string { return i18n.T(lang, key, args...) }
	m := o.Measurement
	if m == nil || m.Method == nil {
		return
	}
	est := m.ModelEstimate
	estModel := tr("result.scene_model")
	if est != nil && est.Model != "" {
		estModel = est.Model
	}
	switch m.Kind() {
	case upstream.MethodModelEstimate: // also a method this gateway does not know
		v.Method = "estimate"
		v.MethodLabel = tr("result.model_estimate")
		note := tr("result.estimate_note", estModel)
		if est != nil && est.Basis != "" {
			note += " " + tr("result.estimate_basis", strings.TrimSuffix(est.Basis, "."))
		}
		note += " " + tr("result.estimate_wide")
		v.EstimateNote = note
	case upstream.MethodWheelGeometry:
		v.Method = "geometry"
		kind := "wheel"
		if m.Reference != nil && !m.Reference.IsPerson() {
			kind = m.Reference.Kind
		}
		v.MethodLabel = tr("result.measured_from", strings.ToLower(tr("ref."+kind)))
		twoReadings(v, m, est, estModel, kind, lang)
	case upstream.MethodPersonReference:
		v.Method = "person"
		v.MethodLabel = tr("result.person_chip")
		if r := m.Reference; r != nil && r.HeightCm != nil && r.HeightSigmaCm != nil {
			v.PersonNote = tr("result.person_note", fmt.Sprintf("%.0f", *r.HeightCm), fmt.Sprintf("%.0f", *r.HeightSigmaCm))
		} else {
			v.PersonNote = tr("result.person_note_plain")
		}
		v.PersonFaces = tr("result.person_faces")
		twoReadings(v, m, est, estModel, "person", lang)
	}
}

// twoReadings lists the measurement and the scene model's own estimate
// side by side, and draws the estimate on the gauge, when both exist.
func twoReadings(v *ResultView, m *upstream.Measurement, est *upstream.ModelEstimate, estModel, kind, lang string) {
	tr := func(key string, args ...any) string { return i18n.T(lang, key, args...) }
	if est == nil || m.DepthCm == nil || m.DepthSigmaCm == nil {
		return
	}
	v.Readings = []ReadingRow{
		{Label: tr("result.reading_measured", strings.ToLower(tr("ref."+kind))), Value: fmt.Sprintf("%.0f ± %.1f cm", *m.DepthCm, *m.DepthSigmaCm), Note: tr("result.reading_used"), Primary: true},
		{Label: tr("result.reading_estimate", estModel), Value: fmt.Sprintf("%.0f ± %.0f cm", est.DepthCm, est.DepthSigmaCm), Note: capitalise(strings.TrimSuffix(est.Basis, "."))},
	}
	if math.Abs(*m.DepthCm-est.DepthCm) > 2*math.Hypot(*m.DepthSigmaCm, est.DepthSigmaCm) {
		v.Readings[1].Note += ". " + tr("result.readings_disagree")
	}
	addEstimateBand(&v.Gauge, est.DepthCm, est.DepthSigmaCm)
}

func addEstimateBand(g *Gauge, depth, sigma float64) {
	maxCm := float64(g.Max)
	lo := math.Max(0, depth-2*sigma)
	hi := math.Min(maxCm, depth+2*sigma)
	g.HasEstimate = true
	g.EstMarkPct = fmt.Sprintf("%.1f%%", math.Min(100, depth/maxCm*100))
	g.EstLowPct = fmt.Sprintf("%.1f%%", lo/maxCm*100)
	g.EstWidth = fmt.Sprintf("%.1f%%", (hi-lo)/maxCm*100)
}

func languageName(lang, code string) string {
	switch strings.ToLower(code) {
	case "ta", "en", "hi", "te", "kn", "ml", "bn", "mr":
		return i18n.T(lang, "langname."+strings.ToLower(code))
	case "ta+en", "en+ta":
		return i18n.T(lang, "langname.ta_en")
	}
	return code
}

func methodName(lang, m string) string {
	switch m {
	case "sam_mask_intersection":
		return i18n.T(lang, "fact.waterline_masks")
	case "person_mask_bottom":
		return i18n.T(lang, "fact.waterline_person")
	case "":
		return i18n.T(lang, "fact.not_reported")
	}
	return humanize(m)
}

func modelsLine(lang string, m *upstream.Measurement) string {
	var parts []string
	for _, k := range []string{"scene", "masks"} {
		if name, ok := m.Models[k]; ok {
			parts = append(parts, name)
		}
	}
	var times []string
	for _, k := range []string{"scene", "masks", "geometry"} {
		if ms, ok := m.TimingsMs[k]; ok {
			label := i18n.T(lang, "timing."+k)
			if ms >= 1000 {
				times = append(times, fmt.Sprintf("%s %.1f s", label, ms/1000))
			} else {
				times = append(times, fmt.Sprintf("%s %.0f ms", label, ms))
			}
		}
	}
	line := strings.Join(parts, ", ")
	if len(times) > 0 {
		line += ". " + i18n.T(lang, "result.timings", strings.Join(times, ", "))
	}
	if m.MeasurementID != "" {
		line += ". " + m.MeasurementID
	}
	return line
}

func newGauge(lang string, depth, sigma float64) Gauge {
	maxCm := 100.0
	if hi := depth + 2*sigma; hi > 90 {
		maxCm = math.Ceil(hi/50) * 50
	}
	lo := math.Max(0, depth-2*sigma)
	hi := math.Min(maxCm, depth+2*sigma)
	g := Gauge{
		Max:      int(maxCm),
		MarkPct:  fmt.Sprintf("%.1f%%", math.Min(100, depth/maxCm*100)),
		LowPct:   fmt.Sprintf("%.1f%%", lo/maxCm*100),
		WidthPct: fmt.Sprintf("%.1f%%", (hi-lo)/maxCm*100),
	}
	// Vehicle limits from docs/ENGINE.md.
	g.Ticks = []Tick{
		{Pct: fmt.Sprintf("%.1f%%", 15/maxCm*100), Label: i18n.T(lang, "gauge.car_limit"), Row2: true},
		{Pct: fmt.Sprintf("%.1f%%", 30/maxCm*100), Label: i18n.T(lang, "gauge.bus_limit")},
	}
	return g
}

// DepthColor maps a depth to the palette's depth scale.
func DepthColor(cm float64) string {
	switch {
	case cm < 15:
		return "#BAE6FD"
	case cm < 30:
		return "#38BDF8"
	case cm < 50:
		return "#2563EB"
	case cm < 80:
		return "#4C1D95"
	default:
		return "#9F1239"
	}
}

// HazardLabel names a Defra hazard class.
func HazardLabel(lang, c string) string {
	switch c {
	case "low", "danger_some", "danger_most", "danger_all":
		return i18n.T(lang, "hazard."+c)
	default:
		return i18n.T(lang, "hazard.unknown")
	}
}

// HazardChip is the chip class for a hazard.
func HazardChip(c string) string {
	switch c {
	case "danger_most":
		return "chip chip-violet"
	case "danger_all":
		return "chip chip-danger"
	case "low", "danger_some":
		return "chip"
	default:
		return "chip chip-grey"
	}
}

func pct(x float64) string {
	return fmt.Sprintf("%.0f%%", math.Max(0, math.Min(1, x))*100)
}

func humanize(s string) string {
	s = strings.ReplaceAll(s, "_", " ")
	return capitalise(s)
}

func capitalise(s string) string {
	if s == "" {
		return s
	}
	r := []rune(s)
	return strings.ToUpper(string(r[:1])) + string(r[1:])
}

// RouteStat is one number in the route summary.
type RouteStat struct {
	Label, Value, Note string
	Warn               bool
}

// RouteView is the route summary.
type RouteView struct {
	Vehicle     string
	VehicleName string
	Kind        string
	Passable    bool
	Simulated   bool
	Stats       []RouteStat
	Explanation string
	Limit       string
	GeoJSON     template.JS
	WatchHref   string // /alerts with this route, set by the caller
}

type routeProps struct {
	RouteKind           string   `json:"route_kind"`
	DistanceM           float64  `json:"distance_m"`
	EtaMin              float64  `json:"eta_min"`
	MaxDepthCm          *float64 `json:"max_depth_cm_on_route"`
	MaxDepthSigmaCm     *float64 `json:"max_depth_sigma_cm"`
	CellsOnRoute        int      `json:"cells_on_route"`
	CellsAvoided        int      `json:"cells_avoided"`
	UnknownCellsCrossed int      `json:"unknown_cells_crossed"`
	Impassable          int      `json:"impassable_cells_crossed"`
	Passable            bool     `json:"passable"`
	Explanation         string   `json:"explanation"`
	DepthLimitCm        *float64 `json:"depth_limit_cm"`
	PedestrianBlock     *string  `json:"pedestrian_block_class"`
	SafetySigmas        float64  `json:"safety_sigmas"`
}

// NewRouteView builds the summary from the engine's route Feature. The
// engine writes its explanation in English; other languages get a summary
// built from the same numbers.
func NewRouteView(raw []byte, vehicle string, simulated bool, lang string) (*RouteView, error) {
	tr := func(key string, args ...any) string { return i18n.T(lang, key, args...) }
	var f struct {
		Properties routeProps `json:"properties"`
		Baseline   struct {
			Properties routeProps `json:"properties"`
		} `json:"baseline"`
	}
	if err := json.Unmarshal(raw, &f); err != nil {
		return nil, err
	}
	p, b := f.Properties, f.Baseline.Properties
	v := &RouteView{
		Vehicle: vehicle, VehicleName: tr("vehicle." + vehicle), Kind: p.RouteKind, Passable: p.Passable,
		Simulated: simulated, Explanation: p.Explanation,
	}
	extra := p.DistanceM - b.DistanceM
	distNote := tr("route.same_as_shortest")
	if extra > 1 {
		distNote = tr("route.longer_by", km(extra))
	}
	v.Stats = append(v.Stats,
		RouteStat{Label: tr("route.stat_distance"), Value: km(p.DistanceM), Note: distNote},
		RouteStat{Label: tr("route.stat_time"), Value: fmt.Sprintf("%.0f min", math.Max(1, p.EtaMin)), Note: tr("route.shortest_time", fmt.Sprintf("%.0f", math.Max(1, b.EtaMin)))},
	)
	if p.MaxDepthCm != nil {
		val := fmt.Sprintf("%.0f cm", *p.MaxDepthCm)
		if p.MaxDepthSigmaCm != nil {
			val = fmt.Sprintf("%.0f ± %.0f cm", *p.MaxDepthCm, *p.MaxDepthSigmaCm)
		}
		v.Stats = append(v.Stats, RouteStat{Label: tr("route.stat_max_depth"), Value: val, Note: tr("route.deepest_known"), Warn: !p.Passable})
	} else {
		v.Stats = append(v.Stats, RouteStat{Label: tr("route.stat_max_depth"), Value: tr("route.none_known"), Note: tr("route.no_confident")})
	}
	pass := tr("route.pass_yes")
	if !p.Passable {
		pass = tr("route.pass_no")
	}
	v.Stats = append(v.Stats,
		RouteStat{Label: tr("route.stat_avoided"), Value: fmt.Sprintf("%d", p.CellsAvoided), Note: tr("route.baseline_crosses", b.Impassable)},
		RouteStat{Label: tr("route.stat_unknown"), Value: tr("route.n_of_m", p.UnknownCellsCrossed, p.CellsOnRoute), Note: tr("route.unknown_meaning")},
		RouteStat{Label: tr("route.stat_impassable"), Value: fmt.Sprintf("%d", p.Impassable), Warn: p.Impassable > 0, Note: pass},
	)
	switch {
	case p.DepthLimitCm != nil:
		v.Limit = tr("route.limit_depth", fmt.Sprintf("%.0f", *p.DepthLimitCm), v.VehicleName, fmt.Sprintf("%.1f", p.SafetySigmas))
	case p.PedestrianBlock != nil:
		v.Limit = tr("route.limit_walk", HazardLabel(lang, *p.PedestrianBlock))
	}
	if lang != i18n.Default {
		// The engine's explanation is English prose; say the same facts.
		switch {
		case p.RouteKind == "least_bad":
			v.Explanation = tr("route.explain_least_bad", p.Impassable)
		case p.CellsAvoided > 0:
			v.Explanation = tr("route.explain_avoids", p.CellsAvoided)
		default:
			v.Explanation = tr("route.explain_clear")
		}
		v.Explanation += " " + tr("route.explain_unknown", p.UnknownCellsCrossed, p.CellsOnRoute) + " " + tr("advisory.long")
	}
	// Re-encode so the JSON is safe inside a script element.
	var anyJSON any
	if err := json.Unmarshal(raw, &anyJSON); err != nil {
		return nil, err
	}
	safe, err := json.Marshal(anyJSON)
	if err != nil {
		return nil, err
	}
	v.GeoJSON = template.JS(safe)
	return v, nil
}

func km(m float64) string {
	if m < 1000 {
		return fmt.Sprintf("%.0f m", m)
	}
	return fmt.Sprintf("%.1f km", m/1000)
}
