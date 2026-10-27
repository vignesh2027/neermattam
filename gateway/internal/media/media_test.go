package media

import (
	"bytes"
	"encoding/binary"
	"testing"
	"time"

	"neermattam/gateway/internal/testmedia"
)

func TestSniff(t *testing.T) {
	img := testmedia.Scene(64, 48, 1)
	cases := []struct {
		name string
		data []byte
		kind Kind
		mime string
	}{
		{"jpeg", testmedia.JPEG(img, 80), Image, "image/jpeg"},
		{"png", testmedia.PNG(img), Image, "image/png"},
		{"mp4", testmedia.MP4("isom", 5, 0), Video, "video/mp4"},
		{"mov", testmedia.MP4("qt  ", 5, 0), Video, "video/quicktime"},
		{"ogg", []byte("OggS\x00\x02rest of a voice note"), Audio, "audio/ogg"},
		{"wav", []byte("RIFF\x24\x00\x00\x00WAVEfmt "), Audio, "audio/wav"},
		{"m4a", testmedia.MP4("M4A ", 5, 0), Audio, "audio/mp4"},
	}
	for _, c := range cases {
		got, err := Sniff(c.data)
		if err != nil || got.Kind != c.kind || got.MIME != c.mime {
			t.Errorf("%s: got %+v, %v", c.name, got, err)
		}
	}
	for name, data := range map[string][]byte{
		"html":  []byte("<!doctype html><script>alert(1)</script>"),
		"gif":   []byte("GIF89a......"),
		"heic":  testmedia.MP4("heic", 0, 0),
		"webp":  []byte("RIFF\x24\x00\x00\x00WEBPVP8 "),
		"empty": nil,
	} {
		if _, err := Sniff(data); err == nil {
			t.Errorf("%s: accepted", name)
		}
	}
}

func TestMP4Duration(t *testing.T) {
	d, err := MP4Duration(testmedia.MP4("isom", 12.5, 4096))
	if err != nil || d != 12500*time.Millisecond {
		t.Fatalf("got %v, %v", d, err)
	}
	// Version 1 mvhd with 64-bit fields and a 64-bit box size.
	var mv bytes.Buffer
	mv.Write([]byte{1, 0, 0, 0})
	mv.Write(make([]byte, 16))
	_ = binary.Write(&mv, binary.BigEndian, uint32(1000))
	_ = binary.Write(&mv, binary.BigEndian, uint64(31_000))
	mv.Write(make([]byte, 80))
	mvhd := append([]byte{0, 0, 0, 0}, []byte("mvhd")...)
	binary.BigEndian.PutUint32(mvhd, uint32(8+mv.Len()))
	mvhd = append(mvhd, mv.Bytes()...)
	var moov bytes.Buffer
	_ = binary.Write(&moov, binary.BigEndian, uint32(1))
	moov.WriteString("moov")
	_ = binary.Write(&moov, binary.BigEndian, uint64(16+len(mvhd)))
	moov.Write(mvhd)
	d, err = MP4Duration(append(testmedia.MP4("isom", 0, 0)[:20], moov.Bytes()...))
	if err != nil || d != 31*time.Second {
		t.Fatalf("v1: got %v, %v", d, err)
	}
	for name, b := range map[string][]byte{
		"truncated": testmedia.MP4("isom", 10, 0)[:30],
		"no moov":   []byte("\x00\x00\x00\x10ftypisom\x00\x00\x00\x00"),
		"bad size":  []byte("\xff\xff\xff\xffmoov"),
	} {
		if _, err := MP4Duration(b); err == nil {
			t.Errorf("%s: no error", name)
		}
	}
}

func TestExifCapture(t *testing.T) {
	jpg := testmedia.JPEG(testmedia.Scene(32, 32, 2), 80)
	if _, ok := ExifCapture(jpg); ok {
		t.Fatal("found a capture time in a JPEG without EXIF")
	}
	c, ok := ExifCapture(testmedia.WithExif(jpg, "2026:10:27 09:30:00", ""))
	if !ok || c.ZoneKnown {
		t.Fatalf("got %+v %v", c, ok)
	}
	if want := time.Date(2026, 10, 27, 4, 0, 0, 0, time.UTC); !c.Time.Equal(want) {
		t.Errorf("without an offset the time is read as IST: got %v want %v", c.Time, want)
	}
	c, ok = ExifCapture(testmedia.WithExif(jpg, "2026:10:27 09:30:00", "+00:00"))
	if !ok || !c.ZoneKnown || !c.Time.Equal(time.Date(2026, 10, 27, 9, 30, 0, 0, time.UTC)) {
		t.Errorf("with offset: %+v %v", c, ok)
	}
	if _, ok := ExifCapture(testmedia.WithExif(jpg, "not a date", "")); ok {
		t.Error("accepted a malformed date")
	}
	if _, ok := ExifCapture([]byte{0xFF, 0xD8, 0xFF, 0xE1, 0xFF, 0xFF}); ok {
		t.Error("accepted a truncated segment")
	}
}

func TestDHashFindsReencodedCopies(t *testing.T) {
	a := testmedia.Scene(320, 240, 1)
	orig, _ := ImageDHash(testmedia.JPEG(a, 92))
	recoded, _ := ImageDHash(testmedia.JPEG(a, 35))
	asPNG, _ := ImageDHash(testmedia.PNG(a))
	other, _ := ImageDHash(testmedia.JPEG(testmedia.Scene(320, 240, 3), 92))
	if d := Hamming(orig, recoded); d > 5 {
		t.Errorf("re-encoded copy is %d bits away", d)
	}
	if d := Hamming(orig, asPNG); d > 5 {
		t.Errorf("PNG copy is %d bits away", d)
	}
	if d := Hamming(orig, other); d <= 5 {
		t.Errorf("a different scene is only %d bits away", d)
	}
}

func TestCheckImageRefusesHugeDimensions(t *testing.T) {
	jpg := testmedia.JPEG(testmedia.Scene(200, 100, 1), 80)
	if _, err := CheckImage(jpg, 10_000); err == nil {
		t.Fatal("20,000 pixels accepted under a 10,000 pixel limit")
	}
	if _, err := CheckImage(jpg, 50_000); err != nil {
		t.Fatal(err)
	}
	if _, err := CheckImage([]byte("\xff\xd8\xff garbage"), 50_000); err == nil {
		t.Fatal("garbage accepted")
	}
}
