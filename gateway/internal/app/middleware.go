package app

import (
	"context"
	"encoding/json"
	"net/http"
	"runtime/debug"
	"strconv"
	"strings"
	"time"

	"neermattam/gateway/internal/ratelimit"
	"neermattam/gateway/internal/web"
)

type ctxKey int

const nonceKey ctxKey = 1

func nonceFrom(ctx context.Context) string {
	n, _ := ctx.Value(nonceKey).(string)
	return n
}

// statusWriter records the status and size. Unwrap lets
// http.ResponseController reach Flush and SetWriteDeadline underneath.
type statusWriter struct {
	http.ResponseWriter
	status int
	bytes  int
}

func (w *statusWriter) WriteHeader(code int) {
	if w.status == 0 {
		w.status = code
	}
	w.ResponseWriter.WriteHeader(code)
}

func (w *statusWriter) Write(b []byte) (int, error) {
	if w.status == 0 {
		w.status = http.StatusOK
	}
	n, err := w.ResponseWriter.Write(b)
	w.bytes += n
	return n, err
}

func (w *statusWriter) Unwrap() http.ResponseWriter { return w.ResponseWriter }

// Flush keeps http.Flusher working for code that type-asserts.
func (w *statusWriter) Flush() {
	if f, ok := w.ResponseWriter.(http.Flusher); ok {
		f.Flush()
	}
}

func isStream(r *http.Request) bool { return r.URL.Path == "/api/stream" }

// observe logs every request (without client addresses) and records metrics.
// Non-streaming responses get a write deadline; the server-wide
// WriteTimeout stays off so live streams can run for hours.
func (a *App) observe(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		start := time.Now()
		sw := &statusWriter{ResponseWriter: w}
		if !isStream(r) {
			_ = http.NewResponseController(sw).SetWriteDeadline(start.Add(3 * time.Minute))
		}
		next.ServeHTTP(sw, r)
		route := r.Pattern
		if route == "" {
			route = "unmatched"
		}
		code := sw.status
		if code == 0 {
			code = http.StatusOK
		}
		a.Metrics.HTTPRequests.Inc(route, r.Method, strconv.Itoa(code))
		if !isStream(r) {
			a.Metrics.HTTPDuration.Observe(time.Since(start).Seconds(), route)
		}
		if strings.HasPrefix(r.URL.Path, "/static/") || r.URL.Path == "/metrics" || r.URL.Path == "/healthz" {
			return
		}
		a.Log.Info("request",
			"method", r.Method, "route", route, "status", code,
			"duration_ms", time.Since(start).Milliseconds(), "bytes", sw.bytes)
	})
}

// secure sets security headers and a per-request CSP nonce.
func (a *App) secure(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		nonce := web.NewNonce()
		h := w.Header()
		h.Set("Content-Security-Policy", web.CSP(nonce))
		h.Set("X-Content-Type-Options", "nosniff")
		h.Set("Referrer-Policy", "strict-origin-when-cross-origin")
		h.Set("Permissions-Policy", "geolocation=(self), camera=(self), microphone=(self)")
		h.Set("Cross-Origin-Opener-Policy", "same-origin")
		next.ServeHTTP(w, r.WithContext(context.WithValue(r.Context(), nonceKey, nonce)))
	})
}

func (a *App) recoverer(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		defer func() {
			if v := recover(); v != nil {
				if v == http.ErrAbortHandler {
					panic(v)
				}
				a.Log.Error("panic", "path", r.URL.Path, "panic", v, "stack", string(debug.Stack()))
				writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "internal error"})
			}
		}()
		next.ServeHTTP(w, r)
	})
}

// limit applies a per-IP token bucket and answers 429 with Retry-After.
func (a *App) limit(w http.ResponseWriter, r *http.Request, l *ratelimit.Limiter, scope string) bool {
	ok, wait := l.Allow(ratelimit.ClientIP(r, a.Cfg.TrustProxy))
	if ok {
		return true
	}
	secs := int(wait.Seconds() + 0.999)
	if secs < 1 {
		secs = 1
	}
	a.Metrics.RateLimited.Inc(scope)
	w.Header().Set("Retry-After", strconv.Itoa(secs))
	writeJSON(w, http.StatusTooManyRequests, map[string]any{
		"error":               "Too many requests. Please wait " + strconv.Itoa(secs) + " seconds and try again.",
		"code":                "rate_limited",
		"retry_after_seconds": secs,
	})
	return false
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.Header().Set("Cache-Control", "no-store")
	w.WriteHeader(status)
	enc := json.NewEncoder(w)
	enc.SetEscapeHTML(true)
	_ = enc.Encode(v)
}

func wantsHTML(r *http.Request) bool {
	return strings.Contains(r.Header.Get("Accept"), "text/html")
}
