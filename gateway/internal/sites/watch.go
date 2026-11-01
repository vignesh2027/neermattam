// Package sites watches critical sites for their operators: a Web Push
// message when water in a site's watch zone crosses a threshold, when water
// over it appears within the outer ring, when an access route for the
// tanker, the two-wheeler or the car closes or opens again, and when the
// zone is back under the threshold. It reuses the alerts package's Web Push
// sender (VAPID and RFC 8291 encryption, standard library only) and keeps
// its own small store, site-alerts.json, in the data directory.
//
// Every message says it is advisory, and none calls a site safe: "back
// under" means the cells with recent reports are below the threshold.
package sites

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"math"
	"net/url"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"time"

	"neermattam/gateway/internal/alerts"
	"neermattam/gateway/internal/upstream"
)

// FileName is the store's file in the data directory.
const FileName = "site-alerts.json"

// Zone states, from the engine's site status.
const (
	ZoneOver    = "over"
	ZoneNearby  = "nearby"
	ZoneUnder   = "under"
	ZoneUnknown = "unknown"
)

// Notice kinds.
const (
	KindWatching    = "watching"
	KindOver        = "over"
	KindNearby      = "nearby"
	KindUnder       = "under"
	KindStale       = "stale"
	KindRouteClosed = "route_closed"
	KindRouteOpen   = "route_open"
)

// State is what a watch last saw at its site.
type State struct {
	Zone    string   `json:"zone"`               // over, nearby, under or unknown
	DepthCm float64  `json:"depth_cm,omitempty"` // deepest blocking depth in the zones
	Open    []string `json:"open,omitempty"`     // routes open, "main_road:truck"
	Closed  []string `json:"closed,omitempty"`   // routes with no safe way through
}

// Sub is one watch on one site.
type Sub struct {
	ID          string    `json:"id"`
	TokenHash   string    `json:"token_hash"`
	Endpoint    string    `json:"endpoint"`
	P256dh      string    `json:"p256dh"`
	Auth        string    `json:"auth"`
	Site        string    `json:"site"`
	SiteName    string    `json:"site_name"`
	City        string    `json:"city"`
	Lang        string    `json:"lang"`
	ThresholdCm float64   `json:"threshold_cm"`
	CreatedAt   time.Time `json:"created_at"`
	ExpiresAt   time.Time `json:"expires_at"`
	Last        State     `json:"last"`
	SentAt      time.Time `json:"sent_at,omitempty"`
	Sent        int       `json:"sent"`
	Failures    int       `json:"failures"`
}

// Errors.
var (
	ErrFull        = errors.New("the gateway holds its maximum number of site watches")
	ErrTooMany     = errors.New("this browser already watches the maximum number of sites")
	ErrNotFound    = errors.New("no such site watch")
	errBadSnapshot = errors.New("not a site alerts file")
)

// Store keeps site watches in memory and in one JSON file.
type Store struct {
	MaxTotal, MaxPer int
	TTL              time.Duration

	mu    sync.Mutex
	subs  map[string]*Sub
	dirty bool
	now   func() time.Time
}

// NewStore returns an empty store.
func NewStore(maxTotal, maxPer int, ttl time.Duration) *Store {
	return &Store{MaxTotal: maxTotal, MaxPer: maxPer, TTL: ttl, subs: map[string]*Sub{}, now: time.Now}
}

func randomHex(n int) string {
	b := make([]byte, n)
	_, _ = rand.Read(b)
	return hex.EncodeToString(b)
}

func hashToken(t string) string {
	h := sha256.Sum256([]byte(t))
	return hex.EncodeToString(h[:])
}

// Add stores a watch and returns its token.
func (st *Store) Add(s *Sub) (string, error) {
	st.mu.Lock()
	defer st.mu.Unlock()
	st.pruneLocked()
	if len(st.subs) >= st.MaxTotal {
		return "", ErrFull
	}
	n := 0
	for _, x := range st.subs {
		if x.Endpoint == s.Endpoint {
			n++
		}
	}
	if n >= st.MaxPer {
		return "", ErrTooMany
	}
	token := randomHex(16)
	s.ID = "sw_" + randomHex(8)
	s.TokenHash = hashToken(token)
	s.CreatedAt = st.now()
	s.ExpiresAt = s.CreatedAt.Add(st.TTL)
	cp := *s
	st.subs[s.ID] = &cp
	st.dirty = true
	return token, nil
}

