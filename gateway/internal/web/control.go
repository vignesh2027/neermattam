package web

import (
	"fmt"
	"html"
	"html/template"
	"math"
	"net/url"
	"strings"
	"time"

	"neermattam/gateway/internal/i18n"
	"neermattam/gateway/internal/upstream"
)

// ControlView is the control room for one city.
type ControlView struct {
	Demo      bool   // the public read-only demo over simulated Chennai
	Base      string // "/control" or "/control/demo"
	City      string
	Generated string
	Error     string // the engine could not be read

	Tiles    []Tile
	Blocked  []BlockedCount
	Hotspots []HotspotView
	Fresh    []FreshView
	FreshAll int
	Routing  []RoutingView
	Cell     *CellDetail
	Score    string // the engine's score formula, shown under the list

	ExportCSV, ExportGeoJSON string
	PanelURL                 string
	StreamURL                string
}

// Tile is one summary number.
type Tile struct {
	Key   string // i18n key of the label
	Value string
	Sub   string
	Tone  string // danger, violet, sky, grey or ""
	Words bool   // the value is words, not a number
}

// BlockedCount is the number of cells closed to one vehicle.
type BlockedCount struct {
	Vehicle string
	N       int
}

// HotspotPart is one part of a score.
type HotspotPart struct {
	Key   string
	Value float64
	Max   float64
	Pct   string // share of the 100-point bar
}

// HotspotView is one ranked cell.
type HotspotView struct {
	Rank       int
	Cell       string
	Score      string
	Parts      []HotspotPart
	DepthBlock string
	Depth      string
	Sigma      string
	Class      string
	Age        string
	Reports    int
	Unknown    bool
	Blocks     []string
	Reasons    []string
	MapHref    string
	DetailHref string
	Selected   bool
}

// FreshView is a cell that needs a fresh report.
type FreshView struct {
	Cell        string
	LastDepthCm float64
	LastDepth   string
	Age         string
	Expired     bool
	MapHref     string
	ReportHref  string
	DetailHref  string
}

// RoutingView is one city's road graph state.
type RoutingView struct {
	City    string
	Status  string
	Current bool
}

// CellDetail is the drill-down for one cell.
type CellDetail struct {
	Cell      string
	Found     bool
	Spark     template.HTML
	Points    int
	Trend     string // falling, rising, steady or ""
	Rate      string
	To15      string
	Note      string
	Depth     string
	Class     string
	Latest    string
	MapHref   string
	CloseHref string
	Conflicts int
}

func f0(v float64) string { return fmt.Sprintf("%.0f", v) }

