// Package places is a tiny gazetteer of Chennai localities, used to say
// "near Velachery" instead of raw coordinates, and the route presets. The
// coordinates are approximate locality centres taken from OpenStreetMap; they
// label places, they are never used for measurement.
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

// Presets are the route page's quick picks. Each has been checked to snap to
// the road graph for every vehicle.
var Presets = []Place{
	{"velachery", "Velachery station", 12.9791, 80.2209},
	{"guindy", "Guindy", 13.0067, 80.2206},
	{"tnagar", "T. Nagar", 13.0418, 80.2341},
	{"tambaram", "Tambaram", 12.9249, 80.1275},
	{"central", "Chennai Central", 13.0827, 80.2707},
}

// PresetByID finds a preset.
func PresetByID(id string) (Place, bool) {
	for _, p := range Presets {
		if p.ID == id {
			return p, true
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

// Near names the closest locality within 2.5 km, or returns coordinates.
func Near(lat, lon float64) string {
	best, bestD := "", math.Inf(1)
	for _, p := range localities {
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