// Get returns a watch when the token matches.
func (st *Store) Get(id, token string) (Sub, error) {
	st.mu.Lock()
	defer st.mu.Unlock()
	s, ok := st.subs[id]
	if !ok || subtle.ConstantTimeCompare([]byte(s.TokenHash), []byte(hashToken(token))) != 1 {
		return Sub{}, ErrNotFound
	}
	return *s, nil
}

// Delete removes a watch when the token matches.
func (st *Store) Delete(id, token string) error {
	if _, err := st.Get(id, token); err != nil {
		return err
	}
	st.Remove(id)
	return nil
}

// Remove drops a watch.
func (st *Store) Remove(id string) {
	st.mu.Lock()
	defer st.mu.Unlock()
	if _, ok := st.subs[id]; ok {
		delete(st.subs, id)
		st.dirty = true
	}
}

// Len is the number of watches.
func (st *Store) Len() int {
	st.mu.Lock()
	defer st.mu.Unlock()
	return len(st.subs)
}

func (st *Store) pruneLocked() {
	now := st.now()
	for id, s := range st.subs {
		if !now.Before(s.ExpiresAt) {
			delete(st.subs, id)
			st.dirty = true
		}
	}
}

// key groups watches that need the same engine answer.
type key struct {
	site      string
	threshold float64
}

// groups returns the distinct sites and thresholds watched.
func (st *Store) groups() []key {
	st.mu.Lock()
	defer st.mu.Unlock()
	st.pruneLocked()
	seen := map[key]bool{}
	var out []key
	for _, s := range st.subs {
		k := key{s.Site, s.ThresholdCm}
		if !seen[k] {
			seen[k] = true
			out = append(out, k)
		}
	}
	sort.Slice(out, func(i, j int) bool {
		if out[i].site != out[j].site {
			return out[i].site < out[j].site
		}
		return out[i].threshold < out[j].threshold
	})
	return out
}

func (st *Store) each(k key, fn func(*Sub)) {
	st.mu.Lock()
	defer st.mu.Unlock()
	for _, s := range st.subs {
		if s.Site == k.site && s.ThresholdCm == k.threshold {
			fn(s)
		}
	}
	st.dirty = true
}

type file struct {
	Format string `json:"format"`
	Subs   []*Sub `json:"watches"`
}

// Load reads the store's file; a missing file is an empty store.
func (st *Store) Load(path string) (int, error) {
	b, err := os.ReadFile(path)
	if errors.Is(err, os.ErrNotExist) {
		return 0, nil
	}
	if err != nil {
		return 0, err
	}
	var f file
	if err := json.Unmarshal(b, &f); err != nil {
		return 0, err
	}
	if f.Format != "neermattam-site-alerts-1" {
		return 0, errBadSnapshot
	}
	st.mu.Lock()
	defer st.mu.Unlock()
	for _, s := range f.Subs {
		if s != nil && s.ID != "" {
			st.subs[s.ID] = s
		}
	}
	st.pruneLocked()
	return len(st.subs), nil
}

// Save writes the store when it changed, through a temporary file and a
// rename, mode 0600 (it holds push endpoints).
func (st *Store) Save(path string) error {
	st.mu.Lock()
	if !st.dirty {
		st.mu.Unlock()
		return nil
	}
	f := file{Format: "neermattam-site-alerts-1"}
	for _, s := range st.subs {
		cp := *s
		f.Subs = append(f.Subs, &cp)
	}
	st.dirty = false
	st.mu.Unlock()
	sort.Slice(f.Subs, func(i, j int) bool { return f.Subs[i].ID < f.Subs[j].ID })
	b, err := json.Marshal(f)
	if err != nil {
		return err
	}
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return err
	}
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, b, 0o600); err != nil {
		return err
	}
	if err := os.Rename(tmp, path); err != nil {
		st.mu.Lock()
		st.dirty = true
		st.mu.Unlock()
		return err
	}
	return nil
}

