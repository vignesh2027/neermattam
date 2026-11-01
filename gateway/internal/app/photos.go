package app

import (
	"log/slog"

	"neermattam/gateway/internal/photos"
)

// loadPhotos reads the measured archival photos embedded in the binary. A
// bad record is a build mistake; the map then simply has no photo layer.
func loadPhotos(log *slog.Logger) *photos.Set {
	s, err := photos.Load()
	if err != nil {
		log.Error("measured photos not loaded", "error", err.Error())
		return &photos.Set{}
	}
	log.Info("measured photos loaded", "photos", len(s.List))
	return s
}
