package live

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"log/slog"
	"math"
	"sync"
	"sync/atomic"
	"time"

	"neermattam/gateway/internal/metrics"
	"neermattam/gateway/internal/upstream"
)

// Message is one server-sent event for browsers. City is the city whose
// map it belongs to; empty goes to every client.
type Message struct {
	Event string
	Data  []byte
	City  string
}

// ErrFull means the hub already serves its maximum number of clients.
var ErrFull = errors.New("too many live map clients")

// ErrClosed means the hub is shutting down.
var ErrClosed = errors.New("live hub closed")

// Sub is one browser connection. A sub with a city hears only that city's
// cells and reports (and messages for every city).
type Sub struct {
	ch   chan Message
	hub  *Hub
	once sync.Once
	city string
}

// C delivers messages. It is closed when the hub drops the client.
func (s *Sub) C() <-chan Message { return s.ch }

// Close unsubscribes.
func (s *Sub) Close() { s.hub.remove(s, "") }

// FeedItem is one entry in the live report ticker.
type FeedItem struct {
	City         string    `json:"city"`
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
	listeners  []func(Message)

	// DropSimulated leaves out stream cells labelled simulated: set on the
	// live map's hub, which carries real reports only.
	DropSimulated bool
}

// NewHub returns a hub serving at most max clients.
func NewHub(max int, origins *Origins, log *slog.Logger, m *metrics.Set) *Hub {
	return &Hub{max: max, buffer: 256, origins: origins, log: log, metrics: m, clients: map[*Sub]struct{}{}}
}

// Listen registers a function that sees every message, for the gateway's
// own consumers (alerts, the control room). It must not block.
func (h *Hub) Listen(fn func(Message)) {
	h.mu.Lock()
	defer h.mu.Unlock()
	h.listeners = append(h.listeners, fn)
}

// Subscribe adds a client for every city or refuses when the hub is full.
func (h *Hub) Subscribe() (*Sub, error) { return h.SubscribeCity("") }

