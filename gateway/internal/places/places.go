// Package places is a tiny gazetteer of localities, used to say "near
// Velachery" instead of raw coordinates, and the route presets for each
// city. The coordinates are approximate centres taken from OpenStreetMap;
// they label places and seed route forms, they are never used for
// measurement.
package places

import (
	_ "embed"
	"encoding/json"
	"fmt"
	"math"
)

// Place is a named point.
type Place struct {
	ID   string
	Name string
	Lat  float64
	Lon  float64
}

// Coord formats lat,lon the way the engine's route API expects.
func (p Place) Coord() string { return fmt.Sprintf("%.4f,%.4f", p.Lat, p.Lon) }

// Presets are Chennai's route quick picks.
var Presets []Place

// CityPresets are each city's route quick picks, the first two being the
// form's default ends. They are read from presets.json, which the engine's
// real-graph test (engine/tests/presets.rs) reads too: every preset snaps to
// its city's road graph, every pair routes for a car, and the default pair
// routes for a two-wheeler and a car. Several are places that are known to
// flood. Chennai's default pair, Kodambakkam station to Guindy station,
// crosses the simulated scenario's water, so the first click shows a route
// that bends around it (internal/demo/presets_live_test.go).
var CityPresets map[string][]Place

//go:embed presets.json
var presetsJSON []byte

type presetFile struct {
	Cities map[string][]struct {
		ID   string  `json:"id"`
		Name string  `json:"name"`
		Lat  float64 `json:"lat"`
		Lon  float64 `json:"lon"`
	} `json:"cities"`
}

func init() {
	var f presetFile
	if err := json.Unmarshal(presetsJSON, &f); err != nil {
		panic("places: presets.json: " + err.Error())
	}
	CityPresets = map[string][]Place{}
	for city, list := range f.Cities {
		for _, p := range list {
			CityPresets[city] = append(CityPresets[city], Place{p.ID, p.Name, p.Lat, p.Lon})
		}
	}
	Presets = CityPresets["chennai"]
}

// PresetByID finds a preset in any city.
func PresetByID(id string) (Place, bool) {
	for _, list := range CityPresets {
		for _, p := range list {
			if p.ID == id {
				return p, true
			}
		}
	}
	return Place{}, false
}

var localities = []Place{
	{"", "Velachery", 12.9791, 80.2209},
	{"", "Ram Nagar, Velachery", 12.9768, 80.2152},
	{"", "Guindy", 13.0067, 80.2206},
	{"", "T. Nagar", 13.0418, 80.2341},
	{"", "West Mambalam", 13.0367, 80.2220},
	{"", "Saidapet", 13.0213, 80.2231},
	{"", "Pallikaranai", 12.9349, 80.2137},
	{"", "Madipakkam", 12.9640, 80.1990},
	{"", "Tambaram", 12.9249, 80.1275},
	{"", "Chennai Central", 13.0827, 80.2707},
	{"", "Adyar", 13.0067, 80.2570},
	{"", "Anna Nagar", 13.0850, 80.2101},
	{"", "Egmore", 13.0732, 80.2609},
	{"", "Mylapore", 13.0339, 80.2697},
	{"", "Perungudi", 12.9654, 80.2461},
	{"", "Thoraipakkam", 12.9416, 80.2362},
	{"", "Ashok Nagar", 13.0359, 80.2104},
	{"", "K. K. Nagar", 13.0390, 80.1990},
	{"", "Kodambakkam", 13.0521, 80.2255},
	{"", "Vadapalani", 13.0500, 80.2121},
	{"", "Nungambakkam", 13.0569, 80.2425},
	{"", "Porur", 13.0382, 80.1565},
	{"", "Chromepet", 12.9516, 80.1462},
	{"", "Medavakkam", 12.9171, 80.1923},
	{"", "Sholinganallur", 12.9010, 80.2279},
	{"", "Navalur", 12.8456, 80.2272},
	{"", "Siruseri", 12.8368, 80.2034},
	{"", "Perambur", 13.1210, 80.2330},
	{"", "Kolathur", 13.1240, 80.2120},
	{"", "Royapuram", 13.1137, 80.2954},
	{"", "Besant Nagar", 13.0003, 80.2667},
	{"", "Nanganallur", 12.9767, 80.1866},
	{"", "Taramani", 12.9850, 80.2440},
	{"", "Kotturpuram", 13.0180, 80.2420},
}

// Landmarks are the localities worth labelling on a small map.
func Landmarks() []Place {
	keep := map[string]bool{
		"Velachery": true, "Guindy": true, "T. Nagar": true, "Saidapet": true, "Pallikaranai": true,
		"Madipakkam": true, "Adyar": true, "Perungudi": true, "Taramani": true, "Ashok Nagar": true, "Nanganallur": true,
	}
	var out []Place
	for _, p := range localities {
		if keep[p.Name] {
			out = append(out, p)
		}
	}
	return out
}

// Nearest is the closest locality (or another city's preset) within maxM
// metres, by name.
func Nearest(lat, lon, maxM float64) (string, bool) {
	best, bestD := "", math.Inf(1)
	for _, p := range nearAll() {
		if d := distanceM(lat, lon, p.Lat, p.Lon); d < bestD {
			best, bestD = p.Name, d
		}
	}
	return best, bestD <= maxM
}

func nearAll() []Place {
	all := localities
	for id, list := range CityPresets {
		if id != "chennai" {
			all = append(all[:len(all):len(all)], list...)
		}
	}
	return all
}

// Near names the closest locality (or city preset) within 2.5 km, or
// returns coordinates.
func Near(lat, lon float64) string {
	best, bestD := "", math.Inf(1)
	all := localities
	for id, list := range CityPresets {
		if id != "chennai" {
			all = append(all[:len(all):len(all)], list...)
		}
	}
	for _, p := range all {
		d := distanceM(lat, lon, p.Lat, p.Lon)
		if d < bestD {
			best, bestD = p.Name, d
		}
	}
	if bestD <= 2500 {
		return "near " + best
	}
	return fmt.Sprintf("at %.4f, %.4f", lat, lon)
}

func distanceM(lat1, lon1, lat2, lon2 float64) float64 {
	const r = 6371008.8
	p1, p2 := lat1*math.Pi/180, lat2*math.Pi/180
	dp := p2 - p1
	dl := (lon2 - lon1) * math.Pi / 180
	a := math.Sin(dp/2)*math.Sin(dp/2) + math.Cos(p1)*math.Cos(p2)*math.Sin(dl/2)*math.Sin(dl/2)
	return 2 * r * math.Asin(math.Min(1, math.Sqrt(a)))
}