// Evaluate turns the engine's answer for a site into a State.
func Evaluate(d *upstream.SiteDetail) State {
	st := State{Zone: d.Status}
	switch st.Zone {
	case ZoneOver, ZoneNearby, ZoneUnder:
	default:
		st.Zone = ZoneUnknown
	}
	for _, z := range d.Zones {
		if z.Deepest != nil {
			st.DepthCm = math.Max(st.DepthCm, math.Round(z.Deepest.DepthBlockCm))
		}
	}
	if d.Access != nil {
		for _, r := range d.Access.Routes {
			id := r.Target + ":" + r.Vehicle
			switch r.Status {
			case "open":
				st.Open = append(st.Open, id)
			case "no_safe_route":
				st.Closed = append(st.Closed, id)
			}
		}
	}
	sort.Strings(st.Open)
	sort.Strings(st.Closed)
	return st
}

func has(list []string, s string) bool {
	for _, x := range list {
		if x == s {
			return true
		}
	}
	return false
}

// Change is what a new state calls for: the zone notice, if any, and the
// routes that closed or opened again since the last state. A route that
// was unavailable (routing loading, no road) is neither.
type Change struct {
	Zone   string   // over, nearby, under, stale or ""
	Closed []string // newly closed routes
	Opened []string // routes open again after being closed
}

// Empty reports whether nothing changed that is worth a message.
func (c Change) Empty() bool { return c.Zone == "" && len(c.Closed) == 0 && len(c.Opened) == 0 }

// Decide compares two states.
func Decide(prev, cur State) Change {
	var c Change
	wet := func(z string) bool { return z == ZoneOver || z == ZoneNearby }
	switch {
	case cur.Zone == ZoneOver && prev.Zone != ZoneOver:
		c.Zone = KindOver
	case cur.Zone == ZoneNearby && !wet(prev.Zone):
		c.Zone = KindNearby
	case wet(prev.Zone) && cur.Zone == ZoneUnder:
		c.Zone = KindUnder
	case wet(prev.Zone) && cur.Zone == ZoneUnknown:
		c.Zone = KindStale
	}
	for _, r := range cur.Closed {
		if !has(prev.Closed, r) {
			c.Closed = append(c.Closed, r)
		}
	}
	for _, r := range cur.Open {
		if has(prev.Closed, r) {
			c.Opened = append(c.Opened, r)
		}
	}
	return c
}

// Fetch asks the engine for a site's zones and access routes at a
// threshold.
type Fetch func(ctx context.Context, site string, thresholdCm float64) (*upstream.SiteDetail, error)

// Sender delivers one encrypted push message.
type Sender func(ctx context.Context, s Sub, payload []byte, urgency string) error

// Service checks watched sites on a timer.
type Service struct {
	Store     *Store
	Fetch     Fetch
	Send      Sender
	PublicURL string
	Log       *slog.Logger
	// Sweep is how often every watched site is checked (access routes are
	// planned each time, so this is slower than the street watch's events).
	Sweep time.Duration
	// MinGap is the least time between two messages to one watch.
	MinGap time.Duration
	OnSent func(kind, outcome string)
	now    func() time.Time
}

// PushSender sends through the alerts package's Web Push client.
func PushSender(p *alerts.Pusher) Sender {
	return func(ctx context.Context, s Sub, payload []byte, urgency string) error {
		return p.Send(ctx, s.Endpoint, s.P256dh, s.Auth, payload, urgency)
	}
}

func (sv *Service) clock() time.Time {
	if sv.now != nil {
		return sv.now()
	}
	return time.Now()
}

// Watch stores a new watch, reads its site now and sends the first message
// saying what is watched and how things stand.
func (sv *Service) Watch(ctx context.Context, s *Sub) (string, State, error) {
	st := State{Zone: ZoneUnknown}
	if d, err := sv.Fetch(ctx, s.Site, s.ThresholdCm); err == nil {
		st = Evaluate(d)
	}
	s.Last = st
	s.SentAt = sv.clock()
	token, err := sv.Store.Add(s)
	if err != nil {
		return "", st, err
	}
	cp := *s
	go sv.deliver(context.WithoutCancel(ctx), cp, Change{Zone: KindWatching}, st)
	return token, st, nil
}