// NewControlView builds the view from the engine's answers. Reasons are
// the engine's own sentences in English; other languages get sentences
// built from the same parts.
func NewControlView(lang, city, base string, demo bool, sum *upstream.Summary, hs *upstream.Hotspots, cell string) *ControlView {
	v := &ControlView{Demo: demo, Base: base, City: city}
	cityQ := ""
	if !demo {
		cityQ = "&city=" + url.QueryEscape(city)
	}
	v.ExportCSV = base + "/export?format=csv" + cityQ
	v.ExportGeoJSON = base + "/export?format=geojson" + cityQ
	v.PanelURL = base + "/panel?lang=" + url.QueryEscape(lang) + cityQ
	if cell != "" {
		v.PanelURL += "&cell=" + url.QueryEscape(cell)
	}
	v.StreamURL = "/api/stream?city=" + url.QueryEscape(city)
	q := func(extra url.Values) string {
		if !demo {
			extra.Set("city", city)
		}
		return base + "?" + extra.Encode()
	}
	if sum != nil {
		v.Generated = When(lang, sum.GeneratedAt)
		bc := sum.ByClass
		v.Tiles = []Tile{
			{Key: "hazard.danger_all", Value: fmt.Sprint(bc["danger_all"]), Tone: "danger"},
			{Key: "hazard.danger_most", Value: fmt.Sprint(bc["danger_most"]), Tone: "violet"},
			{Key: "hazard.danger_some", Value: fmt.Sprint(bc["danger_some"]), Tone: "sky"},
			{Key: "hazard.low", Value: fmt.Sprint(bc["low"])},
			{Key: "ctl.tile_unknown", Value: fmt.Sprint(sum.Unknown), Tone: "grey"},
			{Key: "ctl.tile_stale", Value: fmt.Sprint(sum.Stale), Sub: i18n.T(lang, "ctl.tile_expired", sum.Expired), Tone: "grey"},
			{Key: "ctl.tile_conflicts", Value: fmt.Sprint(sum.CellsInConflict), Sub: i18n.T(lang, "ctl.tile_conflicting", sum.ConflictingReadings)},
			{Key: "ctl.tile_fresh", Value: fmt.Sprint(sum.NeedsFreshReport)},
		}
		newest := Tile{Key: "ctl.tile_newest", Value: i18n.T(lang, "js.none"), Sub: i18n.T(lang, "ctl.tile_readings", sum.Readings), Words: true}
		if sum.NewestReadingAt != nil {
			newest.Value = Ago(lang, *sum.NewestReadingAt)
		}
		newest.Words = true
		v.Tiles = append(v.Tiles, newest)
		for _, veh := range []string{"pedestrian", "two_wheeler", "auto", "car", "bus"} {
			v.Blocked = append(v.Blocked, BlockedCount{veh, sum.Blocked[veh]})
		}
		v.FreshAll = sum.NeedsFreshReport
		for _, c := range sum.NeedsFreshReportCells {
			at := fmt.Sprintf("%.5f,%.5f", c.Lat, c.Lon)
			v.Fresh = append(v.Fresh, FreshView{
				Cell: c.Cell, LastDepthCm: c.LastDepthCm, LastDepth: f0(c.LastDepthCm), Age: hours(lang, c.LastReadingAgeH), Expired: c.Expired,
				MapHref:    "/map?" + url.Values{"city": {city}, "at": {at}}.Encode(),
				ReportHref: "/report?" + url.Values{"city": {city}, "at": {at}}.Encode(),
				DetailHref: q(url.Values{"cell": {c.Cell}}),
			})
		}
	}
	if hs != nil {
		v.Score = hs.Score
		for i, h := range hs.Hotspots {
			hv := HotspotView{
				Rank: i + 1, Cell: h.Cell, Score: fmt.Sprintf("%.1f", h.Score),
				DepthBlock: f0(h.DepthBlockCm), Depth: f0(h.DepthCm), Sigma: f0(h.DepthSigmaCm),
				Class: orUnknown(h.HazardClassUpper), Age: hours(lang, h.AgeH), Reports: h.IndependentReports,
				Unknown: h.Unknown, Blocks: h.Blocks,
				MapHref:    "/map?" + url.Values{"city": {city}, "at": {fmt.Sprintf("%.5f,%.5f", h.Lat, h.Lon)}}.Encode(),
				DetailHref: q(url.Values{"cell": {h.Cell}}),
				Selected:   h.Cell == cell,
			}
			for _, p := range []struct {
				key      string
				val, max float64
			}{{"depth", h.Parts.Depth, 40}, {"hazard", h.Parts.Hazard, 30}, {"freshness", h.Parts.Freshness, 20}, {"reports", h.Parts.Reports, 10}} {
				hv.Parts = append(hv.Parts, HotspotPart{Key: p.key, Value: math.Round(p.val*10) / 10, Max: p.max, Pct: fmt.Sprintf("%.1f%%", math.Max(0, p.val))})
			}
			if lang == i18n.Default {
				hv.Reasons = h.Reasons
			} else {
				hv.Reasons = []string{
					i18n.T(lang, "ctl.reason_depth", f0(h.DepthBlockCm), f0(h.DepthCm), fmt.Sprintf("%.1f", h.Parts.Depth)),
					i18n.T(lang, "ctl.reason_hazard", i18n.T(lang, "hazard."+orUnknown(h.HazardClassUpper)), fmt.Sprintf("%.0f", h.Parts.Hazard)),
					i18n.T(lang, "ctl.reason_fresh", fmt.Sprintf("%.1f", h.AgeH), fmt.Sprintf("%.1f", h.Parts.Freshness)),
					i18n.T(lang, "ctl.reason_reports", h.IndependentReports, fmt.Sprintf("%.0f", h.Parts.Reports)),
				}
			}
			v.Hotspots = append(v.Hotspots, hv)
		}
	}
	return v
}

func orUnknown(c string) string {
	if c == "" {
		return "unknown"
	}
	return c
}

func hours(lang string, h float64) string {
	switch {
	case h < 1.0/60:
		return i18n.T(lang, "js.just_now")
	case h < 1:
		return i18n.T(lang, "js.min_ago", int(math.Round(h*60)))
	default:
		return i18n.T(lang, "js.h_ago", fmt.Sprintf("%.1f", h))
	}
}

// NewCellDetail builds the drill-down from a recession answer (nil when no
// report has touched the cell).
func NewCellDetail(lang, city, cell, closeHref string, r *upstream.Recession) *CellDetail {
	d := &CellDetail{Cell: cell, CloseHref: closeHref}
	if r == nil {
		return d
	}
	d.Found = true
	d.Points = len(r.History)
	d.Spark = Sparkline(lang, r.History)
	d.Note = r.DrainNote
	if lang != i18n.Default {
		d.Note = i18n.T(lang, "ctl.drain_note")
		if r.Drain == nil {
			d.Note = i18n.T(lang, "ctl.drain_none")
		}
	}
	if r.Drain != nil {
		d.Trend = r.Drain.Trend
		d.Rate = fmt.Sprintf("%.1f ± %.1f", math.Abs(r.Drain.RateCmPerH), r.Drain.RateSigmaCmPerH)
		if r.Drain.HoursTo15Cm != nil {
			d.To15 = fmt.Sprintf("%.1f", *r.Drain.HoursTo15Cm)
		}
	}
	if p := r.Current; p != nil {
		d.Depth = fmt.Sprintf("%.0f ± %.0f", p.DepthCm, p.DepthSigmaCm)
		d.Class = orUnknown(p.HazardClass)
		if p.Unknown {
			d.Class = "unknown"
		}
		if !p.UpdatedAt.IsZero() {
			d.Latest = Ago(lang, p.UpdatedAt)
		}
		d.Conflicts = p.Conflicts
	}
	if r.Lat != 0 || r.Lon != 0 {
		d.MapHref = "/map?" + url.Values{"city": {city}, "at": {fmt.Sprintf("%.5f,%.5f", r.Lat, r.Lon)}}.Encode()
	}
	return d
}

