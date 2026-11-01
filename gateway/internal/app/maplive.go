package app

import (
	"context"
	"fmt"
	"net/url"

	"neermattam/gateway/internal/web"
)

// mapExtras fills the map page's city strip (real reports, rainfall and
// measured photos per city, from memory or a cache, never waiting on the
// engine) and the page city's measured archival photos.
func (a *App) mapExtras(_ context.Context, p *web.Page) {
	doc := a.indiaCached()
	for _, c := range doc.Cities {
		q := url.Values{"city": {c.ID}}
		st := web.CityStatus{ID: c.ID, Href: "/map?" + q.Encode(), Current: c.ID == p.City.ID, Reports: c.RealReports24h, Photos: c.MeasuredPhotos, Cells: -1}
		if c.CellsWithEvidence != nil {
			st.Cells = *c.CellsWithEvidence
		}
		if c.RainMM24h != nil && c.RainMM1h != nil && c.RainAt != nil {
			st.HasRain = true
			st.Rain24h, st.Rain1h = web.RainFigure(*c.RainMM24h), web.RainFigure(*c.RainMM1h)
			st.RainAt = web.RainTime(p.Lang, *c.RainAt)
			st.Wet = *c.RainMM24h >= 0.1
		}
		p.Overview = append(p.Overview, st)
	}
	for _, ph := range a.Photos.InCity(p.City.ID) {
		mp := web.MapPhoto{
			ID: ph.MeasurementID(), Place: ph.Place, Year: ph.Year(), MeasuredOn: web.Day(p.Lang, ph.MeasuredAt()),
			Method: ph.Method(), Thumb: a.Renderer.Assets().URL("photos/" + ph.ID + ".jpg"),
			Verify: "/verify/" + ph.MeasurementID(), Credit: ph.Credit(), Source: ph.SourceURL, Lat: ph.Lat, Lon: ph.Lon,
		}
		if mp.Method == "" {
			mp.Method = "not_measured"
		} else if m := ph.Measurement; m.DepthCm != nil && m.DepthSigmaCm != nil {
			mp.Depth, mp.Sigma = fmt.Sprintf("%.0f", *m.DepthCm), fmt.Sprintf("%.0f", *m.DepthSigmaCm)
		}
		p.Photos = append(p.Photos, mp)
	}
	p.PhotosJSON = web.PhotosGeoJSON(p.Lang, p.Photos)
}
