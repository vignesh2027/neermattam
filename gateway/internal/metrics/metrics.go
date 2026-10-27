// Package metrics is a small Prometheus text-format exporter. The gateway
// needs counters, gauges and histograms with a few labels; the official
// client library would bring a dozen transitive modules for that, so this
// package writes exposition format 0.0.4 directly.
package metrics

import (
	"fmt"
	"io"
	"math"
	"net/http"
	"sort"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
)

// DefaultBuckets suit request latencies from a millisecond to a minute.
var DefaultBuckets = []float64{0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60}

type collector interface {
	write(w io.Writer)
	name() string
}

// Registry holds every metric and serves them.
type Registry struct {
	mu      sync.Mutex
	metrics []collector
}

// NewRegistry returns an empty registry.
func NewRegistry() *Registry { return &Registry{} }

func (r *Registry) add(c collector) {
	r.mu.Lock()
	defer r.mu.Unlock()
	for _, m := range r.metrics {
		if m.name() == c.name() {
			panic("metrics: duplicate metric " + c.name())
		}
	}
	r.metrics = append(r.metrics, c)
}

// Write writes every metric in exposition format, sorted by name.
func (r *Registry) Write(w io.Writer) {
	r.mu.Lock()
	ms := append([]collector(nil), r.metrics...)
	r.mu.Unlock()
	sort.Slice(ms, func(i, j int) bool { return ms[i].name() < ms[j].name() })
	for _, m := range ms {
		m.write(w)
	}
}

// Handler serves the registry at /metrics.
func (r *Registry) Handler() http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
		r.Write(w)
	})
}

func header(w io.Writer, name, help, typ string) {
	fmt.Fprintf(w, "# HELP %s %s\n# TYPE %s %s\n", name, escapeHelp(help), name, typ)
}

