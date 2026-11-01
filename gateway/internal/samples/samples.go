// Package samples holds a few real flood photos that a visitor can send
// through the live measuring pipeline in one tap, without a photo of their
// own. Faces and number plates are blurred, the files carry no metadata, and
// every photo is credited. A sample is measured for real, every time, but it
// is never added to the live map: the water in it is years old.
package samples

import (
	"embed"
	"encoding/json"
	"fmt"
	"sync"
)

//go:embed samples.json photos/*.jpg
var files embed.FS

// Sample is one photo.
type Sample struct {
	ID     string  `json:"id"`
	EvalID string  `json:"eval_id"` // the photo's id in eval/RESULTS_REAL.md
	City   string  `json:"city"`
	Place  string  `json:"place"`
	Lat    float64 `json:"lat"`
	Lon    float64 `json:"lon"`
	// Taken is the date the photo was taken (YYYY-MM-DD), empty when the
	// source does not give one.
	Taken      string `json:"taken"`
	Kind       string `json:"kind"` // what the ruler will probably be: wheel, person or car
	Title      string `json:"title"`
	Author     string `json:"author"`
	Licence    string `json:"licence"`
	LicenceURL string `json:"licence_url"`
	SourceURL  string `json:"source_url"`
}

// Thumb is the thumbnail's static path, for the asset helper.
func (s Sample) Thumb() string { return "samples/" + s.ID + ".webp" }

// Year is the year the photo was taken, or "".
func (s Sample) Year() string {
	if len(s.Taken) >= 4 {
		return s.Taken[:4]
	}
	return ""
}

// Photo returns the photo's bytes.
func (s Sample) Photo() ([]byte, error) {
	return files.ReadFile("photos/" + s.ID + ".jpg")
}

var (
	once sync.Once
	list []Sample
	err  error
)

func load() {
	var doc struct {
		Samples []Sample `json:"samples"`
	}
	b, e := files.ReadFile("samples.json")
	if e != nil {
		err = e
		return
	}
	if e := json.Unmarshal(b, &doc); e != nil {
		err = fmt.Errorf("samples.json: %w", e)
		return
	}
	list = doc.Samples
}

// All returns every sample in display order.
func All() []Sample {
	once.Do(load)
	return list
}

// Get returns one sample by id.
func Get(id string) (Sample, bool) {
	for _, s := range All() {
		if s.ID == id {
			return s, true
		}
	}
	return Sample{}, false
}

// Err reports a problem reading the embedded list, for tests.
func Err() error {
	once.Do(load)
	return err
}
