// Package photos holds the measured archival photos: real street flood
// photos from Wikimedia Commons, one or more per city where a suitable one
// was found, each measured once by the real perception service with its
// signed certificate (eval/india_photos.py writes records/<id>.json).
//
// They are archival: the photos are years old, so they are shown as a
// separate, labelled layer ("Photo from 2008, measured by Neermattam on
// 1 November 2026") and never enter live fusion, the live stats or the
// engine.
package photos

import (
	"embed"
	"encoding/json"
	"fmt"
	"io/fs"
	"sort"
	"strings"
	"time"

	"neermattam/gateway/internal/upstream"
)

//go:embed all:records
var dataFS embed.FS

// Photo is one measured archival photo.
type Photo struct {
	ID          string  `json:"id"`
	City        string  `json:"city"`
	Place       string  `json:"place"`
	Lat         float64 `json:"lat"`
	Lon         float64 `json:"lon"`
	CoordSource string  `json:"coord_source"` // camera_geotag or place_name
	PhotoDate   string  `json:"photo_date"`   // YYYY-MM-DD
	MeasuredOn  string  `json:"measured_on"`  // YYYY-MM-DD
	Title       string  `json:"title"`
	SourceURL   string  `json:"source_url"`
	Author      string  `json:"author"`
	Licence     string  `json:"licence"`
	LicenceURL  string  `json:"licence_url"`
	// FileSHA256 is the SHA-256 of the file sent to perception, computed by
	// the script that sent it.
	FileSHA256 string `json:"file_sha256"`

	// Measurement is perception's whole answer, its certificate included
	// (kept raw, so its bytes are served as perception signed them).
	Measurement upstream.Measurement `json:"measurement"`
}

// Year is the year the photo was taken.
func (p Photo) Year() string {
	if len(p.PhotoDate) >= 4 {
		return p.PhotoDate[:4]
	}
	return ""
}

// MeasurementID is perception's id for the measurement.
func (p Photo) MeasurementID() string { return p.Measurement.MeasurementID }

// Method is wheel_geometry, person_reference or model_estimate; "" when
// perception could not measure the photo.
func (p Photo) Method() string {
	if !p.Measurement.Usable || p.Measurement.Method == nil {
		return ""
	}
	switch m := *p.Measurement.Method; m {
	case "wheel_geometry", "person_reference":
		return m
	default:
		// A method this gateway does not know is shown as an estimate.
		return "model_estimate"
	}
}

// Credit is "Author, Licence".
func (p Photo) Credit() string {
	return strings.TrimSpace(p.Author) + ", " + p.Licence
}

// MeasuredAt is when the measurement was made: the signing time in the
// certificate's record (issued_at), else the measured_on date at midnight
// UTC. Exact reports whether the time of day is real; a bare date must be
// shown as a date only, never as a clock time.
func (p Photo) MeasuredAt() (t time.Time, exact bool) {
	if t, ok := issuedAt(p.Measurement.Certificate); ok {
		return t, true
	}
	t, err := time.Parse("2006-01-02", p.MeasuredOn)
	if err != nil {
		return time.Time{}, false
	}
	return t, false
}

// issuedAt reads issued_at from a certificate's signed payload without
// checking the signature; the verify page checks it.
func issuedAt(cert json.RawMessage) (time.Time, bool) {
	var env struct {
		Payload string `json:"payload"`
	}
	if len(cert) == 0 || json.Unmarshal(cert, &env) != nil || env.Payload == "" {
		return time.Time{}, false
	}
	var rec struct {
		IssuedAt string `json:"issued_at"`
	}
	if json.Unmarshal([]byte(env.Payload), &rec) != nil {
		return time.Time{}, false
	}
	t, err := time.Parse(time.RFC3339, rec.IssuedAt)
	if err != nil {
		return time.Time{}, false
	}
	return t.UTC(), true
}

// Set is every measured photo, in city then id order.
type Set struct {
	List []Photo
	byID map[string]int // by measurement id
}

// Load reads the embedded records.
func Load() (*Set, error) { return load(dataFS) }

func load(fsys fs.FS) (*Set, error) {
	s := &Set{byID: map[string]int{}}
	err := fs.WalkDir(fsys, ".", func(path string, d fs.DirEntry, err error) error {
		if err != nil || d.IsDir() || !strings.HasSuffix(path, ".json") {
			return err
		}
		raw, err := fs.ReadFile(fsys, path)
		if err != nil {
			return err
		}
		var p Photo
		if err := json.Unmarshal(raw, &p); err != nil {
			return fmt.Errorf("%s: %w", path, err)
		}
		if p.ID == "" || p.City == "" || p.Measurement.MeasurementID == "" {
			return fmt.Errorf("%s: id, city and measurement id are required", path)
		}
		s.List = append(s.List, p)
		return nil
	})
	if err != nil {
		return nil, err
	}
	sort.Slice(s.List, func(i, j int) bool {
		if s.List[i].City != s.List[j].City {
			return s.List[i].City < s.List[j].City
		}
		return s.List[i].ID < s.List[j].ID
	})
	for i, p := range s.List {
		s.byID[p.MeasurementID()] = i
	}
	return s, nil
}

// ByMeasurement finds a photo by its measurement id.
func (s *Set) ByMeasurement(id string) (Photo, bool) {
	if s == nil {
		return Photo{}, false
	}
	i, ok := s.byID[id]
	if !ok {
		return Photo{}, false
	}
	return s.List[i], true
}

// InCity lists a city's photos.
func (s *Set) InCity(city string) []Photo {
	if s == nil {
		return nil
	}
	var out []Photo
	for _, p := range s.List {
		if p.City == city {
			out = append(out, p)
		}
	}
	return out
}

// Count is how many photos a city has.
func (s *Set) Count(city string) int { return len(s.InCity(city)) }
