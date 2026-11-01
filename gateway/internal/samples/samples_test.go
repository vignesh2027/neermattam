package samples

import (
	"bytes"
	"image/jpeg"
	"os"
	"strings"
	"testing"
)

func TestEverySampleHasItsPhotoThumbAndCredit(t *testing.T) {
	if err := Err(); err != nil {
		t.Fatal(err)
	}
	if n := len(All()); n < 3 {
		t.Fatalf("%d samples, want at least 3", n)
	}
	seen := map[string]bool{}
	for _, s := range All() {
		if seen[s.ID] {
			t.Errorf("duplicate id %q", s.ID)
		}
		seen[s.ID] = true
		if s.Author == "" || s.Licence == "" || !strings.HasPrefix(s.SourceURL, "https://") || !strings.HasPrefix(s.LicenceURL, "https://") {
			t.Errorf("%s: incomplete credit %+v", s.ID, s)
		}
		if s.City == "" || s.Place == "" || s.Lat == 0 || s.Lon == 0 {
			t.Errorf("%s: no place", s.ID)
		}
		b, err := s.Photo()
		if err != nil {
			t.Fatalf("%s: %v", s.ID, err)
		}
		cfg, err := jpeg.DecodeConfig(bytes.NewReader(b))
		if err != nil {
			t.Fatalf("%s: not a JPEG: %v", s.ID, err)
		}
		if max(cfg.Width, cfg.Height) > 1600 {
			t.Errorf("%s: %dx%d, want at most 1600 px", s.ID, cfg.Width, cfg.Height)
		}
		// No EXIF: the photos are re-encoded without metadata, so no
		// camera, place or time travels with them.
		if bytes.Contains(b[:min(len(b), 4096)], []byte("Exif\x00")) {
			t.Errorf("%s: carries EXIF", s.ID)
		}
		if _, err := os.Stat("../web/static/" + s.Thumb()); err != nil {
			t.Errorf("%s: no thumbnail: %v", s.ID, err)
		}
	}
	if _, ok := Get("nandanam"); !ok {
		t.Error("Get(nandanam) found nothing")
	}
	if _, ok := Get("../etc/passwd"); ok {
		t.Error("Get accepted an unknown id")
	}
}
