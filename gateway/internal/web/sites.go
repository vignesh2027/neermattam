package web

import (
	"encoding/json"
	"fmt"
	"html/template"
	"math"
	"net/url"
	"strings"

	"neermattam/gateway/internal/i18n"
	"neermattam/gateway/internal/upstream"
)

// The "Critical sites" page: OpenStreetMap data centres (and other site
// kinds) with their watch-zone status, a selected site's access routes and
// its area's flood record, and the city's record for siting.

func init() {
	// The page template lives in templates/sites.html.
	pageNames = append(pageNames, "sites")
}

// SiteKinds are the kinds the page offers, in tab order.
var SiteKinds = []string{"data_centre", "hospital", "telephone_exchange", "cable_landing_station"}

// SitesView is the whole page.
type SitesView struct {
	City       string
	Kind       string
	Tabs       []SiteTab
	Cards      []SiteCard
	Total      int
	Shown      int
	OSMCount   int
	DataUntil  string
	Selected   *SiteDetailView
	Siting     *SitingView
	SimSiting  *SitingView // the simulated scenario's record, labelled as such
	Error      string      // i18n key
	Push       bool
	Threshold  float64
	MapJSON    template.JS // sites, rings and routes for the glue
	ConfigJSON template.JS // {"key","city","lang","site"} for the push glue
}

// SiteTab is one kind tab.
type SiteTab struct {
	Kind    string
	N       int
	Href    string
	Current bool
}

// SiteCard is one site in the list.
type SiteCard struct {
	ID       string
	Name     string
	Operator string
	Kind     string
	Status   string // over, nearby, under, unknown
	Chip     string // CSS class of the status chip
	Label    string
	Zones    []ZoneLine
	Href     string
	OSMHref  string
	Selected bool
}

// ZoneLine is one ring, in words.
type ZoneLine struct {
	Radius   string
	Status   string
	Chip     string
	Label    string
	Depth    string // "up to 34 cm (mean 30 ± 3 cm)" or ""
	Coverage string // "12 of 21 cells reported"
	Fresh    string // "newest report 5 min ago" or ""
	Trend    string // "rising", "falling", "steady" in words, or ""
	OldDeep  string // a warning about expired deep reports, or ""
}

// SiteDetailView is the selected site.
type SiteDetailView struct {
	Card       SiteCard
	Coords     string
	Routes     []RouteRow
	RoutesNote string
	Risk       []AreaRow
	RiskNote   string
	RiskSince  string
}

// RouteRow is one access target with a cell per vehicle.
type RouteRow struct {
	Target string // main_road or fuel
	Label  string
	Dest   string
	Cells  []RouteCell
}

// RouteCell is one vehicle's route to one target.
type RouteCell struct {
	Vehicle string
	Status  string // open, no_safe_route, unavailable
	Chip    string
	Label   string
	Detail  string
	Explain string
}

// AreaRow is one area of the flood record.
type AreaRow struct {
	Area       string
	Days       int
	Peak       string
	Recession  string
	Open       int
	Unresolved int
	Readings   int
	Wet        int
	Bar        string // width of the days bar, for a quick visual ranking
	MapHref    string
}

// SitingView is the city's flood record for siting.
type SitingView struct {
	Simulated bool
	Since     string
	Days      string
	Total     int
	Areas     []AreaRow
	Note      string
}

// SitesOptions are what the handler knows beyond the engine's answers.
type SitesOptions struct {
	Lang, City, Kind, Site string
	Push                   bool
	VAPIDKey               string
	Error                  string
}

func statusChip(s string) string {
	switch s {
	case "over":
		return "chip-danger"
	case "nearby":
		return "chip-violet"
	case "under":
		return ""
	}
	return "chip-grey"
}

func radiusText(m float64) string {
	if m >= 1000 {
		return strings.TrimSuffix(fmt.Sprintf("%.1f", m/1000), ".0") + " km"
	}
	return fmt.Sprintf("%.0f m", m)
}

func siteName(lang string, s upstream.Site) string {
	if s.Name != "" {
		return s.Name
	}
	return i18n.T(lang, "sites.unnamed_"+s.Kind)
}

