package web

import (
	"encoding/base64"
	"encoding/json"
	"fmt"
	"html/template"
	"math"
	"strings"
	"time"

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
}

var ruleNames = map[string]string{
	"confidence_floor":      "Confidence",
	"photo_freshness":       "Freshness",
	"source":                "Source",
	"neighbour_agreement":   "Nearby cells",
	"rainfall_plausibility": "Rainfall",
}

// NewResultView builds the card from a pipeline outcome.
func NewResultView(o *report.Outcome) *ResultView {
	v := &ResultView{
		Status:   o.Status,
		Place:    capitalise(o.Place),
		When:     o.ReceivedAt.In(ist).Format("3:04 pm, 2 Jan"),
		Advisory: o.Advisory,
		Source:   o.Source,
		MapLink:  fmt.Sprintf("/?at=%.5f,%.5f", o.Lat, o.Lon),
	}
	m := o.Measurement
	if m != nil && m.OverlayPNGB64 != "" {
		if raw, err := base64.StdEncoding.DecodeString(m.OverlayPNGB64); err == nil && len(raw) > 8 && string(raw[1:4]) == "PNG" {
			// Only a verified PNG becomes a data URL; html/template would
			// otherwise refuse it, and rightly.
			v.Overlay = template.URL("data:image/png;base64," + m.OverlayPNGB64)
		}
	}
	if o.DepthCm != nil && o.DepthSigmaCm != nil {
		v.HasDepth = true
		v.Depth = fmt.Sprintf("%.0f", *o.DepthCm)
		v.Sigma = fmt.Sprintf("%.1f", *o.DepthSigmaCm)
		v.DepthColor = DepthColor(*o.DepthCm)
		v.Gauge = newGauge(*o.DepthCm, *o.DepthSigmaCm)
	}
	switch o.Status {
	case report.StatusFused:
		v.Title = "Added to the live map"
	case report.StatusUnusable:
		v.Title = "We could not measure this one"
		v.Message = o.Summary
		if m != nil {
			v.Reasons = m.Reasons
		}
	case report.StatusRejected:
		v.Title = "Measured, but not added to the map"
	}
	if m != nil {
		if r := m.Reference; r != nil {
			val := referenceName(r.Kind)
			note := r.Source
			if r.DiameterCm != nil {
				val += fmt.Sprintf(", %.1f cm", *r.DiameterCm)
				if r.DiameterSigmaCm != nil {
					val += fmt.Sprintf(" ± %.1f", *r.DiameterSigmaCm)
				}
			}
			v.Facts = append(v.Facts, Fact{Label: "Ruler used", Value: val, Note: note})
		}
		if w := m.Waterline; w != nil {
			note := ""
			if w.AgreementCm != nil {
				note = fmt.Sprintf("Two waterline estimates agree within %.1f cm", *w.AgreementCm)
			}
			v.Facts = append(v.Facts, Fact{Label: "Waterline", Value: methodName(w.Method), Note: note})
		}
		if m.Usable || m.Confidence > 0 {
			v.Facts = append(v.Facts, Fact{Label: "Model confidence", Value: fmt.Sprintf("%.2f", m.Confidence), Meter: pct(m.Confidence)})
		}
		if m.VelocityMps != nil {
			val := fmt.Sprintf("%.2f m/s", *m.VelocityMps)
			if m.VelocitySigmaMps != nil {
				val = fmt.Sprintf("%.2f ± %.2f m/s", *m.VelocityMps, *m.VelocitySigmaMps)
			}
			v.Facts = append(v.Facts, Fact{Label: "Surface speed", Value: val, Note: "From tracking the water between frames"})
		}
		v.ModelsLine = modelsLine(m)
	}
	if o.Voice != nil {
		v.Facts = append(v.Facts, Fact{Label: "Heard", Value: o.Voice.Words, Note: "Read as " + o.Voice.Basis})
	}
	if o.Transcript != nil {
		v.Transcript = o.Transcript.Text
	}
	if vr := o.Verification; vr != nil {
		v.Trust = fmt.Sprintf("%.2f", vr.Trust)
		v.TrustPct = pct(vr.Trust)
		for _, r := range vr.Rules {
			factor := ""
			if r.Outcome == verify.Lower {
				factor = fmt.Sprintf("× %.2f", r.Factor)
			}
			name := ruleNames[r.Rule]
			if name == "" {
				name = humanize(r.Rule)
			}
			v.Rules = append(v.Rules, RuleView{Name: name, Outcome: string(r.Outcome), Reason: capitalise(r.Reason), Factor: factor})
		}
	}
	if c := o.Cell; c != nil {
		v.Cell = &CellView{
			Depth: fmt.Sprintf("%.0f", c.DepthCm), Sigma: fmt.Sprintf("%.1f", c.DepthSigmaCm), Upper: fmt.Sprintf("%.0f", c.DepthUpperCm),
			Hazard: HazardLabel(c.Hazard.Class), HazardChip: HazardChip(c.Hazard.Class), Readings: c.NReadings, Unknown: c.Unknown,
			Simulated: o.CellHasSimulated,
		}
	}
	return v
}

// ErrorView is the card for a report that could not be processed.
func ErrorView(msg string) *ResultView {
	return &ResultView{Status: "error", Title: "This report was not processed", Message: msg, Advisory: report.Advisory}
}

func referenceName(k string) string {
	switch k {
	case "two_wheeler_wheel":
		return "Two-wheeler wheel"
	case "auto_wheel":
		return "Auto-rickshaw wheel"
	}
	return humanize(k)
}

func methodName(m string) string {
	switch m {
	case "sam_mask_intersection":
		return "Where the wheel and water masks meet"
	case "":
		return "Not reported"
	}
	return humanize(m)
}

