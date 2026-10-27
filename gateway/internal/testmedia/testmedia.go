// Package testmedia builds small, valid media files for tests: JPEGs with or
// without EXIF capture times, PNGs and MP4 headers of a chosen length.
package testmedia

import (
	"bytes"
	"encoding/binary"
	"image"
	"image/color"
	"image/jpeg"
	"image/png"
	"math"
)

// Scene draws a deterministic picture; different seeds give different scenes.
func Scene(w, h int, seed int) *image.RGBA {
	img := image.NewRGBA(image.Rect(0, 0, w, h))
	cx, cy := float64(w)*(0.3+0.1*float64(seed%5)), float64(h)*0.45
	r := float64(h) * 0.25
	for y := 0; y < h; y++ {
		for x := 0; x < w; x++ {
			c := color.RGBA{uint8(150 + 40*math.Sin(float64(x+seed*37)/23)), uint8(160 + 30*math.Cos(float64(y)/17)), 190, 255}
			if y > h*6/10 {
				c = color.RGBA{60, 120, 160, 255}
			}
			if d := math.Hypot(float64(x)-cx, float64(y)-cy); d < r {
				c = color.RGBA{20, 20, 24, 255}
				if d < r*0.55 {
					c = color.RGBA{190, 190, 190, 255}
				}
			}
			img.Set(x, y, c)
		}
	}
	return img
}

// JPEG encodes an image.
func JPEG(img image.Image, quality int) []byte {
	var b bytes.Buffer
	_ = jpeg.Encode(&b, img, &jpeg.Options{Quality: quality})
	return b.Bytes()
}

// PNG encodes an image.
func PNG(img image.Image) []byte {
	var b bytes.Buffer
	_ = png.Encode(&b, img)
	return b.Bytes()
}

// WithExif inserts an APP1 Exif segment carrying DateTimeOriginal
// ("2006:01:02 15:04:05") and, when offset is not empty, OffsetTimeOriginal.
func WithExif(jpg []byte, dateTime, offset string) []byte {
	le := binary.LittleEndian
	var t bytes.Buffer
	t.WriteString("II")
	_ = binary.Write(&t, le, uint16(42))
	_ = binary.Write(&t, le, uint32(8))
	// IFD0 at 8: one entry pointing at the Exif IFD.
	exifIFD := uint32(8 + 2 + 12 + 4)
	_ = binary.Write(&t, le, uint16(1))
	_ = binary.Write(&t, le, uint16(0x8769))
	_ = binary.Write(&t, le, uint16(4))
	_ = binary.Write(&t, le, uint32(1))
	_ = binary.Write(&t, le, exifIFD)
	_ = binary.Write(&t, le, uint32(0))
	n := uint16(1)
	if offset != "" {
		n = 2
	}
	data := exifIFD + 2 + uint32(n)*12 + 4
	dt := append([]byte(dateTime), 0)
	_ = binary.Write(&t, le, n)
	_ = binary.Write(&t, le, uint16(0x9003))
	_ = binary.Write(&t, le, uint16(2))
	_ = binary.Write(&t, le, uint32(len(dt)))
	_ = binary.Write(&t, le, data)
	var oz []byte
	if offset != "" {
		oz = append([]byte(offset), 0)
		_ = binary.Write(&t, le, uint16(0x9011))
		_ = binary.Write(&t, le, uint16(2))
		_ = binary.Write(&t, le, uint32(len(oz)))
		_ = binary.Write(&t, le, data+uint32(len(dt)))
	}
	_ = binary.Write(&t, le, uint32(0))
	t.Write(dt)
	t.Write(oz)

	seg := append([]byte("Exif\x00\x00"), t.Bytes()...)
	var out bytes.Buffer
	out.Write(jpg[:2])
	out.Write([]byte{0xFF, 0xE1})
	_ = binary.Write(&out, binary.BigEndian, uint16(len(seg)+2))
	out.Write(seg)
	out.Write(jpg[2:])
	return out.Bytes()
}

func box(typ string, payload []byte) []byte {
	var b bytes.Buffer
	_ = binary.Write(&b, binary.BigEndian, uint32(8+len(payload)))
	b.WriteString(typ)
	b.Write(payload)
	return b.Bytes()
}

// MP4 returns an MP4 (brand "isom") or QuickTime (brand "qt  ") header whose
// movie lasts seconds, padded to size bytes.
func MP4(brand string, seconds float64, size int) []byte {
	ftyp := box("ftyp", append([]byte(brand+"\x00\x00\x00\x00"), []byte(brand)...))
	var mv bytes.Buffer
	mv.Write([]byte{0, 0, 0, 0}) // version 0, flags
	mv.Write(make([]byte, 8))    // creation and modification times
	_ = binary.Write(&mv, binary.BigEndian, uint32(600))
	_ = binary.Write(&mv, binary.BigEndian, uint32(seconds*600))
	mv.Write(make([]byte, 80))
	moov := box("moov", box("mvhd", mv.Bytes()))
	out := append(ftyp, moov...)
	if pad := size - len(out) - 8; pad > 0 {
		out = append(out, box("mdat", make([]byte, pad))...)
	}
	return out
}
