// Package media identifies uploads by their bytes, never by the name or the
// Content-Type the client claims, and reads the few facts the gateway needs:
// video length from the MP4 header, capture time from EXIF, and a
// perceptual hash for near-duplicate photos.
package media

import (
	"bytes"
	"encoding/binary"
	"errors"
	"fmt"
	"image"
	"image/color"
	_ "image/jpeg" // register decoders for DecodeConfig and Decode
	_ "image/png"
	"math/bits"
	"time"
)

// Kind is the broad class of an upload.
type Kind string

// Upload kinds.
const (
	Image Kind = "image"
	Video Kind = "video"
	Audio Kind = "audio"
)

// Type is a sniffed media type.
type Type struct {
	Kind Kind
	MIME string
	Ext  string
}

// ErrUnsupported means the bytes are not a format perception accepts.
var ErrUnsupported = errors.New("unsupported media type")

// Sniff identifies a JPEG, PNG, MP4, QuickTime, Ogg, WAV or M4A file from its
// first bytes. The formats match what perception accepts (CONTRACTS.md 1).
func Sniff(b []byte) (Type, error) {
	switch {
	case len(b) >= 3 && b[0] == 0xFF && b[1] == 0xD8 && b[2] == 0xFF:
		return Type{Image, "image/jpeg", ".jpg"}, nil
	case bytes.HasPrefix(b, []byte("\x89PNG\r\n\x1a\n")):
		return Type{Image, "image/png", ".png"}, nil
	case bytes.HasPrefix(b, []byte("OggS")):
		return Type{Audio, "audio/ogg", ".ogg"}, nil
	case len(b) >= 12 && bytes.Equal(b[0:4], []byte("RIFF")) && bytes.Equal(b[8:12], []byte("WAVE")):
		return Type{Audio, "audio/wav", ".wav"}, nil
	case len(b) >= 12 && bytes.Equal(b[4:8], []byte("ftyp")):
		brand := string(b[8:12])
		switch brand {
		case "M4A ", "M4B ":
			return Type{Audio, "audio/mp4", ".m4a"}, nil
		case "qt  ":
			return Type{Video, "video/quicktime", ".mov"}, nil
		case "heic", "heix", "mif1", "msf1", "avif":
			return Type{}, fmt.Errorf("%w: HEIC or AVIF photo; please send a JPEG or PNG", ErrUnsupported)
		default:
			return Type{Video, "video/mp4", ".mp4"}, nil
		}
	case bytes.HasPrefix(b, []byte("RIFF")) && len(b) >= 12 && bytes.Equal(b[8:12], []byte("WEBP")):
		return Type{}, fmt.Errorf("%w: WebP photo; please send a JPEG or PNG", ErrUnsupported)
	}
	return Type{}, ErrUnsupported
}

// ErrNoDuration means the MP4 has no readable movie header.
var ErrNoDuration = errors.New("could not read the video length")

// MP4Duration reads the duration from the `mvhd` box inside `moov`. It works
// for MP4 and QuickTime files, including those with 64-bit box sizes, and
// never reads past the slice.
func MP4Duration(b []byte) (time.Duration, error) {
	moov, ok := findBox(b, "moov")
	if !ok {
		return 0, ErrNoDuration
	}
	mvhd, ok := findBox(moov, "mvhd")
	if !ok || len(mvhd) < 4 {
		return 0, ErrNoDuration
	}
	version := mvhd[0]
	var timescale uint32
	var duration uint64
	switch version {
	case 0:
		if len(mvhd) < 20 {
			return 0, ErrNoDuration
		}
		timescale = binary.BigEndian.Uint32(mvhd[12:16])
		duration = uint64(binary.BigEndian.Uint32(mvhd[16:20]))
	case 1:
		if len(mvhd) < 32 {
			return 0, ErrNoDuration
		}
		timescale = binary.BigEndian.Uint32(mvhd[20:24])
		duration = binary.BigEndian.Uint64(mvhd[24:32])
	default:
		return 0, ErrNoDuration
	}
	if timescale == 0 {
		return 0, ErrNoDuration
	}
	secs := float64(duration) / float64(timescale)
	if secs > 24*3600 {
		return 0, ErrNoDuration
	}
	return time.Duration(secs * float64(time.Second)), nil
}

