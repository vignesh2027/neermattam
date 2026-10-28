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

// The preview frames south and central Chennai, where the 2023 low points
// are: Velachery, Pallikaranai, Madipakkam, Guindy, Saidapet, T. Nagar.
const (
	pvW, pvH = 600, 640
	pvMinLon = 80.165
	pvMaxLon = 80.275
	pvMaxLat = 13.047
	pvCosLat = 0.9744 // cos(13 degrees): metres per degree of longitude over latitude
)

// NewPreview projects cells into the preview.
func NewPreview(cells []upstream.CellFeature) PreviewView {
	kx := float64(pvW) / (pvMaxLon - pvMinLon)
	ky := kx / pvCosLat
	minLat := pvMaxLat - float64(pvH)/ky
	v := PreviewView{W: pvW, H: pvH, Bounds: fmt.Sprintf("%g,%g,%.3f,%.3f", pvMinLon, pvMaxLat, kx, ky), Count: len(cells)}
	for x := 40; x < pvW; x += 80 {
		v.Grid = append(v.Grid, fmt.Sprintf("M%d 0V%d", x, pvH))
	}
	for y := 40; y < pvH; y += 80 {
		v.Grid = append(v.Grid, fmt.Sprintf("M0 %dH%d", y, pvW))
	}
	proj := func(lon, lat float64) (float64, float64) {
		return (lon - pvMinLon) * kx, (pvMaxLat - lat) * ky
	}
	paths := map[string]*strings.Builder{}
	for _, c := range cells {
		if c.Lat < minLat || c.Lat > pvMaxLat || c.Lon < pvMinLon || c.Lon > pvMaxLon || len(c.RawPolygon) == 0 {
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
	for _, p := range places.Landmarks() {
		if p.Lat < minLat+0.004 || p.Lat > pvMaxLat-0.004 || p.Lon < pvMinLon+0.003 || p.Lon > pvMaxLon-0.02 {
			continue
		}
		x, y := proj(p.Lon, p.Lat)
		v.Places = append(v.Places, PreviewPlace{X: fmt.Sprintf("%.1f", x), Y: fmt.Sprintf("%.1f", y), Name: p.Name})
	}
	return v
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