func modelsLine(m *upstream.Measurement) string {
	var parts []string
	for _, k := range []string{"scene", "masks"} {
		if name, ok := m.Models[k]; ok {
			parts = append(parts, name)
		}
	}
	var times []string
	for _, k := range []string{"scene", "masks", "geometry"} {
		if ms, ok := m.TimingsMs[k]; ok {
			if ms >= 1000 {
				times = append(times, fmt.Sprintf("%s %.1f s", k, ms/1000))
			} else {
				times = append(times, fmt.Sprintf("%s %.0f ms", k, ms))
			}
		}
	}
	line := strings.Join(parts, " and ")
	if len(times) > 0 {
		line += ". Timings: " + strings.Join(times, ", ")
	}
	if m.MeasurementID != "" {
		line += ". Measurement " + m.MeasurementID
	}
	return line
}

func newGauge(depth, sigma float64) Gauge {
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
		{Pct: fmt.Sprintf("%.1f%%", 15/maxCm*100), Label: "Car, auto and two-wheeler limit, 15 cm", Row2: true},
		{Pct: fmt.Sprintf("%.1f%%", 30/maxCm*100), Label: "Bus limit, 30 cm"},
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
func HazardLabel(c string) string {
	switch c {
	case "low":
		return "Caution"
	case "danger_some":
		return "Danger for some"
	case "danger_most":
		return "Danger for most"
	case "danger_all":
		return "Danger for all"
	default:
		return "Unknown"
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
	return strings.ToUpper(s[:1]) + s[1:]
}

var ist = time.FixedZone("IST", 5*3600+30*60)

// Ago formats a past time for people.
func Ago(t time.Time) string {
	d := time.Since(t)
	switch {
	case d < time.Minute:
		return "just now"
	case d < time.Hour:
		return fmt.Sprintf("%d min ago", int(d.Minutes()))
	case d < 24*time.Hour:
		return fmt.Sprintf("%.0f h ago", d.Hours())
	default:
		return t.In(ist).Format("2 Jan")
	}
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

// VehicleNames are the UI names for engine vehicle ids.
var VehicleNames = map[string]string{
	"pedestrian":  "Walking",
	"two_wheeler": "Two-wheeler",
	"auto":        "Auto",
	"car":         "Car",
	"bus":         "Bus",
}

// NewRouteView builds the summary from the engine's route Feature.
func NewRouteView(raw []byte, vehicle string, simulated bool) (*RouteView, error) {
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
		Vehicle: vehicle, VehicleName: VehicleNames[vehicle], Kind: p.RouteKind, Passable: p.Passable,
		Simulated: simulated, Explanation: p.Explanation,
	}
	extra := p.DistanceM - b.DistanceM
	distNote := "Same as the shortest route"
	if extra > 1 {
		distNote = fmt.Sprintf("%s longer than the shortest route", meters(extra))
	}
	v.Stats = append(v.Stats,
		RouteStat{Label: "Distance", Value: km(p.DistanceM), Note: distNote},
		RouteStat{Label: "Time, rough", Value: fmt.Sprintf("%.0f min", math.Max(1, p.EtaMin)), Note: fmt.Sprintf("Shortest route %.0f min", math.Max(1, b.EtaMin))},
	)
	if p.MaxDepthCm != nil {
		note := "Deepest known cell on the route"
		val := fmt.Sprintf("%.0f cm", *p.MaxDepthCm)
		if p.MaxDepthSigmaCm != nil {
			val = fmt.Sprintf("%.0f ± %.0f cm", *p.MaxDepthCm, *p.MaxDepthSigmaCm)
		}
		v.Stats = append(v.Stats, RouteStat{Label: "Max depth on route", Value: val, Note: note, Warn: !p.Passable})
	} else {
		v.Stats = append(v.Stats, RouteStat{Label: "Max depth on route", Value: "None known", Note: "No cell on it has a confident depth"})
	}
	v.Stats = append(v.Stats,
		RouteStat{Label: "Flooded cells avoided", Value: fmt.Sprintf("%d", p.CellsAvoided), Note: fmt.Sprintf("Shortest route crosses %d impassable", b.Impassable)},
		RouteStat{Label: "Unknown cells crossed", Value: fmt.Sprintf("%d of %d", p.UnknownCellsCrossed, p.CellsOnRoute), Note: "No recent report, so not known to be dry"},
		RouteStat{Label: "Impassable cells on route", Value: fmt.Sprintf("%d", p.Impassable), Warn: p.Impassable > 0, Note: passNote(p.Passable)},
	)
	switch {
	case p.DepthLimitCm != nil:
		v.Limit = fmt.Sprintf("Blocks water that may be deeper than %.0f cm for a %s, judged on the mean plus %.1f standard deviations.", *p.DepthLimitCm, strings.ToLower(v.VehicleName), p.SafetySigmas)
	case p.PedestrianBlock != nil:
		v.Limit = fmt.Sprintf("Walkers are routed around cells rated %s or worse.", strings.ToLower(HazardLabel(*p.PedestrianBlock)))
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

func passNote(ok bool) string {
	if ok {
		return "A dry route under this vehicle's limit exists"
	}
	return "No dry route exists; this is the least bad"
}

func km(m float64) string {
	if m < 1000 {
		return fmt.Sprintf("%.0f m", m)
	}
	return fmt.Sprintf("%.1f km", m/1000)
}

func meters(m float64) string {
	if m < 1000 {
		return fmt.Sprintf("%.0f m", m)
	}
	return fmt.Sprintf("%.1f km", m/1000)
}
