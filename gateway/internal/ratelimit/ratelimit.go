// Package ratelimit is a per-key token bucket. Each key (a client IP) owns a
// bucket of `burst` tokens that refills at `rate` tokens per second; a request
// spends one token or is refused with the time until the next token.
package ratelimit

import (
	"math"
	"net"
	"net/http"
	"strings"
	"sync"
	"time"
)

type bucket struct {
	tokens float64
	last   time.Time
}

// Limiter is safe for concurrent use.
type Limiter struct {
	rate    float64 // tokens per second
	burst   float64
	maxKeys int
	now     func() time.Time

	mu      sync.Mutex
	buckets map[string]*bucket
}

// New returns a limiter allowing perMinute requests per minute per key on
// average, with bursts of up to burst requests.
func New(perMinute, burst float64) *Limiter {
	if burst < 1 {
		burst = 1
	}
	return &Limiter{
		rate:    perMinute / 60,
		burst:   burst,
		maxKeys: 100_000,
		now:     time.Now,
		buckets: map[string]*bucket{},
	}
}

// WithClock replaces the clock, for tests.
func (l *Limiter) WithClock(now func() time.Time) *Limiter {
	l.now = now
	return l
}

// Allow spends a token for key. When none is left it returns false and how
// long the caller should wait.
func (l *Limiter) Allow(key string) (bool, time.Duration) {
	now := l.now()
	l.mu.Lock()
	defer l.mu.Unlock()
	b, ok := l.buckets[key]
	if !ok {
		if len(l.buckets) >= l.maxKeys {
			l.sweepLocked(now)
		}
		b = &bucket{tokens: l.burst, last: now}
		l.buckets[key] = b
	}
	if elapsed := now.Sub(b.last).Seconds(); elapsed > 0 {
		b.tokens = math.Min(l.burst, b.tokens+elapsed*l.rate)
		b.last = now
	}
	if b.tokens >= 1 {
		b.tokens--
		return true, 0
	}
	wait := time.Duration((1 - b.tokens) / l.rate * float64(time.Second))
	return false, wait
}

// Len returns the number of tracked keys.
func (l *Limiter) Len() int {
	l.mu.Lock()
	defer l.mu.Unlock()
	return len(l.buckets)
}

// Sweep forgets buckets that have refilled completely; they behave exactly
// like a new bucket, so dropping them loses nothing.
func (l *Limiter) Sweep() {
	now := l.now()
	l.mu.Lock()
	defer l.mu.Unlock()
	l.sweepLocked(now)
}

func (l *Limiter) sweepLocked(now time.Time) {
	full := time.Duration(l.burst / l.rate * float64(time.Second))
	for k, b := range l.buckets {
		if now.Sub(b.last) >= full {
			delete(l.buckets, k)
		}
	}
}

// ClientIP returns the address that identifies a client. X-Forwarded-For is
// honoured only when the gateway runs behind a trusted proxy, because anyone
// can send that header.
func ClientIP(r *http.Request, trustProxy bool) string {
	if trustProxy {
		if xff := r.Header.Get("X-Forwarded-For"); xff != "" {
			first := strings.TrimSpace(strings.Split(xff, ",")[0])
			if ip := net.ParseIP(first); ip != nil {
				return ip.String()
			}
		}
	}
	host, _, err := net.SplitHostPort(r.RemoteAddr)
	if err != nil {
		return r.RemoteAddr
	}
	return host
}
