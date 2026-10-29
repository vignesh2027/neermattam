package web

import (
	"fmt"
	"math"
	"strconv"
	"strings"

	"neermattam/gateway/internal/facts"
	"neermattam/gateway/internal/places"
	"neermattam/gateway/internal/upstream"
)

// HomeView is the landing page's data.
type HomeView struct {
	Preview PreviewView
	Trace   *HomeTrace
	Facts   *facts.Facts
}

// HomeTrace is the recorded agent run shown as cards.
type HomeTrace struct {
	Steps               []TraceStepView
	Total               int
	Photo, Credit, Date string
}

// PreviewView is a small schematic map of the live cells, drawn as SVG on
// the server: no tiles, no script needed to see it, nothing to shift.
type PreviewView struct {
	W, H   int
	Bounds string // minLon,maxLat,kx,ky for the inline glue
	Grid   []string
	Bands  []PreviewBand
	Places []PreviewPlace
	Count  int
}

// PreviewBand is every cell of one colour, drawn as a single path of
// relative hexagon outlines. At this scale a 66 m cell is about 3 px wide,
// so one element per cell would cost far more bytes than it shows; live
// changes are drawn on top by the inline glue.
type PreviewBand struct {
	Fill, D string
}

// PreviewPlace is a labelled locality.
type PreviewPlace struct {
	X, Y string
	Name string
}

// Frame is the part of a city the preview draws. Chennai's frames the
// south and centre, where the 2023 low points are (Velachery,
// Pallikaranai, Madipakkam, Guindy, Saidapet, T. Nagar); other cities fit
// their whole box.
type Frame struct {
	City                   string
	MinLon, MaxLon, MaxLat float64
	W, H                   int
}

const (
	pvW      = 600
	pvCosLat = 0.9744 // cos(13 degrees), for Chennai's fixed frame
)

// FrameFor returns the preview frame for a city and its box.
func FrameFor(city string, minLon, minLat, maxLon, maxLat float64) Frame {
	if city == "chennai" || city == "" {
		return Frame{City: "chennai", MinLon: 80.165, MaxLon: 80.275, MaxLat: 13.047, W: pvW, H: 640}
	}
	kx := float64(pvW) / (maxLon - minLon)
	ky := kx / math.Cos((minLat+maxLat)/2*math.Pi/180)
	h := int(math.Round((maxLat - minLat) * ky))
	return Frame{City: city, MinLon: minLon, MaxLon: maxLon, MaxLat: maxLat, W: pvW, H: min(max(h, 360), 820)}
}

// NewPreview projects cells into a frame.
func NewPreview(cells []upstream.CellFeature, f Frame) PreviewView {
	if f.W == 0 {
		f = FrameFor("chennai", 0, 0, 0, 0)
	}
	kx := float64(f.W) / (f.MaxLon - f.MinLon)
	cos := pvCosLat
	if f.City != "chennai" {
		cos = math.Cos((f.MaxLat - float64(f.H)/2/kx) * math.Pi / 180)
	}
	ky := kx / cos
	minLat := f.MaxLat - float64(f.H)/ky
	v := PreviewView{W: f.W, H: f.H, Bounds: fmt.Sprintf("%g,%g,%.3f,%.3f", f.MinLon, f.MaxLat, kx, ky), Count: len(cells)}
	for x := 40; x < f.W; x += 80 {
		v.Grid = append(v.Grid, fmt.Sprintf("M%d 0V%d", x, f.H))
	}
	for y := 40; y < f.H; y += 80 {
		v.Grid = append(v.Grid, fmt.Sprintf("M0 %dH%d", y, f.W))
	}
	proj := func(lon, lat float64) (float64, float64) {
		return (lon - f.MinLon) * kx, (f.MaxLat - lat) * ky
	}
	paths := map[string]*strings.Builder{}
	for _, c := range cells {
		if c.Lat < minLat || c.Lat > f.MaxLat || c.Lon < f.MinLon || c.Lon > f.MaxLon || len(c.RawPolygon) == 0 {
			continue
		}
		fill := DepthColor(c.Props.DepthCm)
		if c.Props.Unknown {
			fill = "url(#pv-hatch)"
		}
		b := paths[fill]
		if b == nil {
			b = &strings.Builder{}
			paths[fill] = b
		}
		ring := c.RawPolygon[0]
		if n := len(ring); n > 1 && ring[0] == ring[n-1] {
			ring = ring[:n-1]
		}
		var px, py float64
		for i, p := range ring {
			x, y := proj(p[0], p[1])
			x, y = math.Round(x*10)/10, math.Round(y*10)/10
			if i == 0 {
				b.WriteString("M" + num(x) + " " + num(y) + "l")
			} else {
				dx, dy := num(x-px), num(y-py)
				if i > 1 && dx[0] != '-' {
					b.WriteByte(' ')
				}
				b.WriteString(dx)
				if dy[0] != '-' {
					b.WriteByte(' ')
				}
				b.WriteString(dy)
			}
			px, py = x, y
		}
		b.WriteByte('z')
	}
	// Unknown first, then shallow to deep, so the deepest water is on top.
	for _, fill := range []string{"url(#pv-hatch)", "#BAE6FD", "#38BDF8", "#2563EB", "#4C1D95", "#9F1239"} {
		if b := paths[fill]; b != nil {
			v.Bands = append(v.Bands, PreviewBand{Fill: fill, D: b.String()})
		}
	}
	marks := places.Landmarks()
	if f.City != "chennai" {
		marks = places.CityPresets[f.City]
	}
	var placed [][2]float64
	for _, p := range marks {
		if p.Lat < minLat+0.004 || p.Lat > f.MaxLat-0.004 || p.Lon < f.MinLon+0.003 || p.Lon > f.MaxLon-0.02 {
			continue
		}
		x, y := proj(p.Lon, p.Lat)
		if clashes(placed, x, y) {
			continue // a label already sits there
		}
		placed = append(placed, [2]float64{x, y})
		v.Places = append(v.Places, PreviewPlace{X: fmt.Sprintf("%.1f", x), Y: fmt.Sprintf("%.1f", y), Name: p.Name})
	}
	return v
}

// clashes reports whether a label at x, y would overlap one already placed.
func clashes(placed [][2]float64, x, y float64) bool {
	for _, q := range placed {
		if math.Abs(q[1]-y) < 14 && math.Abs(q[0]-x) < 110 {
			return true
		}
	}
	return false
}

// num prints a coordinate to 0.1 with no leading zero: 1.5, -.9, 12.
func num(f float64) string {
	s := strconv.FormatFloat(math.Round(f*10)/10, 'f', -1, 64)
	if s == "-0" {
		return "0"
	}
	if strings.HasPrefix(s, "0.") {
		return s[1:]
	}
	if strings.HasPrefix(s, "-0.") {
		return "-" + s[2:]
	}
	return s
}

// NewHomeTrace renders the recorded trace in a language.
func NewHomeTrace(lang string, t facts.Trace) *HomeTrace {
	if len(t.Steps) == 0 {
		return nil
	}
	h := &HomeTrace{Total: t.Total, Photo: t.Photo, Credit: t.Credit, Date: t.Date}
	for i, s := range t.Steps {
		st := NewTraceStep(lang, i, s)
		st.DelayMs = 0
		h.Steps = append(h.Steps, st)
	}
	return h
}