// SubscribeCity adds a client that hears one city ("" for all).
func (h *Hub) SubscribeCity(city string) (*Sub, error) {
	h.mu.Lock()
	defer h.mu.Unlock()
	if h.closed {
		return nil, ErrClosed
	}
	if len(h.clients) >= h.max {
		return nil, ErrFull
	}
	s := &Sub{ch: make(chan Message, h.buffer), hub: h, city: city}
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
	listeners := h.listeners
	for s := range h.clients {
		if s.city != "" && m.City != "" && s.city != m.City {
			continue
		}
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
	for _, fn := range listeners {
		fn(m)
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
	h.Broadcast(Message{Event: "report", Data: b, City: item.City})
}

// RestoreFeed adds a past item without broadcasting it.
func (h *Hub) RestoreFeed(item FeedItem) {
	h.mu.Lock()
	defer h.mu.Unlock()
	if item.Simulated {
		h.feed = append([]FeedItem{item}, h.feed...)
		return
	}
	h.real = append([]FeedItem{item}, h.real...)
	if len(h.real) > 8 {
		h.real = h.real[:8]
	}
}

// Feed returns ticker items: real reports first (newest first), then
// simulated ones; with a city, only that city's.
func (h *Hub) Feed(city string) []FeedItem {
	h.mu.Lock()
	defer h.mu.Unlock()
	var out []FeedItem
	for _, list := range [][]FeedItem{h.real, h.feed} {
		for _, it := range list {
			if city == "" || it.City == "" || it.City == city {
				out = append(out, it)
			}
		}
	}
	return out
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
	data, city, err := DecorateFeatureCity([]byte(ev.Data), h.origins)
	if err != nil {
		h.log.Warn("engine sent an unreadable cell event", "error", err.Error())
		return
	}
	if h.DropSimulated && bytes.Contains(data, []byte(`"origin":"simulated"`)) {
		return
	}
	h.Broadcast(Message{Event: "cell", Data: data, City: city})
}

// keptProps are the cell properties the map and its popup use. The rest
// (debris factor, upper hazard class, the duplicate top-level id) stay in
// the engine's own API.
var keptProps = []string{
	"cell", "city", "depth_cm", "depth_sigma_cm", "depth_upper_cm",
	"velocity_mps", "velocity_sigma_mps", "velocity_assumed",
	"hazard_rating", "hazard_class", "hazard_class_upper", "unknown", "expired", "n_readings", "n_neighbour", "updated_at",
}

// DecorateFeature compacts one GeoJSON cell feature and adds
// properties.origin.
func DecorateFeature(raw []byte, o *Origins) ([]byte, error) {
	b, _, err := DecorateFeatureCity(raw, o)
	return b, err
}

// DecorateFeatureCity is DecorateFeature that also returns the cell's city.
func DecorateFeatureCity(raw []byte, o *Origins) ([]byte, string, error) {
	var f map[string]any
	if err := json.Unmarshal(raw, &f); err != nil {
		return nil, "", err
	}
	c := compact(f, o)
	city, _ := c["properties"].(map[string]any)["city"].(string)
	b, err := json.Marshal(c)
	return b, city, err
}

// DecorateCollection compacts every feature, adds properties.origin, and
// says whether the map holds simulated water.
func DecorateCollection(raw []byte, o *Origins) ([]byte, error) {
	return DecorateCollectionFor(raw, o, "", o.HasSimulated())
}

// DecorateCollectionFor is DecorateCollection for one city's map, with the
// caller saying whether that city holds the simulated scenario.
func DecorateCollectionFor(raw []byte, o *Origins, city string, simulated bool) ([]byte, error) {
	var fc map[string]any
	if err := json.Unmarshal(raw, &fc); err != nil {
		return nil, err
	}
	feats, _ := fc["features"].([]any)
	out := make([]any, 0, len(feats))
	for _, x := range feats {
		if f, ok := x.(map[string]any); ok {
			out = append(out, compact(f, o))
		}
	}
	body := map[string]any{
		"type":               "FeatureCollection",
		"features":           out,
		"truncated":          fc["truncated"],
		"generated_at":       fc["generated_at"],
		"simulated_scenario": simulated,
	}
	if city != "" {
		body["city"] = city
	}
	return json.Marshal(body)
}

// DecorateLive is DecorateCollectionFor for the live map, which holds real
// reports only: a cell labelled simulated (left in the engine by an older
// deployment that mixed the scenario into the live map) is left out.
func DecorateLive(raw []byte, o *Origins, city string) ([]byte, error) {
	var fc map[string]any
	if err := json.Unmarshal(raw, &fc); err != nil {
		return nil, err
	}
	feats, _ := fc["features"].([]any)
	out := make([]any, 0, len(feats))
	for _, x := range feats {
		f, ok := x.(map[string]any)
		if !ok {
			continue
		}
		c := compact(f, o)
		if c["properties"].(map[string]any)["origin"] == OriginSimulated {
			continue
		}
		out = append(out, c)
	}
	body := map[string]any{
		"type":               "FeatureCollection",
		"features":           out,
		"truncated":          fc["truncated"],
		"generated_at":       fc["generated_at"],
		"simulated_scenario": false,
	}
	if city != "" {
		body["city"] = city
	}
	return json.Marshal(body)
}

// compact keeps the properties the map uses and rounds coordinates to five
// decimals (about a metre, far finer than a 66 m hexagon).
func compact(f map[string]any, o *Origins) map[string]any {
	props, _ := f["properties"].(map[string]any)
	if props == nil {
		props = map[string]any{}
	}
	id, _ := f["id"].(string)
	if id == "" {
		id, _ = props["cell"].(string)
	}
	lat, lon := featureCentroid(f)
	kept := make(map[string]any, len(keptProps)+1)
	for _, k := range keptProps {
		if v, ok := props[k]; ok {
			kept[k] = v
		}
	}
	kept["cell"] = id
	kept["origin"] = o.Label(id, lat, lon)
	geom, _ := f["geometry"].(map[string]any)
	if rings, ok := geom["coordinates"].([]any); ok {
		for _, ring := range rings {
			pts, _ := ring.([]any)
			for _, p := range pts {
				if xy, ok := p.([]any); ok {
					for i, c := range xy {
						if v, ok := c.(float64); ok {
							xy[i] = math.Round(v*1e5) / 1e5
						}
					}
				}
			}
		}
	}
	return map[string]any{"type": "Feature", "geometry": geom, "properties": kept}
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