// findBox scans sibling boxes in b and returns the payload of the first one
// named typ.
func findBox(b []byte, typ string) ([]byte, bool) {
	for len(b) >= 8 {
		size := uint64(binary.BigEndian.Uint32(b[0:4]))
		name := string(b[4:8])
		hdr := uint64(8)
		switch size {
		case 0:
			size = uint64(len(b))
		case 1:
			if len(b) < 16 {
				return nil, false
			}
			size = binary.BigEndian.Uint64(b[8:16])
			hdr = 16
		}
		if size < hdr || size > uint64(len(b)) {
			return nil, false
		}
		if name == typ {
			return b[hdr:size], true
		}
		b = b[size:]
	}
	return nil, false
}

// CheckImage decodes only the header and refuses images whose pixel count
// could exhaust memory when decoded (a decompression bomb).
func CheckImage(b []byte, maxPixels int) (image.Config, error) {
	cfg, _, err := image.DecodeConfig(bytes.NewReader(b))
	if err != nil {
		return cfg, fmt.Errorf("the image could not be read: %w", err)
	}
	if cfg.Width <= 0 || cfg.Height <= 0 {
		return cfg, errors.New("the image has no pixels")
	}
	if cfg.Width*cfg.Height > maxPixels {
		return cfg, fmt.Errorf("the image is %dx%d, larger than the %d megapixel limit", cfg.Width, cfg.Height, maxPixels/1_000_000)
	}
	return cfg, nil
}

// DHash computes a 64-bit difference hash: the image is shrunk to 9x8 grey
// pixels by area averaging and each bit records whether a pixel is brighter
// than its right-hand neighbour. Re-encoded, resized or lightly cropped
// copies of a photo land within a few bits of each other.
func DHash(img image.Image) uint64 {
	const w, h = 9, 8
	b := img.Bounds()
	var grid [h][w]float64
	bw, bh := b.Dx(), b.Dy()
	if bw == 0 || bh == 0 {
		return 0
	}
	for gy := 0; gy < h; gy++ {
		y0 := b.Min.Y + gy*bh/h
		y1 := b.Min.Y + (gy+1)*bh/h
		if y1 <= y0 {
			y1 = y0 + 1
		}
		for gx := 0; gx < w; gx++ {
			x0 := b.Min.X + gx*bw/w
			x1 := b.Min.X + (gx+1)*bw/w
			if x1 <= x0 {
				x1 = x0 + 1
			}
			// Sample at most 16x16 points per block; that is plenty for a
			// 72-pixel thumbnail and keeps a 12 MP photo cheap.
			stepX := max(1, (x1-x0)/16)
			stepY := max(1, (y1-y0)/16)
			var sum float64
			var n int
			for y := y0; y < y1; y += stepY {
				for x := x0; x < x1; x += stepX {
					g := color.GrayModel.Convert(img.At(x, y)).(color.Gray)
					sum += float64(g.Y)
					n++
				}
			}
			grid[gy][gx] = sum / float64(n)
		}
	}
	var hash uint64
	for y := 0; y < h; y++ {
		for x := 0; x < w-1; x++ {
			hash <<= 1
			if grid[y][x] > grid[y][x+1] {
				hash |= 1
			}
		}
	}
	return hash
}

// ImageDHash decodes the image and returns its difference hash.
func ImageDHash(b []byte) (uint64, error) {
	img, _, err := image.Decode(bytes.NewReader(b))
	if err != nil {
		return 0, err
	}
	return DHash(img), nil
}

// Hamming is the number of differing bits between two hashes.
func Hamming(a, b uint64) int { return bits.OnesCount64(a ^ b) }
