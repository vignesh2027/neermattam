package live

import (
	"context"
	"encoding/json"
	"errors"
	"log/slog"
	"sync"
	"sync/atomic"
	"time"

	"neermattam/gateway/internal/metrics"
	"neermattam/gateway/internal/upstream"
)

// Message is one server-sent event for browsers.
type Message struct {
	Event string
	Data  []byte
}

// ErrFull means the hub already serves its maximum number of clients.
var ErrFull = errors.New("too many live map clients")

// ErrClosed means the hub is shutting down.
var ErrClosed = errors.New("live hub closed")

// Sub is one browser connection.
type Sub struct {
	ch   chan Message
	hub  *Hub
	once sync.Once
}

// C delivers messages. It is closed when the hub drops the client.
func (s *Sub) C() <-chan Message { return s.ch }

// Close unsubscribes.
func (s *Sub) Close() { s.hub.remove(s, "") }

// FeedItem is one entry in the live report ticker.
type FeedItem struct {
	Simulated    bool      `json:"simulated"`
	Source       string    `json:"source"`
	DepthCm      float64   `json:"depth_cm"`
	DepthSigmaCm float64   `json:"depth_sigma_cm"`
	Place        string    `json:"place"`
	Lat          float64   `json:"lat"`
	Lon          float64   `json:"lon"`
	At           time.Time `json:"at"`
}

// Hub fans out the engine's stream plus the gateway's own feed events.
type Hub struct {
	max     int
	buffer  int
	origins *Origins
	log     *slog.Logger
	metrics *metrics.Set

	mu      sync.Mutex
	clients map[*Sub]struct{}
	closed  bool
	feed    []FeedItem // simulated items, newest first
	real    []FeedItem // real reports, newest first, kept apart so the demo drip never pushes them out

	upstreamUp atomic.Bool
}

// NewHub returns a hub serving at most max clients.
func NewHub(max int, origins *Origins, log *slog.Logger, m *metrics.Set) *Hub {
	return &Hub{max: max, buffer: 256, origins: origins, log: log, metrics: m, clients: map[*Sub]struct{}{}}
}

// Subscribe adds a client or refuses when the hub is full.
func (h *Hub) Subscribe() (*Sub, error) {
	h.mu.Lock()
	defer h.mu.Unlock()
	if h.closed {
		return nil, ErrClosed
	}
	if len(h.clients) >= h.max {
		return nil, ErrFull
	}
	s := &Sub{ch: make(chan Message, h.buffer), hub: h}
	h.clients[s] = struct{}{}
	return s, nil
}

// Clients is the number of connected browsers.
func (h *Hub) Clients() int {
	h.mu.Lock()
	defer h.mu.Unlock()
	return len(h.clients)
}

// UpstreamConnected reports whether the engine stream is open.
func (h *Hub) UpstreamConnected() bool { return h.upstreamUp.Load() }

func (h *Hub) remove(s *Sub, reason string) {
	h.mu.Lock()
	_, ok := h.clients[s]
	delete(h.clients, s)
	h.mu.Unlock()
	if ok {
		s.once.Do(func() { close(s.ch) })
		if reason != "" && h.metrics != nil {
			h.metrics.SSEDropped.Inc(reason)
		}
	}
}

// Broadcast sends to every client without blocking. A client whose buffer
// is full has fallen behind; it is disconnected, and its browser reconnects
// and refetches the cells, which is cheaper than queueing without bound.
func (h *Hub) Broadcast(m Message) {
	h.mu.Lock()
	var slow []*Sub
	for s := range h.clients {
		select {
		case s.ch <- m:
		default:
			slow = append(slow, s)
		}
	}
	h.mu.Unlock()
	for _, s := range slow {
		h.remove(s, "slow")
	}
}

// Close disconnects every client and refuses new ones.
func (h *Hub) Close() {
	h.mu.Lock()
	h.closed = true
	subs := make([]*Sub, 0, len(h.clients))
	for s := range h.clients {
		subs = append(subs, s)
	}
	h.mu.Unlock()
	for _, s := range subs {
		h.remove(s, "shutdown")
	}
}