func siteHref(city, kind, id string) string {
	return "/sites?" + url.Values{"city": {city}, "kind": {kind}, "site": {id}}.Encode() + "#site"
}

// zoneLines describes a site's rings.
func zoneLines(lang string, s upstream.Site, threshold float64) []ZoneLine {
	var out []ZoneLine
	lim := f0(threshold)
	for _, z := range s.Zones {
		r := radiusText(z.RadiusM)
		zl := ZoneLine{Radius: r, Status: z.Status, Chip: statusChip(z.Status)}
		switch z.Status {
		case "over":
			zl.Label = i18n.T(lang, "sites.zone_over", lim)
		case "under":
			zl.Label = i18n.T(lang, "sites.zone_under", lim)
		default:
			zl.Label = i18n.T(lang, "sites.zone_unknown")
		}
		if d := z.Deepest; d != nil {
			zl.Depth = i18n.T(lang, "sites.depth_upto", f0(d.DepthBlockCm), f0(d.DepthCm), f0(d.DepthSigmaCm))
		}
		zl.Coverage = i18n.T(lang, "sites.coverage", z.CellsKnown, z.Cells)
		if z.NewestEvidenceAgeH != nil {
			zl.Fresh = i18n.T(lang, "sites.newest", hours(lang, *z.NewestEvidenceAgeH))
		}
		if z.Trend != nil {
			zl.Trend = i18n.T(lang, "sites.trend_"+*z.Trend)
		}
		if z.OldDeepCells > 0 && z.OldDeepMaxCm != nil && z.OldDeepAgeH != nil {
			zl.OldDeep = i18n.T(lang, "sites.old_deep", z.OldDeepCells, f0(*z.OldDeepMaxCm), f0(*z.OldDeepAgeH))
		}
		out = append(out, zl)
	}
	return out
}

func siteCard(lang, city, kind string, s upstream.Site, threshold float64, selected string) SiteCard {
	c := SiteCard{
		ID: s.ID, Name: siteName(lang, s), Operator: s.Operator, Kind: s.Kind,
		Status: s.Status, Chip: statusChip(s.Status), Href: siteHref(city, kind, s.ID),
		Zones: zoneLines(lang, s, threshold), Selected: s.ID == selected,
	}
	if s.OSM != "" {
		c.OSMHref = "https://www.openstreetmap.org/" + s.OSM
	}
	outer := "1 km"
	if n := len(s.Zones); n > 0 {
		outer = radiusText(s.Zones[n-1].RadiusM)
	}
	inner := "300 m"
	if len(s.Zones) > 0 {
		inner = radiusText(s.Zones[0].RadiusM)
	}
	switch s.Status {
	case "over":
		c.Label = i18n.T(lang, "sites.status_over", f0(threshold), inner)
	case "nearby":
		c.Label = i18n.T(lang, "sites.status_nearby", f0(threshold), outer)
	case "under":
		c.Label = i18n.T(lang, "sites.status_under", f0(threshold))
	default:
		c.Label = i18n.T(lang, "sites.status_unknown")
	}
	return c
}

