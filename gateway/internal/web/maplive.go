package web

import (
	"encoding/json"
	"fmt"
	"html/template"
	"time"

	"neermattam/gateway/internal/i18n"
)

// NoValue is shown where a number is not known, such as the deepest water
// in a city with no reports. It is never "0" and never "None".
const NoValue = "–"

// ReplayView is the switch for the simulated replay of a past flood.
type ReplayView struct {
	Available bool   // the page's city has the replay on this gateway
	On        bool   // the page shows it
	OnHref    string // the same page with the replay on
	OffHref   string // the same page on the live map
	// DemoHref opens the replay on the map from any city, when this
	// gateway has it; an empty city offers it instead of a bare map.
	DemoHref string
}

// CityStatus is one city in the map's city strip: real data only.
type CityStatus struct {
	ID      string
	Href    string
	Current bool
	// Reports is real reports fused in the last 24 hours.
	Reports int
	// Rain24h is modelled rainfall in the last 24 hours ("12.4"), and
	// RainAt when it was read; HasRain is false when it could not be read.
	HasRain bool
	Rain24h string
	Rain1h  string
	RainAt  string
	Wet     bool // some rain in the last 24 hours
	Photos  int
	// Cells is live cells with evidence in force, or -1 when unknown.
	Cells int
}

// Active says whether the city has anything real to show right now.
func (c CityStatus) Active() bool { return c.Reports > 0 || c.Cells > 0 || c.Wet }

// MapPhoto is one measured archival photo in the city's list.
type MapPhoto struct {
	ID         string // perception's measurement id
	Place      string
	Year       string
	MeasuredOn string
	Depth      string // "26", or "" when not measured
	Sigma      string
	Method     string // wheel_geometry, person_reference, model_estimate, not_measured
	Thumb      string
	Verify     string
	Credit     string
	Source     string
	Lat, Lon   float64
}

// RainFigure formats millimetres of rain.
func RainFigure(mm float64) string { return fmt.Sprintf("%.1f", mm) }

// RainTime is when a rainfall reading was taken, as a clock time in IST
// with the day when it was not today.
func RainTime(lang string, t time.Time) string {
	if t.IsZero() {
		return ""
	}
	return When(lang, t)
}

// PhotosGeoJSON is the photo layer for the map glue. Labels are rendered
// on the server in the page's language.
func PhotosGeoJSON(lang string, list []MapPhoto) template.JS {
	feats := make([]map[string]any, 0, len(list))
	for _, p := range list {
		label := i18n.T(lang, "photos.label", p.Year, p.MeasuredOn)
		depth := i18n.T(lang, "photos.not_measured")
		if p.Depth != "" {
			depth = p.Depth + " ± " + p.Sigma + " cm"
		}
		feats = append(feats, map[string]any{
			"type":     "Feature",
			"geometry": map[string]any{"type": "Point", "coordinates": []float64{p.Lon, p.Lat}},
			"properties": map[string]any{
				"id": p.ID, "place": p.Place, "label": label, "depth": depth,
				"method": i18n.T(lang, "photos.method_"+p.Method), "thumb": p.Thumb,
				"verify": p.Verify, "credit": p.Credit, "source": p.Source,
			},
		})
	}
	b, _ := json.Marshal(map[string]any{"type": "FeatureCollection", "features": feats})
	return template.JS(b)
}

// Pair passes a page and a flag to a template, such as the map's city strip
// rendered once for the wide side panel and once for narrow screens.
type Pair struct {
	A any
	B bool
}
