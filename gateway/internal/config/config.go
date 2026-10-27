// Package config reads the gateway's settings from environment variables.
// Every setting has a default that works on a laptop next to the engine and
// perception services, so `go run ./cmd/neermattam-gateway` needs no setup.
package config

import (
	"fmt"
	"os"
	"strconv"
	"strings"
	"time"
)

// BBox is a service area as minLon, minLat, maxLon, maxLat.
type BBox struct {
	MinLon, MinLat, MaxLon, MaxLat float64
}

// Contains reports whether the point lies inside the box, edges included.
func (b BBox) Contains(lat, lon float64) bool {
	return lat >= b.MinLat && lat <= b.MaxLat && lon >= b.MinLon && lon <= b.MaxLon
}

// String formats the box the way the engine's bbox parameter expects.
func (b BBox) String() string {
	return fmt.Sprintf("%g,%g,%g,%g", b.MinLon, b.MinLat, b.MaxLon, b.MaxLat)
}

// ParseBBox parses "minLon,minLat,maxLon,maxLat".
func ParseBBox(s string) (BBox, error) {
	parts := strings.Split(s, ",")
	if len(parts) != 4 {
		return BBox{}, fmt.Errorf("bbox must be minLon,minLat,maxLon,maxLat")
	}
	var v [4]float64
	for i, p := range parts {
		f, err := strconv.ParseFloat(strings.TrimSpace(p), 64)
		if err != nil {
			return BBox{}, fmt.Errorf("bbox value %q is not a number", p)
		}
		v[i] = f
	}
	b := BBox{v[0], v[1], v[2], v[3]}
	if b.MinLon >= b.MaxLon || b.MinLat >= b.MaxLat {
		return BBox{}, fmt.Errorf("bbox minimums must be below maximums")
	}
	if b.MinLat < -90 || b.MaxLat > 90 || b.MinLon < -180 || b.MaxLon > 180 {
		return BBox{}, fmt.Errorf("bbox is outside the globe")
	}
	return b, nil
}

// WhatsApp holds the WhatsApp Cloud API settings. The webhook is enabled only
// when the verify token, app secret and access token are all present.
type WhatsApp struct {
	VerifyToken   string
	AppSecret     string
	AccessToken   string
	PhoneNumberID string
	GraphURL      string
}

// Enabled reports whether every secret the webhook needs is configured.
func (w WhatsApp) Enabled() bool {
	return w.VerifyToken != "" && w.AppSecret != "" && w.AccessToken != ""
}

// Missing lists the names (never the values) of absent WhatsApp variables.
func (w WhatsApp) Missing() []string {
	var m []string
	if w.VerifyToken == "" {
		m = append(m, "WHATSAPP_VERIFY_TOKEN")
	}
	if w.AppSecret == "" {
		m = append(m, "WHATSAPP_APP_SECRET")
	}
	if w.AccessToken == "" {
		m = append(m, "WHATSAPP_TOKEN")
	}
	return m
}

// Config is the full gateway configuration.
type Config struct {
	Addr      string
	PublicURL string
	LogLevel  string

	EngineURL     string
	PerceptionURL string
	OpenMeteoURL  string

	EngineTimeout     time.Duration
	RouteTimeout      time.Duration
	PerceptionTimeout time.Duration
	WeatherTimeout    time.Duration

	ServiceArea BBox

	MaxImageBytes   int64
	MaxVideoBytes   int64
	MaxVideoSeconds float64
	MaxAudioBytes   int64
	MaxPixels       int

	ReportsPerMinute float64
	ReportBurst      float64
	APIPerMinute     float64
	APIBurst         float64
	TrustProxy       bool
	MaxInflight      int

	MaxSSEClients int
	CellsCacheTTL time.Duration

	Demo             bool
	DemoDrip         time.Duration
	DemoReseedEvery  time.Duration
	DuplicateWindow  time.Duration
	NearDuplicateMax int

	WhatsApp WhatsApp
}

// FromEnv reads the configuration, returning an error that names the first
// malformed variable.
func FromEnv() (Config, error) {
	return fromLookup(os.LookupEnv)
}