// PublishFeed records a report in the ticker and tells every browser.
func (h *Hub) PublishFeed(item FeedItem) {
	h.mu.Lock()
	if item.Simulated {
		h.feed = append([]FeedItem{item}, h.feed...)
		if len(h.feed) > 8 {
			h.feed = h.feed[:8]
		}
	} else {
		h.real = append([]FeedItem{item}, h.real...)
		if len(h.real) > 8 {
			h.real = h.real[:8]
		}
	}
	h.mu.Unlock()
	b, err := json.Marshal(item)
	if err != nil {
		return
	}
	h.Broadcast(Message{Event: "report", Data: b})
}

// Feed returns ticker items: real reports first (newest first), then
// simulated ones.
func (h *Hub) Feed() []FeedItem {
	h.mu.Lock()
	defer h.mu.Unlock()
	out := append([]FeedItem(nil), h.real...)
	return append(out, h.feed...)
}

// Run keeps one connection to the engine stream open until ctx ends,
// reconnecting with capped exponential backoff. After every reconnect, and
// whenever the engine says it dropped events, browsers are told to resync.
func (h *Hub) Run(ctx context.Context, e *upstream.Engine) {
	backoff := 500 * time.Millisecond
	for ctx.Err() == nil {
		connected := false
		err := e.Stream(ctx, func(ev upstream.Event) {
			if !connected {
				connected = true
				h.upstreamUp.Store(true)
				backoff = 500 * time.Millisecond
				h.log.Info("engine stream connected")
				h.Broadcast(Message{Event: "resync", Data: []byte(`{"reason":"connected"}`)})
			}
			h.handle(ev)
		})
		h.upstreamUp.Store(false)
		if ctx.Err() != nil {
			return
		}
		h.log.Warn("engine stream ended; reconnecting", "error", errString(err), "backoff_ms", backoff.Milliseconds())
		select {
		case <-ctx.Done():
			return
		case <-time.After(backoff):
		}
		backoff = min(backoff*2, 15*time.Second)
	}
}

func (h *Hub) handle(ev upstream.Event) {
	if ev.Comment != "" {
		// The engine writes a comment when this subscriber lagged; the
		// browsers' copies are now stale too.
		h.log.Warn("engine stream lagged", "comment", ev.Comment)
		h.Broadcast(Message{Event: "resync", Data: []byte(`{"reason":"lagged"}`)})
		return
	}
	if ev.Name != "cell" {
		return
	}
	data, err := DecorateFeature([]byte(ev.Data), h.origins)
	if err != nil {
		h.log.Warn("engine sent an unreadable cell event", "error", err.Error())
		return
	}
	h.Broadcast(Message{Event: "cell", Data: data})
}

// DecorateFeature adds properties.origin to one GeoJSON cell feature.
func DecorateFeature(raw []byte, o *Origins) ([]byte, error) {
	var f map[string]any
	if err := json.Unmarshal(raw, &f); err != nil {
		return nil, err
	}
	decorate(f, o)
	return json.Marshal(f)
}

// DecorateCollection adds properties.origin to every feature.
func DecorateCollection(raw []byte, o *Origins) ([]byte, error) {
	var fc map[string]any
	if err := json.Unmarshal(raw, &fc); err != nil {
		return nil, err
	}
	feats, _ := fc["features"].([]any)
	for _, x := range feats {
		if f, ok := x.(map[string]any); ok {
			decorate(f, o)
		}
	}
	fc["simulated_scenario"] = o.HasSimulated()
	return json.Marshal(fc)
}

func decorate(f map[string]any, o *Origins) {
	props, _ := f["properties"].(map[string]any)
	if props == nil {
		props = map[string]any{}
		f["properties"] = props
	}
	id, _ := f["id"].(string)
	if id == "" {
		id, _ = props["cell"].(string)
	}
	lat, lon := featureCentroid(f)
	props["origin"] = o.Label(id, lat, lon)
}

func featureCentroid(f map[string]any) (float64, float64) {
	g, _ := f["geometry"].(map[string]any)
	rings, _ := g["coordinates"].([]any)
	if len(rings) == 0 {
		return 0, 0
	}
	ring, _ := rings[0].([]any)
	var pts [][2]float64
	for _, p := range ring {
		xy, _ := p.([]any)
		if len(xy) < 2 {
			continue
		}
		x, _ := xy[0].(float64)
		y, _ := xy[1].(float64)
		pts = append(pts, [2]float64{x, y})
	}
	return upstream.Centroid([][][2]float64{pts})
}

func errString(err error) string {
	if err == nil {
		return ""
	}
	return err.Error()
}