// Sparkline draws a cell's depth history as a small SVG: each direct
// reading with its plus or minus, the fused estimate as a line with its
// band, and the 15 cm and 30 cm vehicle limits. It is plain markup, drawn
// on the server.
func Sparkline(lang string, hist []upstream.HistoryPoint) template.HTML {
	const w, h, padL, padR, padT, padB = 560.0, 170.0, 34.0, 10.0, 12.0, 26.0
	if len(hist) == 0 {
		return ""
	}
	t0, t1 := hist[0].At, hist[len(hist)-1].At
	if !t1.After(t0) {
		t0 = t0.Add(-30 * time.Minute)
		t1 = t1.Add(30 * time.Minute)
	}
	top := 40.0
	for _, p := range hist {
		top = math.Max(top, math.Max(p.ReadingCm+p.ReadingSigmaCm, p.FusedCm+p.FusedSigmaCm))
	}
	top = math.Ceil(top/10) * 10
	x := func(t time.Time) float64 {
		return padL + (w-padL-padR)*t.Sub(t0).Seconds()/t1.Sub(t0).Seconds()
	}
	y := func(cm float64) float64 { return padT + (h-padT-padB)*(1-math.Max(0, cm)/top) }
	var b strings.Builder
	fmt.Fprintf(&b, `<svg class="spark" viewBox="0 0 %.0f %.0f" width="%.0f" height="%.0f" role="img" aria-label="%s">`, w, h, w, h, html.EscapeString(i18n.T(lang, "ctl.spark_aria", len(hist))))
	// Axes and limits.
	for _, lim := range []float64{15, 30} {
		if lim < top {
			fmt.Fprintf(&b, `<line class="lim" x1="%.1f" x2="%.1f" y1="%.1f" y2="%.1f"/><text class="lim-t" x="%.1f" y="%.1f">%.0f cm</text>`, padL, w-padR, y(lim), y(lim), w-padR, y(lim)-3, lim)
		}
	}
	fmt.Fprintf(&b, `<line class="axis" x1="%.1f" x2="%.1f" y1="%.1f" y2="%.1f"/>`, padL, w-padR, y(0), y(0))
	fmt.Fprintf(&b, `<text class="ax-t" x="%.1f" y="%.1f" text-anchor="end">%.0f</text><text class="ax-t" x="%.1f" y="%.1f" text-anchor="end">0</text>`, padL-5, y(top)+4, top, padL-5, y(0)+4)
	fmt.Fprintf(&b, `<text class="ax-t" x="%.1f" y="%.1f">%s</text><text class="ax-t" x="%.1f" y="%.1f" text-anchor="end">%s</text>`,
		padL, h-6, html.EscapeString(When(lang, t0)), w-padR, h-6, html.EscapeString(When(lang, t1)))
	// Fused band and line.
	var up, down, line []string
	for _, p := range hist {
		up = append(up, fmt.Sprintf("%.1f,%.1f", x(p.At), y(p.FusedCm+p.FusedSigmaCm)))
		down = append([]string{fmt.Sprintf("%.1f,%.1f", x(p.At), y(p.FusedCm-p.FusedSigmaCm))}, down...)
		line = append(line, fmt.Sprintf("%.1f,%.1f", x(p.At), y(p.FusedCm)))
	}
	if len(hist) > 1 {
		fmt.Fprintf(&b, `<polygon class="band" points="%s"/>`, strings.Join(append(up, down...), " "))
		fmt.Fprintf(&b, `<polyline class="fused" points="%s"/>`, strings.Join(line, " "))
	}
	// Readings with their plus or minus.
	for _, p := range hist {
		cls := "pt"
		if p.Migrated {
			cls = "pt pt-mig"
		}
		fmt.Fprintf(&b, `<line class="err" x1="%.1f" x2="%.1f" y1="%.1f" y2="%.1f"/><circle class="%s" cx="%.1f" cy="%.1f" r="3.6"><title>%.0f ± %.0f cm, %s</title></circle>`,
			x(p.At), x(p.At), y(p.ReadingCm+p.ReadingSigmaCm), y(p.ReadingCm-p.ReadingSigmaCm), cls, x(p.At), y(p.ReadingCm), p.ReadingCm, p.ReadingSigmaCm, html.EscapeString(When(lang, p.At)))
	}
	b.WriteString(`</svg>`)
	return template.HTML(b.String())
}
