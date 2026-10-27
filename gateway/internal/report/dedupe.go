package report

import (
	"sync"
	"time"

	"neermattam/gateway/internal/media"
)

// Dedupe remembers recent uploads by exact bytes (SHA-256) and, for photos,
// by perceptual difference hash. A repost of the same photo, a re-encode or a
// screenshot of it must not be fused twice: the engine would count the same
// water as independent evidence and grow overconfident.
type Dedupe struct {
	window  time.Duration
	maxBits int
	maxKeep int
	now     func() time.Time

	mu     sync.Mutex
	exact  map[[32]byte]time.Time
	hashes []hashEntry
}

type hashEntry struct {
	hash uint64
	at   time.Time
}

// Match describes a duplicate.
type Match struct {
	Exact    bool
	Distance int // differing dHash bits for a near match
	Ago      time.Duration
}

// NewDedupe remembers uploads for window; near matches are dHashes within
// maxBits of each other.
func NewDedupe(window time.Duration, maxBits int) *Dedupe {
	return &Dedupe{window: window, maxBits: maxBits, maxKeep: 20_000, now: time.Now, exact: map[[32]byte]time.Time{}}
}

// Check looks for an earlier copy.
func (d *Dedupe) Check(sum [32]byte, dhash uint64, hasHash bool) (Match, bool) {
	now := d.now()
	d.mu.Lock()
	defer d.mu.Unlock()
	d.expireLocked(now)
	if at, ok := d.exact[sum]; ok {
		return Match{Exact: true, Ago: now.Sub(at)}, true
	}
	if !hasHash {
		return Match{}, false
	}
	best := Match{Distance: 65}
	for _, h := range d.hashes {
		if dist := media.Hamming(h.hash, dhash); dist < best.Distance {
			best = Match{Distance: dist, Ago: now.Sub(h.at)}
		}
	}
	if best.Distance <= d.maxBits {
		return best, true
	}
	return Match{}, false
}

// Remember records an upload.
func (d *Dedupe) Remember(sum [32]byte, dhash uint64, hasHash bool) {
	now := d.now()
	d.mu.Lock()
	defer d.mu.Unlock()
	d.exact[sum] = now
	if hasHash {
		d.hashes = append(d.hashes, hashEntry{dhash, now})
		if len(d.hashes) > d.maxKeep {
			d.hashes = d.hashes[len(d.hashes)-d.maxKeep:]
		}
	}
}

func (d *Dedupe) expireLocked(now time.Time) {
	for k, at := range d.exact {
		if now.Sub(at) > d.window {
			delete(d.exact, k)
		}
	}
	i := 0
	for i < len(d.hashes) && now.Sub(d.hashes[i].at) > d.window {
		i++
	}
	d.hashes = d.hashes[i:]
}
