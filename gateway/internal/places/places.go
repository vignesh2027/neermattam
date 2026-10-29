// Package places is a tiny gazetteer of localities, used to say "near
// Velachery" instead of raw coordinates, and the route presets for each
// city. The coordinates are approximate centres taken from OpenStreetMap;
// they label places and seed route forms, they are never used for
// measurement.
package places

import (
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
var Presets = CityPresets["chennai"]

// CityPresets are each city's route quick picks, the first two being the
// form's default ends. Every one was checked to snap to its city's road
// graph and to route between each pair for a car (gateway tests and
// docs/GATEWAY.md). Several are places that are known to flood.
var CityPresets = map[string][]Place{
	"chennai": {
		{"velachery", "Velachery station", 12.9791, 80.2209},
		{"tnagar", "T. Nagar", 13.0418, 80.2341},
		{"guindy", "Guindy", 13.0067, 80.2206},
		{"tambaram", "Tambaram", 12.9249, 80.1275},
		{"central", "Chennai Central", 13.0827, 80.2707},
	},
	"mumbai": {
		{"dadar", "Dadar station", 19.0186, 72.8424},
		{"andheri", "Andheri station", 19.1197, 72.8464},
		{"csmt", "CSMT station", 18.9402, 72.8353},
		{"kurla", "Kurla station", 19.0655, 72.8794},
		{"bandra", "Bandra station", 19.0544, 72.8406},
	},
	"bengaluru": {
		{"majestic", "KSR Bengaluru station", 12.9781, 77.5697},
		{"silkboard", "Silk Board junction", 12.9172, 77.6227},
		{"mgroad", "MG Road", 12.9755, 77.6068},
		{"hebbal", "Hebbal", 13.0358, 77.5970},
		{"whitefield", "Whitefield", 12.9698, 77.7500},
	},
	"hyderabad": {
		{"secunderabad", "Secunderabad station", 17.4337, 78.5016},
		{"hitec", "HITEC City", 17.4474, 78.3762},
		{"ameerpet", "Ameerpet", 17.4375, 78.4483},
		{"charminar", "Charminar", 17.3616, 78.4747},
		{"lbnagar", "LB Nagar", 17.3457, 78.5522},
	},
	"kolkata": {
		{"howrah", "Howrah station", 22.5839, 88.3426},
		{"parkstreet", "Park Street", 22.5535, 88.3525},
		{"sealdah", "Sealdah station", 22.5678, 88.3707},
		{"saltlake", "Salt Lake Sector V", 22.5735, 88.4331},
		{"garia", "Garia", 22.4636, 88.3946},
	},
	"delhi": {
		{"ndls", "New Delhi station", 28.6427, 77.2197},
		{"ito", "ITO", 28.6286, 77.2410},
		{"cp", "Connaught Place", 28.6315, 77.2167},
		{"aiims", "AIIMS", 28.5672, 77.2100},
		{"dwarka", "Dwarka Sector 21", 28.5523, 77.0583},
	},
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