func fromLookup(lookup func(string) (string, bool)) (Config, error) {
	r := reader{lookup: lookup}
	c := Config{
		Addr:      r.str("GATEWAY_ADDR", ":8080"),
		PublicURL: strings.TrimRight(r.str("PUBLIC_URL", "http://localhost:8080"), "/"),
		LogLevel:  r.str("LOG_LEVEL", "info"),

		EngineURL:     strings.TrimRight(r.str("ENGINE_URL", "http://127.0.0.1:8302"), "/"),
		PerceptionURL: strings.TrimRight(r.str("PERCEPTION_URL", "http://127.0.0.1:8301"), "/"),
		OpenMeteoURL:  strings.TrimRight(r.str("OPEN_METEO_URL", "https://api.open-meteo.com"), "/"),

		EngineTimeout:     r.dur("ENGINE_TIMEOUT", 5*time.Second),
		RouteTimeout:      r.dur("ROUTE_TIMEOUT", 20*time.Second),
		PerceptionTimeout: r.dur("PERCEPTION_TIMEOUT", 180*time.Second),
		WeatherTimeout:    r.dur("WEATHER_TIMEOUT", 4*time.Second),

		MaxImageBytes:   r.int64("MAX_IMAGE_BYTES", 12<<20),
		MaxVideoBytes:   r.int64("MAX_VIDEO_BYTES", 40<<20),
		MaxVideoSeconds: r.float("MAX_VIDEO_SECONDS", 30),
		MaxAudioBytes:   r.int64("MAX_AUDIO_BYTES", 8<<20),
		MaxPixels:       int(r.int64("MAX_IMAGE_PIXELS", 50_000_000)),

		ReportsPerMinute: r.float("REPORT_RATE_PER_MIN", 6),
		ReportBurst:      r.float("REPORT_BURST", 4),
		APIPerMinute:     r.float("API_RATE_PER_MIN", 120),
		APIBurst:         r.float("API_BURST", 40),
		TrustProxy:       r.bool("TRUST_PROXY", false),
		MaxInflight:      int(r.int64("MAX_INFLIGHT_REPORTS", 8)),

		MaxSSEClients: int(r.int64("MAX_SSE_CLIENTS", 200)),
		CellsCacheTTL: r.dur("CELLS_CACHE_TTL", 2*time.Second),

		Demo:             r.bool("DEMO", false),
		DemoDrip:         r.dur("DEMO_DRIP_INTERVAL", 6*time.Second),
		DemoReseedEvery:  r.dur("DEMO_RESEED_INTERVAL", 30*time.Minute),
		DuplicateWindow:  r.dur("DUPLICATE_WINDOW", 24*time.Hour),
		NearDuplicateMax: int(r.int64("NEAR_DUPLICATE_BITS", 5)),

		WhatsApp: WhatsApp{
			VerifyToken:   r.str("WHATSAPP_VERIFY_TOKEN", ""),
			AppSecret:     r.str("WHATSAPP_APP_SECRET", ""),
			AccessToken:   r.str("WHATSAPP_TOKEN", ""),
			PhoneNumberID: r.str("WHATSAPP_PHONE_NUMBER_ID", ""),
			GraphURL:      strings.TrimRight(r.str("WHATSAPP_GRAPH_URL", "https://graph.facebook.com/v23.0"), "/"),
		},
	}
	area := r.str("GATEWAY_BBOX", "80.10,12.85,80.33,13.24")
	if r.err == nil {
		b, err := ParseBBox(area)
		if err != nil {
			r.err = fmt.Errorf("GATEWAY_BBOX: %w", err)
		}
		c.ServiceArea = b
	}
	if r.err == nil && c.MaxSSEClients < 1 {
		r.err = fmt.Errorf("MAX_SSE_CLIENTS must be at least 1")
	}
	if r.err == nil && c.MaxInflight < 1 {
		r.err = fmt.Errorf("MAX_INFLIGHT_REPORTS must be at least 1")
	}
	return c, r.err
}

type reader struct {
	lookup func(string) (string, bool)
	err    error
}

func (r *reader) raw(key string) (string, bool) {
	v, ok := r.lookup(key)
	v = strings.TrimSpace(v)
	return v, ok && v != ""
}

func (r *reader) str(key, def string) string {
	if v, ok := r.raw(key); ok {
		return v
	}
	return def
}

func (r *reader) fail(key, v, want string) {
	if r.err == nil {
		r.err = fmt.Errorf("%s=%q is not %s", key, v, want)
	}
}

func (r *reader) bool(key string, def bool) bool {
	v, ok := r.raw(key)
	if !ok {
		return def
	}
	b, err := strconv.ParseBool(v)
	if err != nil {
		r.fail(key, v, "a boolean (1, 0, true, false)")
		return def
	}
	return b
}

func (r *reader) int64(key string, def int64) int64 {
	v, ok := r.raw(key)
	if !ok {
		return def
	}
	n, err := strconv.ParseInt(v, 10, 64)
	if err != nil || n < 0 {
		r.fail(key, v, "a non-negative integer")
		return def
	}
	return n
}

func (r *reader) float(key string, def float64) float64 {
	v, ok := r.raw(key)
	if !ok {
		return def
	}
	f, err := strconv.ParseFloat(v, 64)
	if err != nil || f <= 0 {
		r.fail(key, v, "a positive number")
		return def
	}
	return f
}

func (r *reader) dur(key string, def time.Duration) time.Duration {
	v, ok := r.raw(key)
	if !ok {
		return def
	}
	d, err := time.ParseDuration(v)
	if err != nil || d <= 0 {
		r.fail(key, v, "a positive duration such as 5s or 2m")
		return def
	}
	return d
}

// FromMap reads the configuration from a map, for tests and tools.
func FromMap(m map[string]string) (Config, error) {
	return fromLookup(func(k string) (string, bool) {
		v, ok := m[k]
		return v, ok
	})
}