// routeRows lays the six access routes out as two rows of three.
func routeRows(lang string, routes []upstream.SiteRoute) []RouteRow {
	var out []RouteRow
	for _, target := range []string{"main_road", "fuel"} {
		row := RouteRow{Target: target, Label: i18n.T(lang, "sites.target_title."+target)}
		for _, veh := range []string{"truck", "two_wheeler", "car"} {
			cell := RouteCell{Vehicle: veh, Status: "unavailable", Chip: "chip-grey", Label: i18n.T(lang, "sites.route_unavailable")}
			for _, r := range routes {
				if r.Target != target || r.Vehicle != veh {
					continue
				}
				cell.Status = r.Status
				if r.Destination != nil && row.Dest == "" && r.Destination.Name != nil && *r.Destination.Name != "" {
					row.Dest = *r.Destination.Name
				}
				switch r.Status {
				case "open":
					cell.Chip = "chip-ok"
					if r.DetourM != nil && *r.DetourM >= 50 {
						cell.Label = i18n.T(lang, "sites.route_open_detour", km(*r.DetourM))
					} else {
						cell.Label = i18n.T(lang, "sites.route_open")
					}
				case "no_safe_route":
					cell.Chip = "chip-danger"
					cell.Label = i18n.T(lang, "sites.route_closed")
				default:
					reason := "no_path"
					if r.Reason != nil {
						reason = *r.Reason
					}
					cell.Detail = i18n.T(lang, "sites.reason."+reason)
				}
				if r.DistanceM != nil && *r.DistanceM < 30 {
					// The site and the destination snap to the same stretch of road.
					cell.Detail = i18n.T(lang, "sites.route_at_site."+target)
				} else if r.DistanceM != nil && r.EtaMin != nil {
					cell.Detail = i18n.T(lang, "sites.route_len", km(*r.DistanceM), f0(math.Max(1, *r.EtaMin)))
					if r.UnknownCellsCrossed != nil && r.CellsOnRoute != nil && *r.CellsOnRoute > 0 {
						cell.Detail += " " + i18n.T(lang, "sites.route_unknown", *r.UnknownCellsCrossed, *r.CellsOnRoute)
					}
				}
				if lang == i18n.Default && r.Explanation != nil {
					cell.Explain = *r.Explanation
				}
			}
			row.Cells = append(row.Cells, cell)
		}
		out = append(out, row)
	}
	return out
}

func areaRows(lang, city string, areas []upstream.SiteArea) []AreaRow {
	most := 1
	for _, a := range areas {
		most = max(most, a.DaysWithWater)
	}
	var out []AreaRow
	for _, a := range areas {
		row := AreaRow{
			Area: a.Area, Days: a.DaysWithWater, Open: a.OpenEpisodes, Unresolved: a.Unresolved,
			Readings: a.Readings, Wet: a.WetReadings,
			Peak:    i18n.T(lang, "sites.peak", f0(a.PeakCm), f0(a.PeakSigmaCm)),
			Bar:     fmt.Sprintf("%.0f%%", 100*float64(a.DaysWithWater)/float64(most)),
			MapHref: "/map?" + url.Values{"city": {city}, "at": {fmt.Sprintf("%.5f,%.5f", a.Lat, a.Lon)}}.Encode(),
		}
		if a.MedianRecessionH != nil {
			row.Recession = i18n.T(lang, "sites.recession", fmt.Sprintf("%.1f", *a.MedianRecessionH), a.Episodes)
		} else {
			row.Recession = i18n.T(lang, "sites.recession_none")
		}
		out = append(out, row)
	}
	return out
}

func sitingView(lang, city string, s *upstream.Siting, simulated bool, limit int) *SitingView {
	if s == nil {
		return nil
	}
	areas := s.Areas
	if len(areas) > limit {
		areas = areas[:limit]
	}
	return &SitingView{
		Simulated: simulated,
		Since:     When(lang, s.Since),
		Days:      fmt.Sprintf("%.1f", s.HistoryDays),
		Total:     s.Total,
		Areas:     areaRows(lang, city, areas),
		Note:      i18n.T(lang, "sites.ledger_note", When(lang, s.Since)),
	}
}

// circle is a ring as a GeoJSON polygon, 64 points.
func circle(lat, lon, radiusM float64) [][2]float64 {
	const n = 64
	out := make([][2]float64, 0, n+1)
	dLat := radiusM / 111195.0
	dLon := dLat / math.Max(0.1, math.Cos(lat*math.Pi/180))
	for i := 0; i <= n; i++ {
		a := 2 * math.Pi * float64(i) / n
		out = append(out, [2]float64{lon + dLon*math.Cos(a), lat + dLat*math.Sin(a)})
	}
	return out
}

