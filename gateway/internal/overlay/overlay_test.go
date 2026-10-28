package overlay

import (
	"bytes"
	"encoding/base64"
	"image"
	"image/jpeg"
	"testing"
	"time"

	"neermattam/gateway/internal/testmedia"
)

func TestReencodeShrinksToJPEG(t *testing.T) {
	// Photo-like content: a scene with sensor noise, as a real overlay has.
	scene := testmedia.Scene(2048, 1536, 3)
	seed := uint32(7)
	for i := 0; i < len(scene.Pix); i += 4 {
		seed = seed*1664525 + 1013904223
		n := int(seed>>27) - 16
		for c := 0; c < 3; c++ {
			scene.Pix[i+c] = uint8(max(0, min(255, int(scene.Pix[i+c])+n)))
		}
	}
	png := testmedia.PNG(scene)
	out, err := Reencode(base64.StdEncoding.EncodeToString(png), MaxSide, Quality)
	if err != nil {
		t.Fatal(err)
	}
	img, format, err := image.Decode(bytes.NewReader(out))
	if err != nil || format != "jpeg" {
		t.Fatalf("not a JPEG: %v %s", err, format)
	}
	if b := img.Bounds(); b.Dx() != 1280 || b.Dy() != 960 {
		t.Fatalf("size %v, want 1280x960", b)
	}
	if len(out) >= len(png) {
		t.Fatalf("JPEG %d bytes is not smaller than the PNG's %d", len(out), len(png))
	}
	small := testmedia.PNG(testmedia.Scene(400, 300, 1))
	out, _ = Reencode(base64.StdEncoding.EncodeToString(small), MaxSide, Quality)
	if cfg, _ := jpeg.DecodeConfig(bytes.NewReader(out)); cfg.Width != 400 {
		t.Fatalf("a small overlay must not be scaled: %d", cfg.Width)
	}
	if _, err := Reencode("not base64!", MaxSide, Quality); err == nil {
		t.Fatal("garbage accepted")
	}
}

func TestStoreBoundsAndExpiry(t *testing.T) {
	now := time.Unix(0, 0)
	s := NewStore(time.Hour, 2, 1<<20).WithClock(func() time.Time { return now })
	s.Put("a", []byte("1"))
	s.Put("b", []byte("2"))
	s.Get("a") // a is now the most recent
	s.Put("c", []byte("3"))
	if _, ok := s.Get("b"); ok {
		t.Fatal("the least recently used entry should be evicted")
	}
	if _, ok := s.Get("a"); !ok {
		t.Fatal("a was used recently and must stay")
	}
	now = now.Add(61 * time.Minute)
	if _, ok := s.Get("c"); ok {
		t.Fatal("expired entry served")
	}
	big := NewStore(time.Hour, 100, 10)
	big.Put("x", make([]byte, 8))
	big.Put("y", make([]byte, 8))
	if big.Len() != 1 {
		t.Fatalf("byte bound not applied: %d entries", big.Len())
	}
	if ValidID("../etc") || !ValidID("m_01JABC-x") {
		t.Fatal("ValidID")
	}
}