func escapeHelp(s string) string {
	return strings.NewReplacer(`\`, `\\`, "\n", `\n`).Replace(s)
}

func escapeLabel(s string) string {
	return strings.NewReplacer(`\`, `\\`, "\n", `\n`, `"`, `\"`).Replace(s)
}

func labelString(names, values []string, extra ...string) string {
	if len(names) == 0 && len(extra) == 0 {
		return ""
	}
	var b strings.Builder
	b.WriteByte('{')
	first := true
	for i, n := range names {
		if !first {
			b.WriteByte(',')
		}
		first = false
		fmt.Fprintf(&b, `%s="%s"`, n, escapeLabel(values[i]))
	}
	for i := 0; i+1 < len(extra); i += 2 {
		if !first {
			b.WriteByte(',')
		}
		first = false
		fmt.Fprintf(&b, `%s="%s"`, extra[i], escapeLabel(extra[i+1]))
	}
	b.WriteByte('}')
	return b.String()
}

func formatFloat(f float64) string {
	switch {
	case math.IsInf(f, 1):
		return "+Inf"
	case math.IsInf(f, -1):
		return "-Inf"
	case math.IsNaN(f):
		return "NaN"
	}
	return strconv.FormatFloat(f, 'g', -1, 64)
}

const sep = "\xff"

// CounterVec is a family of monotonically increasing counters.
type CounterVec struct {
	n, help string
	labels  []string
	mu      sync.Mutex
	values  map[string]*atomic.Uint64
}

// NewCounterVec registers a counter family.
func (r *Registry) NewCounterVec(name, help string, labels ...string) *CounterVec {
	c := &CounterVec{n: name, help: help, labels: labels, values: map[string]*atomic.Uint64{}}
	r.add(c)
	return c
}

func (c *CounterVec) name() string { return c.n }

// Inc adds one to the counter with these label values.
func (c *CounterVec) Inc(values ...string) { c.Add(1, values...) }

// Add adds n to the counter with these label values.
func (c *CounterVec) Add(n uint64, values ...string) {
	if len(values) != len(c.labels) {
		panic(fmt.Sprintf("metrics: %s wants %d labels, got %d", c.n, len(c.labels), len(values)))
	}
	key := strings.Join(values, sep)
	c.mu.Lock()
	v, ok := c.values[key]
	if !ok {
		v = &atomic.Uint64{}
		c.values[key] = v
	}
	c.mu.Unlock()
	v.Add(n)
}

// Value returns the current count, for tests.
func (c *CounterVec) Value(values ...string) uint64 {
	c.mu.Lock()
	defer c.mu.Unlock()
	if v, ok := c.values[strings.Join(values, sep)]; ok {
		return v.Load()
	}
	return 0
}

func (c *CounterVec) write(w io.Writer) {
	header(w, c.n, c.help, "counter")
	c.mu.Lock()
	keys := make([]string, 0, len(c.values))
	for k := range c.values {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	for _, k := range keys {
		vals := splitKey(k, len(c.labels))
		fmt.Fprintf(w, "%s%s %d\n", c.n, labelString(c.labels, vals), c.values[k].Load())
	}
	c.mu.Unlock()
}

func splitKey(k string, n int) []string {
	if n == 0 {
		return nil
	}
	return strings.SplitN(k, sep, n)
}

// GaugeFunc reports a value computed at scrape time.
type GaugeFunc struct {
	n, help string
	fn      func() float64
}

// NewGaugeFunc registers a gauge whose value comes from fn.
func (r *Registry) NewGaugeFunc(name, help string, fn func() float64) {
	r.add(&GaugeFunc{n: name, help: help, fn: fn})
}

func (g *GaugeFunc) name() string { return g.n }

func (g *GaugeFunc) write(w io.Writer) {
	header(w, g.n, g.help, "gauge")
	fmt.Fprintf(w, "%s %s\n", g.n, formatFloat(g.fn()))
}

// HistogramVec is a family of histograms with fixed buckets.
type HistogramVec struct {
	n, help string
	labels  []string
	buckets []float64
	mu      sync.Mutex
	series  map[string]*histSeries
}

type histSeries struct {
	counts []uint64 // per bucket, not cumulative
	sum    float64
	count  uint64
}

// NewHistogramVec registers a histogram family. Buckets must be ascending.
func (r *Registry) NewHistogramVec(name, help string, buckets []float64, labels ...string) *HistogramVec {
	if !sort.Float64sAreSorted(buckets) {
		panic("metrics: buckets must be sorted")
	}
	h := &HistogramVec{n: name, help: help, labels: labels, buckets: buckets, series: map[string]*histSeries{}}
	r.add(h)
	return h
}

func (h *HistogramVec) name() string { return h.n }

// Observe records one value.
func (h *HistogramVec) Observe(v float64, values ...string) {
	if len(values) != len(h.labels) {
		panic(fmt.Sprintf("metrics: %s wants %d labels, got %d", h.n, len(h.labels), len(values)))
	}
	key := strings.Join(values, sep)
	h.mu.Lock()
	defer h.mu.Unlock()
	s, ok := h.series[key]
	if !ok {
		s = &histSeries{counts: make([]uint64, len(h.buckets))}
		h.series[key] = s
	}
	for i, b := range h.buckets {
		if v <= b {
			s.counts[i]++
			break
		}
	}
	s.sum += v
	s.count++
}

func (h *HistogramVec) write(w io.Writer) {
	header(w, h.n, h.help, "histogram")
	h.mu.Lock()
	defer h.mu.Unlock()
	keys := make([]string, 0, len(h.series))
	for k := range h.series {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	for _, k := range keys {
		s := h.series[k]
		vals := splitKey(k, len(h.labels))
		var cum uint64
		for i, b := range h.buckets {
			cum += s.counts[i]
			fmt.Fprintf(w, "%s_bucket%s %d\n", h.n, labelString(h.labels, vals, "le", formatFloat(b)), cum)
		}
		fmt.Fprintf(w, "%s_bucket%s %d\n", h.n, labelString(h.labels, vals, "le", "+Inf"), s.count)
		fmt.Fprintf(w, "%s_sum%s %s\n", h.n, labelString(h.labels, vals), formatFloat(s.sum))
		fmt.Fprintf(w, "%s_count%s %d\n", h.n, labelString(h.labels, vals), s.count)
	}
}

// Set is every metric the gateway exports.
type Set struct {
	Registry *Registry

	HTTPRequests     *CounterVec
	HTTPDuration     *HistogramVec
	Upstream         *CounterVec
	UpstreamDuration *HistogramVec
	Reports          *CounterVec
	Rules            *CounterVec
	Trust            *HistogramVec
	RateLimited      *CounterVec
	CellsCache       *CounterVec
	WhatsApp         *CounterVec
	SSEDropped       *CounterVec
}

// NewSet registers the gateway's metrics on a fresh registry.
func NewSet() *Set {
	r := NewRegistry()
	return &Set{
		Registry:         r,
		HTTPRequests:     r.NewCounterVec("neermattam_http_requests_total", "HTTP requests served, by route pattern, method and status code.", "route", "method", "code"),
		HTTPDuration:     r.NewHistogramVec("neermattam_http_request_duration_seconds", "Time to serve a request, by route pattern. Streams are excluded.", DefaultBuckets, "route"),
		Upstream:         r.NewCounterVec("neermattam_upstream_requests_total", "Calls to the engine, perception, Open-Meteo and the Graph API, by outcome.", "upstream", "outcome"),
		UpstreamDuration: r.NewHistogramVec("neermattam_upstream_duration_seconds", "Upstream call latency.", DefaultBuckets, "upstream"),
		Reports:          r.NewCounterVec("neermattam_reports_total", "Reports processed, by channel and final status.", "channel", "status"),
		Rules:            r.NewCounterVec("neermattam_verification_rules_total", "Verification rule outcomes.", "rule", "outcome"),
		Trust:            r.NewHistogramVec("neermattam_report_trust", "Trust assigned to fused reports.", []float64{0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1}),
		RateLimited:      r.NewCounterVec("neermattam_rate_limited_total", "Requests refused by the per-IP rate limiter.", "scope"),
		CellsCache:       r.NewCounterVec("neermattam_cells_cache_total", "Cell proxy cache lookups.", "result"),
		WhatsApp:         r.NewCounterVec("neermattam_whatsapp_messages_total", "WhatsApp messages received, by type.", "type"),
		SSEDropped:       r.NewCounterVec("neermattam_sse_clients_dropped_total", "Live map clients disconnected, by reason.", "reason"),
	}
}
