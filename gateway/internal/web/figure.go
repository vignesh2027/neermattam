package web

import (
	"fmt"
	"math"

	"neermattam/gateway/internal/i18n"
)

// DepthFigure is the depth drawn beside things everyone knows: a ruler, a
// scooter wheel and a standing adult with the ankle, knee, mid-thigh, waist
// and chest marked, with the water at the measured depth and its plus or
// minus as a band. It is drawn on the server as SVG.
//
// The adult is 158.8 cm, the stature prior perception measures people
// against, and the marks are the fractions of stature perception's
// person.py uses (ankle 0.039, knee 0.285, mid-thigh 0.41, waist 0.60,
// chest 0.72). The wheel is 43.4 cm across, the two-wheeler wheel of
// perception's synthetic scenes (synth.py).
type DepthFigure struct {
	W, H      int
	Ground    string // y of 0 cm
	RulerX    string
	Ticks     []FigTick
	Wheel     FigWheel
	Person    string // SVG transform for the adult, drawn in centimetres
	Marks     []FigMark
	WaterY    string
	WaterH    string
	BandY     string
	BandH     string
	LineY     string
	LabelY    string
	Estimate  bool
	Depth     string
	Sigma     string
	Max       int
	Deeper    bool // the water is above the drawn adult's chest
	Aria      string
	PersonTop string // y of the top of the head, for the height label
}

// FigTick is a ruler tick.
type FigTick struct {
	Y     string
	Label string
	Major bool
}

// FigMark is a body landmark.
type FigMark struct {
	Y, Label string
	Cm       int
	Under    bool // the water reaches it
}

// FigWheel is the wheel's circle.
type FigWheel struct {
	CX, CY, R, Tyre, Rim, Hub string
}

const (
	adultCm = 158.8
	wheelCm = 43.4
)

var landmarks = []struct {
	key  string
	frac float64
}{{"ankle", 0.039}, {"knee", 0.285}, {"thigh", 0.41}, {"waist", 0.60}, {"chest", 0.72}}

func f1(x float64) string { return fmt.Sprintf("%.1f", x) }

// NewDepthFigure draws a depth and its sigma. estimate marks the scene
// model's visual estimate, drawn as a hatched band.
func NewDepthFigure(lang string, depth, sigma float64, estimate bool) DepthFigure {
	const w, h, ground, top = 300, 300, 276.0, 18.0
	maxCm := 180.0
	if hi := depth + sigma; hi > 170 {
		maxCm = math.Ceil((hi+15)/50) * 50
	}
	k := (ground - top) / maxCm
	y := func(cm float64) float64 { return ground - math.Max(0, math.Min(maxCm, cm))*k }
	f := DepthFigure{
		W: w, H: h, Ground: f1(ground), RulerX: "40", Max: int(maxCm), Estimate: estimate,
		Depth: fmt.Sprintf("%.0f", depth), Sigma: fmt.Sprintf("%.0f", sigma),
		Deeper: depth > adultCm*0.72,
	}
	step := 10.0
	if maxCm > 200 {
		step = 25
	}
	for cm := 0.0; cm <= maxCm+0.1; cm += step {
		t := FigTick{Y: f1(y(cm))}
		if math.Mod(cm, 50) == 0 {
			t.Major, t.Label = true, fmt.Sprintf("%.0f", cm)
		}
		f.Ticks = append(f.Ticks, t)
	}
	r := wheelCm / 2 * k
	f.Wheel = FigWheel{CX: "92", CY: f1(ground - r), R: f1(r - r*0.09), Tyre: f1(r * 0.18), Rim: f1(r * 0.55), Hub: f1(math.Max(2, r*0.14))}
	f.Person = fmt.Sprintf("translate(184 %s) scale(%.4f)", f1(ground), k)
	f.PersonTop = f1(y(adultCm))
	for _, m := range landmarks {
		cm := m.frac * adultCm
		f.Marks = append(f.Marks, FigMark{Y: f1(y(cm)), Label: i18n.T(lang, "fig."+m.key), Cm: int(math.Round(cm)), Under: depth >= cm})
	}
	f.WaterY = f1(y(depth))
	f.WaterH = f1(ground - y(depth))
	f.LineY = f.WaterY
	lo, hi := y(depth+sigma), y(depth-sigma)
	f.BandY, f.BandH = f1(lo), f1(hi-lo)
	f.LabelY = f1(math.Max(top+10, math.Min(ground-6, y(depth))))
	key := "fig.aria"
	if estimate {
		key = "fig.aria_estimate"
	}
	f.Aria = i18n.T(lang, key, f.Depth, f.Sigma)
	return f
}
