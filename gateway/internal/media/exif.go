package media

import (
	"bytes"
	"encoding/binary"
	"strings"
	"time"
)

// Capture is when a photo says it was taken.
type Capture struct {
	Time time.Time
	// ZoneKnown is true when the file carried OffsetTimeOriginal. Without it
	// the camera's local time is assumed to be India Standard Time.
	ZoneKnown bool
}

// IST is India Standard Time, UTC+05:30, with no daylight saving.
var IST = time.FixedZone("IST", 5*3600+30*60)

const (
	tagExifIFD            = 0x8769
	tagDateTimeOriginal   = 0x9003
	tagOffsetTimeOriginal = 0x9011
)

// ExifCapture reads DateTimeOriginal (and OffsetTimeOriginal when present)
// from a JPEG's APP1 Exif segment. It returns false when the photo has no
// usable capture time, which is normal: WhatsApp and many apps strip EXIF.
func ExifCapture(jpeg []byte) (Capture, bool) {
	tiff, ok := exifTIFF(jpeg)
	if !ok {
		return Capture{}, false
	}
	var order binary.ByteOrder
	switch string(tiff[0:2]) {
	case "II":
		order = binary.LittleEndian
	case "MM":
		order = binary.BigEndian
	default:
		return Capture{}, false
	}
	if order.Uint16(tiff[2:4]) != 42 {
		return Capture{}, false
	}
	ifd0 := order.Uint32(tiff[4:8])
	exifOff, ok := findTag(tiff, order, ifd0, tagExifIFD)
	if !ok {
		return Capture{}, false
	}
	off, ok := tagUint32(tiff, order, exifOff)
	if !ok {
		return Capture{}, false
	}
	dtEntry, ok := findTag(tiff, order, off, tagDateTimeOriginal)
	if !ok {
		return Capture{}, false
	}
	dt, ok := tagASCII(tiff, order, dtEntry)
	if !ok {
		return Capture{}, false
	}
	zone := IST
	known := false
	if ozEntry, ok := findTag(tiff, order, off, tagOffsetTimeOriginal); ok {
		if oz, ok := tagASCII(tiff, order, ozEntry); ok {
			if t, err := time.Parse("-07:00", oz); err == nil {
				_, secs := t.Zone()
				zone = time.FixedZone(oz, secs)
				known = true
			}
		}
	}
	t, err := time.ParseInLocation("2006:01:02 15:04:05", dt, zone)
	if err != nil || t.Year() < 2000 {
		return Capture{}, false
	}
	return Capture{Time: t.UTC(), ZoneKnown: known}, true
}

// exifTIFF walks JPEG markers to the APP1 "Exif\0\0" segment and returns the
// TIFF block inside it.
func exifTIFF(b []byte) ([]byte, bool) {
	if len(b) < 4 || b[0] != 0xFF || b[1] != 0xD8 {
		return nil, false
	}
	i := 2
	for i+4 <= len(b) {
		if b[i] != 0xFF {
			return nil, false
		}
		marker := b[i+1]
		if marker == 0xD8 || (marker >= 0xD0 && marker <= 0xD7) || marker == 0x01 {
			i += 2
			continue
		}
		if marker == 0xDA || marker == 0xD9 { // start of scan or end: no more headers
			return nil, false
		}
		segLen := int(binary.BigEndian.Uint16(b[i+2 : i+4]))
		if segLen < 2 || i+2+segLen > len(b) {
			return nil, false
		}
		seg := b[i+4 : i+2+segLen]
		if marker == 0xE1 && bytes.HasPrefix(seg, []byte("Exif\x00\x00")) && len(seg) >= 14 {
			return seg[6:], true
		}
		i += 2 + segLen
	}
	return nil, false
}

// findTag returns the offset of the 12-byte IFD entry for tag.
func findTag(tiff []byte, order binary.ByteOrder, ifd uint32, tag uint16) (uint32, bool) {
	if uint64(ifd)+2 > uint64(len(tiff)) {
		return 0, false
	}
	n := uint32(order.Uint16(tiff[ifd : ifd+2]))
	if n > 1000 {
		return 0, false
	}
	for k := uint32(0); k < n; k++ {
		e := ifd + 2 + k*12
		if uint64(e)+12 > uint64(len(tiff)) {
			return 0, false
		}
		if order.Uint16(tiff[e:e+2]) == tag {
			return e, true
		}
	}
	return 0, false
}

func tagUint32(tiff []byte, order binary.ByteOrder, e uint32) (uint32, bool) {
	typ := order.Uint16(tiff[e+2 : e+4])
	if typ != 4 && typ != 13 { // LONG or IFD
		return 0, false
	}
	return order.Uint32(tiff[e+8 : e+12]), true
}

func tagASCII(tiff []byte, order binary.ByteOrder, e uint32) (string, bool) {
	if order.Uint16(tiff[e+2:e+4]) != 2 { // ASCII
		return "", false
	}
	count := order.Uint32(tiff[e+4 : e+8])
	if count == 0 || count > 64 {
		return "", false
	}
	var raw []byte
	if count <= 4 {
		raw = tiff[e+8 : e+8+count]
	} else {
		off := order.Uint32(tiff[e+8 : e+12])
		if uint64(off)+uint64(count) > uint64(len(tiff)) {
			return "", false
		}
		raw = tiff[off : off+count]
	}
	return strings.TrimRight(string(raw), "\x00 "), true
}
