package main

import (
	"encoding/json"
	"fmt"
	"log/slog"
	"net/http"
	"net/http/httputil"
	"net/url"
	"os"
	"path/filepath"
	"strconv"
	"sync"
	"time"
)

// india is Indian Standard Time. A fixed zone keeps the image free of tzdata,
// and India has no daylight saving time.
var india = time.FixedZone("IST", 5*3600+30*60)

// costly lists the perception endpoints that spend model credit. Everything
// else (health, readiness) passes through uncounted.
var costly = map[string]bool{
	"/v1/measure":        true,
	"/v1/measure/stream": true,
	"/v1/transcribe":     true,
}

// Budget is a global daily cap on perception calls. The day turns over at
// midnight India time.
type Budget struct {
	Limit int
	Now   func() time.Time
	// File, when set, holds today's count, written after every call, so a
	// restart of the launcher does not reset the cap. The hosting platform
	// copies it to durable storage (deploy/modal-web/app.py).
	File string

	mu   sync.Mutex
	day  string
	used int
}

// Take spends one call from today's budget. It reports whether the call may
// go ahead, how many calls today has used (including this one when allowed)
// and when the budget resets.
func (b *Budget) Take() (ok bool, used int, reset time.Time) {
	now := b.Now().In(india)
	today := now.Format("2006-01-02")
	y, m, d := now.Date()
	reset = time.Date(y, m, d+1, 0, 0, 0, 0, india)
	b.mu.Lock()
	defer b.mu.Unlock()
	if b.day != today {
		b.day, b.used = today, 0
	}
	if b.used >= b.Limit {
		return false, b.used, reset
	}
	b.used++
	b.save()
	return true, b.used, reset
}

type budgetState struct {
	Day  string `json:"day"`
	Used int    `json:"used"`
}

// Load reads the saved count. A missing or unreadable file, or one from an
// earlier day, leaves the budget empty.
func (b *Budget) Load() error {
	if b.File == "" {
		return nil
	}
	raw, err := os.ReadFile(b.File)
	if err != nil {
		if os.IsNotExist(err) {
			return nil
		}
		return err
	}
	var st budgetState
	if err := json.Unmarshal(raw, &st); err != nil {
		return err
	}
	b.mu.Lock()
	defer b.mu.Unlock()
	if st.Day == b.Now().In(india).Format("2006-01-02") && st.Used >= 0 {
		b.day, b.used = st.Day, st.Used
	}
	return nil
}

// save writes the count atomically; the caller holds b.mu.
func (b *Budget) save() {
	if b.File == "" {
		return
	}
	raw, _ := json.Marshal(budgetState{Day: b.day, Used: b.used})
	tmp := b.File + ".tmp"
	if err := os.MkdirAll(filepath.Dir(b.File), 0o755); err != nil {
		return
	}
	if err := os.WriteFile(tmp, raw, 0o644); err != nil {
		return
	}
	_ = os.Rename(tmp, b.File)
}

// Status returns today's use without spending anything.
func (b *Budget) Status() (day string, used int) {
	today := b.Now().In(india).Format("2006-01-02")
	b.mu.Lock()
	defer b.mu.Unlock()
	if b.day != today {
		return today, 0
	}
	return b.day, b.used
}

// pausedMessage is what a visitor reads on the report page once the cap is
// reached. The gateway shows perception's "detail" text in the result card.
func pausedMessage(limit int) string {
	return fmt.Sprintf("Live measurements are paused for today: this demo's daily limit of %d measurements has been used. "+
		"The live map and water-aware routes still work. Measurements resume at midnight India time.", limit)
}

// NewGuard returns a reverse proxy to perception that counts costly calls
// against the budget and refuses them with 429 once it is spent. It also
// answers GET /quota with today's use, for the operator.
func NewGuard(upstream *url.URL, budget *Budget, log *slog.Logger) http.Handler {
	proxy := httputil.NewSingleHostReverseProxy(upstream)
	// Flush every write at once, so perception's server-sent events (the live
	// agent trace) reach the gateway as they happen.
	proxy.FlushInterval = -1
	proxy.ErrorHandler = func(w http.ResponseWriter, r *http.Request, err error) {
		// Readiness probes fail quietly while SAM loads; only a lost report is news.
		if costly[r.URL.Path] {
			log.Warn("perception unreachable", "path", r.URL.Path, "error", err.Error())
		}
		writeDetail(w, http.StatusServiceUnavailable, "model unavailable: the measurement service is starting or restarting", 0)
	}
	mux := http.NewServeMux()
	mux.HandleFunc("GET /quota", func(w http.ResponseWriter, r *http.Request) {
		day, used := budget.Status()
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]any{"day": day, "used": used, "limit": budget.Limit})
	})
	mux.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
		if r.Method == http.MethodPost && costly[r.URL.Path] {
			ok, used, reset := budget.Take()
			if !ok {
				log.Warn("daily perception cap reached; call refused", "path", r.URL.Path, "limit", budget.Limit)
				writeDetail(w, http.StatusTooManyRequests, pausedMessage(budget.Limit), time.Until(reset))
				return
			}
			log.Info("perception call", "path", r.URL.Path, "used_today", used, "limit", budget.Limit)
			t0 := time.Now()
			// When the gateway gives up (its timeout, or the visitor leaving),
			// its request is cancelled, the proxy closes the connection to
			// perception, and perception cancels the measurement at its next
			// await. A SAM pass already running in a thread finishes first.
			// Deferred, because the proxy aborts a half-sent stream by
			// panicking with http.ErrAbortHandler.
			defer func() {
				if r.Context().Err() != nil {
					log.Warn("caller went away; perception request cancelled", "path", r.URL.Path,
						"after_ms", time.Since(t0).Milliseconds())
				} else {
					log.Info("perception call done", "path", r.URL.Path, "ms", time.Since(t0).Milliseconds())
				}
			}()
			proxy.ServeHTTP(w, r)
			return
		}
		proxy.ServeHTTP(w, r)
	})
	return mux
}

// writeDetail answers in FastAPI's error shape, which the gateway reads.
// The status is never 404, 405 or 501: those tell the gateway to fall back
// to the non-streaming endpoint, which would spend a second call.
func writeDetail(w http.ResponseWriter, status int, detail string, retry time.Duration) {
	if retry > 0 {
		w.Header().Set("Retry-After", strconv.Itoa(int(retry.Seconds())+1))
	}
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(map[string]string{"detail": detail})
}