func mapJSON(list *upstream.SiteList, detail *upstream.SiteDetail, names map[string]string) template.JS {
	feat := func(geom any, props map[string]any) map[string]any {
		return map[string]any{"type": "Feature", "geometry": geom, "properties": props}
	}
	var sites, rings, routes []map[string]any
	if list != nil {
		for _, s := range list.Sites {
			sites = append(sites, feat(map[string]any{"type": "Point", "coordinates": [2]float64{s.Lon, s.Lat}},
				map[string]any{"id": s.ID, "name": names[s.ID], "status": s.Status}))
		}
	}
	var focus any
	if detail != nil {
		focus = [2]float64{detail.Lon, detail.Lat}
		for _, z := range detail.Zones {
			rings = append(rings, feat(map[string]any{"type": "Polygon", "coordinates": [][][2]float64{circle(detail.Lat, detail.Lon, z.RadiusM)}},
				map[string]any{"status": z.Status, "radius_m": z.RadiusM}))
		}
		if detail.Access != nil {
			for _, r := range detail.Access.Routes {
				if len(r.Coordinates) < 2 || r.Vehicle != "truck" {
					continue // one line per target keeps the map readable
				}
				routes = append(routes, feat(map[string]any{"type": "LineString", "coordinates": r.Coordinates},
					map[string]any{"status": r.Status, "target": r.Target}))
			}
		}
	}
	fc := func(f []map[string]any) map[string]any {
		if f == nil {
			f = []map[string]any{}
		}
		return map[string]any{"type": "FeatureCollection", "features": f}
	}
	b, _ := json.Marshal(map[string]any{"sites": fc(sites), "rings": fc(rings), "routes": fc(routes), "focus": focus})
	return template.JS(b) //nolint:gosec // json.Marshal escapes <, > and &
}

// NewSitesView builds the page from the engine's answers. Any of them may be
// nil when the engine could not be read.
func NewSitesView(o SitesOptions, list *upstream.SiteList, detail *upstream.SiteDetail, siting, simSiting *upstream.Siting) *SitesView {
	lang := o.Lang
	v := &SitesView{City: o.City, Kind: o.Kind, Error: o.Error, Push: o.Push, Threshold: 15}
	for _, k := range SiteKinds {
		t := SiteTab{Kind: k, Href: "/sites?" + url.Values{"city": {o.City}, "kind": {k}}.Encode(), Current: k == o.Kind}
		if list != nil {
			t.N = list.Counts[k].OSM
		}
		v.Tabs = append(v.Tabs, t)
	}
	names := map[string]string{}
	if list != nil {
		v.Total, v.Shown = list.Total, len(list.Sites)
		v.Threshold = list.ThresholdCm
		v.OSMCount = list.Counts[o.Kind].OSM
		if list.OSM.DataUntil != nil && len(*list.OSM.DataUntil) >= 10 {
			v.DataUntil = (*list.OSM.DataUntil)[:10]
		}
		for _, s := range list.Sites {
			v.Cards = append(v.Cards, siteCard(lang, o.City, o.Kind, s, list.ThresholdCm, o.Site))
			names[s.ID] = siteName(lang, s)
		}
	}
	if detail != nil {
		d := &SiteDetailView{
			Card:   siteCard(lang, o.City, o.Kind, detail.Site, detail.ThresholdCm, detail.ID),
			Coords: fmt.Sprintf("%.5f, %.5f", detail.Lat, detail.Lon),
		}
		if detail.Access != nil {
			d.Routes = routeRows(lang, detail.Access.Routes)
			d.RoutesNote = i18n.T(lang, "sites.routes_note", Ago(lang, detail.Access.ComputedAt))
		}
		if detail.Risk != nil {
			d.Risk = areaRows(lang, o.City, detail.Risk.Areas)
			d.RiskSince = When(lang, detail.Risk.Since)
			d.RiskNote = i18n.T(lang, "sites.ledger_note", d.RiskSince)
		}
		v.Selected = d
	}
	v.Siting = sitingView(lang, o.City, siting, false, 8)
	if simSiting != nil && len(simSiting.Areas) > 0 {
		v.SimSiting = sitingView(lang, o.City, simSiting, true, 8)
	}
	v.MapJSON = mapJSON(list, detail, names)
	cfg, _ := json.Marshal(map[string]any{"key": o.VAPIDKey, "city": o.City, "lang": lang, "site": o.Site})
	v.ConfigJSON = template.JS(cfg) //nolint:gosec // json.Marshal escapes <, > and &
	return v
}
