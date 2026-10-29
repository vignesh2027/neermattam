package web

import (
	"encoding/json"
	"html/template"

	"neermattam/gateway/internal/config"
)

// CityView is the city a page shows.
type CityView struct {
	ID        string
	Name      string // English; templates print {{t (printf "city.%s" .ID)}}
	BBox      config.BBox
	CentreLat float64
	CentreLon float64
	Cells     int
	Routing   string
	Simulated bool        // holds the simulated scenario
	JSON      template.JS // {"id","bbox","centre"} for the map glue
}

// NewCityView builds the view and its JSON for the glue.
func NewCityView(id, name string, b config.BBox, lat, lon float64, cells int, routing string, simulated bool) CityView {
	raw, _ := json.Marshal(map[string]any{
		"id":        id,
		"bbox":      [4]float64{b.MinLon, b.MinLat, b.MaxLon, b.MaxLat},
		"centre":    [2]float64{lon, lat},
		"simulated": simulated,
	})
	return CityView{ID: id, Name: name, BBox: b, CentreLat: lat, CentreLon: lon, Cells: cells, Routing: routing, Simulated: simulated, JSON: template.JS(raw)}
}

// CityLink is one entry of the city switcher.
type CityLink struct {
	ID        string
	Href      string
	Current   bool
	Simulated bool
}