// Run checks every watched site each Sweep until ctx ends.
func (sv *Service) Run(ctx context.Context) {
	if sv.Sweep <= 0 {
		sv.Sweep = 2 * time.Minute
	}
	t := time.NewTicker(sv.Sweep)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-t.C:
			sv.CheckAll(ctx)
		}
	}
}

type pending struct {
	sub Sub
	ch  Change
	st  State
}

// CheckAll reads each watched site once and messages the watches whose
// state changed.
func (sv *Service) CheckAll(ctx context.Context) {
	now := sv.clock()
	for _, k := range sv.Store.groups() {
		d, err := sv.Fetch(ctx, k.site, k.threshold)
		if err != nil {
			if sv.Log != nil {
				sv.Log.Warn("site alerts: site not checked", "site", k.site, "error", err.Error())
			}
			continue
		}
		cur := Evaluate(d)
		var out []pending
		sv.Store.each(k, func(s *Sub) {
			ch := Decide(s.Last, cur)
			if ch.Empty() {
				s.Last = cur
				return
			}
			if now.Sub(s.SentAt) < sv.MinGap {
				return // held for the next check
			}
			s.Last, s.SentAt = cur, now
			out = append(out, pending{*s, ch, cur})
		})
		for _, p := range out {
			sv.deliver(ctx, p.sub, p.ch, p.st)
		}
	}
}

func (sv *Service) deliver(ctx context.Context, s Sub, ch Change, st State) {
	n := Compose(s, ch, st, sv.PublicURL)
	kind := ch.Zone
	if kind == "" && len(ch.Closed) > 0 {
		kind = KindRouteClosed
	} else if kind == "" {
		kind = KindRouteOpen
	}
	urgency := "normal"
	if kind == KindOver || kind == KindNearby || len(ch.Closed) > 0 {
		urgency = "high"
	}
	payload, _ := json.Marshal(map[string]string{"title": n.Title, "body": n.Body, "url": n.URL, "tag": "nm-" + s.ID, "kind": kind})
	ctx, cancel := context.WithTimeout(ctx, 20*time.Second)
	defer cancel()
	outcome := "ok"
	err := errors.New("no sender")
	if sv.Send != nil {
		err = sv.Send(ctx, s, payload, urgency)
	}
	switch {
	case errors.Is(err, alerts.ErrGone):
		outcome = "gone"
		sv.Store.Remove(s.ID)
	case err != nil:
		outcome = "failed"
		sv.Store.each(key{s.Site, s.ThresholdCm}, func(x *Sub) {
			if x.ID == s.ID {
				x.Failures++
				if x.Failures >= 5 {
					x.ExpiresAt = time.Time{}
				}
			}
		})
		if sv.Log != nil {
			sv.Log.Warn("site alert not delivered", "kind", kind, "error", err.Error())
		}
	default:
		sv.Store.each(key{s.Site, s.ThresholdCm}, func(x *Sub) {
			if x.ID == s.ID {
				x.Sent++
				x.Failures = 0
			}
		})
	}
	if sv.OnSent != nil {
		sv.OnSent(kind, outcome)
	}
}

// Link is the page a message opens.
func Link(s Sub, publicURL string) string {
	q := url.Values{"city": {s.City}, "site": {s.Site}, "lang": {s.Lang}}
	return publicURL + "/sites?" + q.Encode()
}

// RouteLabel splits "main_road:truck" into its target and vehicle.
func RouteLabel(id string) (target, vehicle string) {
	target, vehicle, _ = strings.Cut(id, ":")
	return target, vehicle
}

// ValidThreshold reports whether a watch threshold is accepted.
func ValidThreshold(cm float64) bool { return cm >= 5 && cm <= 100 }

// Describe is a short English description, for logs.
func (s Sub) Describe() string {
	return fmt.Sprintf("%s (%s) over %.0f cm", s.SiteName, s.Site, s.ThresholdCm)
}
