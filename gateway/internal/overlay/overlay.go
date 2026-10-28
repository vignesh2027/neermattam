// Package overlay turns perception's overlay PNG (often 1 to 2 MB of
// base64) into a small JPEG served from a short-lived URL, so a report reply
// stays a few kilobytes on a phone.
package overlay

import (
	"bytes"
	"container/list"
	"encoding/base64"
	"errors"
	"image"
	"image/color"
	"image/draw"
	"image/jpeg"
	_ "image/png" // perception sends PNG
	"regexp"
	"sync"
	"time"
)

// Defaults for the re-encode.
const (
	MaxSide = 1280
	Quality = 82
)

// Reencode decodes a base64 PNG (or JPEG) and returns a JPEG at most maxSide
// pixels on its long side, flattened onto white.
func Reencode(b64 string, maxSide, quality int) ([]byte, error) {
	raw, err := base64.StdEncoding.DecodeString(b64)
	if err != nil {
		return nil, err
	}
	cfg, _, err := image.DecodeConfig(bytes.NewReader(raw))
	if err != nil {
		return nil, err
	}
	if cfg.Width*cfg.Height > 60_000_000 {
		return nil, errors.New("overlay is too large to decode")
	}
	src, _, err := image.Decode(bytes.NewReader(raw))
	if err != nil {
		return nil, err
	}
	b := src.Bounds()
	flat := image.NewRGBA(image.Rect(0, 0, b.Dx(), b.Dy()))
	draw.Draw(flat, flat.Bounds(), image.NewUniform(color.White), image.Point{}, draw.Src)
	draw.Draw(flat, flat.Bounds(), src, b.Min, draw.Over)
	out := Shrink(flat, maxSide)
	var buf bytes.Buffer
	if err := jpeg.Encode(&buf, out, &jpeg.Options{Quality: quality}); err != nil {
		return nil, err
	}
	return buf.Bytes(), nil
}

// Shrink scales an image down so its long side is at most maxSide, by area
// averaging (a box filter), which is sharp enough for an overlay and needs
// nothing beyond the standard library. Smaller images are returned as is.
func Shrink(src *image.RGBA, maxSide int) *image.RGBA {
	w, h := src.Bounds().Dx(), src.Bounds().Dy()
	long := max(w, h)
	if long <= maxSide {
		return src
	}
	nw := max(1, w*maxSide/long)
	nh := max(1, h*maxSide/long)
	dst := image.NewRGBA(image.Rect(0, 0, nw, nh))
	for y := 0; y < nh; y++ {
		y0, y1 := y*h/nh, max((y+1)*h/nh, y*h/nh+1)
		for x := 0; x < nw; x++ {
			x0, x1 := x*w/nw, max((x+1)*w/nw, x*w/nw+1)
			var r, g, bl, n uint32
			for yy := y0; yy < y1; yy++ {
				i := src.PixOffset(x0, yy)
				for xx := x0; xx < x1; xx++ {
					r += uint32(src.Pix[i])
					g += uint32(src.Pix[i+1])
					bl += uint32(src.Pix[i+2])
					i += 4
					n++
				}
			}
			j := dst.PixOffset(x, y)
			dst.Pix[j], dst.Pix[j+1], dst.Pix[j+2], dst.Pix[j+3] = uint8(r/n), uint8(g/n), uint8(bl/n), 255
		}
	}
	return dst
}

var validID = regexp.MustCompile(`^[A-Za-z0-9_-]{1,80}$`)

// ValidID reports whether an id is safe to use in a URL path.
func ValidID(id string) bool { return validID.MatchString(id) }

type entry struct {
	id   string
	data []byte
	at   time.Time
}

// Store is a bounded in-memory LRU of JPEGs that expire after a TTL.
type Store struct {
	ttl      time.Duration
	maxItems int
	maxBytes int
	now      func() time.Time

	mu    sync.Mutex
	ll    *list.List
	items map[string]*list.Element
	bytes int
}

// NewStore keeps at most maxItems overlays and maxBytes in total, each for ttl.
func NewStore(ttl time.Duration, maxItems, maxBytes int) *Store {
	return &Store{ttl: ttl, maxItems: maxItems, maxBytes: maxBytes, now: time.Now, ll: list.New(), items: map[string]*list.Element{}}
}

// WithClock replaces the clock, for tests.
func (s *Store) WithClock(now func() time.Time) *Store {
	s.now = now
	return s
}

// Put stores an overlay, evicting the least recently used ones over the bounds.
func (s *Store) Put(id string, data []byte) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if el, ok := s.items[id]; ok {
		s.remove(el)
	}
	el := s.ll.PushFront(&entry{id: id, data: data, at: s.now()})
	s.items[id] = el
	s.bytes += len(data)
	for (s.ll.Len() > s.maxItems || s.bytes > s.maxBytes) && s.ll.Len() > 1 {
		s.remove(s.ll.Back())
	}
}

// Get returns an overlay that has not expired.
func (s *Store) Get(id string) ([]byte, bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	el, ok := s.items[id]
	if !ok {
		return nil, false
	}
	e := el.Value.(*entry)
	if s.now().Sub(e.at) > s.ttl {
		s.remove(el)
		return nil, false
	}
	s.ll.MoveToFront(el)
	return e.data, true
}

// Len is the number of stored overlays.
func (s *Store) Len() int {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.ll.Len()
}

func (s *Store) remove(el *list.Element) {
	e := el.Value.(*entry)
	s.ll.Remove(el)
	delete(s.items, e.id)
	s.bytes -= len(e.data)
}
