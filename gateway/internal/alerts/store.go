// Package alerts is "Watch my street": a visitor picks a point or a route
// and a vehicle, and is told when the water there goes over that vehicle's
// limit, when the hazard worsens, and when it is below the limit again. The
// same watcher feeds two channels: Web Push (VAPID, implemented here with
// the standard library) and WhatsApp messages. Subscriptions are kept in
// the gateway's data directory, capped in number, and expire.
package alerts

import (
	"crypto/rand"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"sync"
	"time"
)

// FileName is the store's file in the data directory.
const FileName = "alerts.json"

// Channels.
const (
	ChannelPush     = "webpush"
	ChannelWhatsApp = "whatsapp"
)

// States of the water at a watched place.
const (
	StateOver    = "over"    // some watched cell is over the vehicle's limit
	StateUnder   = "under"   // reports in force, none over the limit
	StateUnknown = "unknown" // no report in force at the place
)

// Point is a watched place.
type Point struct {
	Lat float64 `json:"lat"`
	Lon float64 `json:"lon"`
}

// Status is the water at a subscription's place.
type Status struct {
	State   string  `json:"state"`
	Class   string  `json:"class,omitempty"`    // worst hazard class at the blocking depth
	DepthCm float64 `json:"depth_cm,omitempty"` // deepest blocking depth among watched cells
	Cells   int     `json:"cells"`              // watched cells with evidence in force
}

// Subscription is one watch.
type Subscription struct {
	ID        string `json:"id"`
	TokenHash string `json:"token_hash"`
	Channel   string `json:"channel"`

	Endpoint string `json:"endpoint,omitempty"`
	P256dh   string `json:"p256dh,omitempty"`
	Auth     string `json:"auth,omitempty"`
	Phone    string `json:"phone,omitempty"`
	PhoneID  string `json:"phone_id,omitempty"`

	City    string       `json:"city"`
	Vehicle string       `json:"vehicle"`
	Lang    string       `json:"lang"`
	Point   *Point       `json:"point,omitempty"`
	Route   [][2]float64 `json:"route,omitempty"`    // lon,lat samples along a route
	Label   string       `json:"label"`              // "near Velachery", or a route's start
	LabelTo string       `json:"label_to,omitempty"` // a route's end

	CreatedAt time.Time `json:"created_at"`
	ExpiresAt time.Time `json:"expires_at"`

	Last     Status    `json:"last"`
	SentAt   time.Time `json:"sent_at,omitempty"`
	Sent     int       `json:"sent"`
	Failures int       `json:"failures"`

	minLat, minLon, maxLat, maxLon float64
}

// Targets are the points the watch checks.
func (s *Subscription) Targets() []Point {
	if s.Point != nil {
		return []Point{*s.Point}
	}
	out := make([]Point, len(s.Route))
	for i, p := range s.Route {
		out[i] = Point{Lat: p[1], Lon: p[0]}
	}
	return out
}

func (s *Subscription) bounds() {
	s.minLat, s.minLon, s.maxLat, s.maxLon = 90, 180, -90, -180
	for _, p := range s.Targets() {
		s.minLat, s.maxLat = min(s.minLat, p.Lat), max(s.maxLat, p.Lat)
		s.minLon, s.maxLon = min(s.minLon, p.Lon), max(s.maxLon, p.Lon)
	}
}

// Errors.
var (
	ErrFull        = errors.New("the gateway holds its maximum number of watches")
	ErrTooMany     = errors.New("this browser or number already watches the maximum number of places")
	ErrNotFound    = errors.New("no such watch")
	ErrWrongToken  = errors.New("wrong token for this watch")
	errBadSnapshot = errors.New("not an alerts file")
)

// Store keeps subscriptions in memory and in one JSON file.
type Store struct {
	MaxTotal      int
	MaxPerAddress int
	TTL           time.Duration

	mu    sync.Mutex
	subs  map[string]*Subscription
	dirty bool
	now   func() time.Time
}

// NewStore returns an empty store.
func NewStore(maxTotal, maxPer int, ttl time.Duration) *Store {
	return &Store{MaxTotal: maxTotal, MaxPerAddress: maxPer, TTL: ttl, subs: map[string]*Subscription{}, now: time.Now}
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

func (s *Subscription) address() string {
	if s.Channel == ChannelWhatsApp {
		return "wa:" + s.Phone
	}
	return "push:" + s.Endpoint
}

// Add stores a new subscription and returns its token, shown once.
func (st *Store) Add(sub *Subscription) (string, error) {
	st.mu.Lock()
	defer st.mu.Unlock()
	st.pruneLocked()
	if len(st.subs) >= st.MaxTotal {
		return "", ErrFull
	}
	n := 0
	for _, o := range st.subs {
		if o.address() == sub.address() {
			n++
		}
	}
	if n >= st.MaxPerAddress {
		return "", ErrTooMany
	}
	token := randomHex(16)
	now := st.now()
	sub.ID = randomHex(8)
	sub.TokenHash = hashToken(token)
	sub.CreatedAt = now.UTC()
	sub.ExpiresAt = now.Add(st.TTL).UTC()
	sub.bounds()
	st.subs[sub.ID] = sub
	st.dirty = true
	return token, nil
}

// Get returns a copy of a subscription after checking its token.
func (st *Store) Get(id, token string) (Subscription, error) {
	st.mu.Lock()
	defer st.mu.Unlock()
	s, ok := st.subs[id]
	if !ok {
		return Subscription{}, ErrNotFound
	}
	if subtle.ConstantTimeCompare([]byte(hashToken(token)), []byte(s.TokenHash)) != 1 {
		return Subscription{}, ErrWrongToken
	}
	return *s, nil
}

// Delete removes a subscription after checking its token.
func (st *Store) Delete(id, token string) error {
	if _, err := st.Get(id, token); err != nil {
		return err
	}
	st.Remove(id)
	return nil
}

// Remove drops a subscription without a token (the push service said it is
// gone, or the number sent "stop").
func (st *Store) Remove(id string) {
	st.mu.Lock()
	defer st.mu.Unlock()
	if _, ok := st.subs[id]; ok {
		delete(st.subs, id)
		st.dirty = true
	}
}

// RemovePhone drops every WhatsApp watch of a number.
func (st *Store) RemovePhone(phone string) int {
	st.mu.Lock()
	defer st.mu.Unlock()
	n := 0
	for id, s := range st.subs {
		if s.Channel == ChannelWhatsApp && s.Phone == phone {
			delete(st.subs, id)
			n++
		}
	}
	if n > 0 {
		st.dirty = true
	}
	return n
}

// Len is the number of live subscriptions.
func (st *Store) Len() int {
	st.mu.Lock()
	defer st.mu.Unlock()
	return len(st.subs)
}

// Cities lists the cities that have watches.
func (st *Store) Cities() []string {
	st.mu.Lock()
	defer st.mu.Unlock()
	seen := map[string]bool{}
	for _, s := range st.subs {
		seen[s.City] = true
	}
	out := make([]string, 0, len(seen))
	for c := range seen {
		out = append(out, c)
	}
	sort.Strings(out)
	return out
}

// each calls fn with every live subscription of a city under the lock; fn
// may update the subscription in place and returns whether it changed.
func (st *Store) each(city string, fn func(*Subscription) bool) {
	st.mu.Lock()
	defer st.mu.Unlock()
	st.pruneLocked()
	for _, s := range st.subs {
		if city == "" || s.City == city {
			if fn(s) {
				st.dirty = true
			}
		}
	}
}

func (st *Store) pruneLocked() {
	now := st.now()
	for id, s := range st.subs {
		if now.After(s.ExpiresAt) {
			delete(st.subs, id)
			st.dirty = true
		}
	}
}

type file struct {
	Version       int             `json:"version"`
	Subscriptions []*Subscription `json:"subscriptions"`
}

// Load reads the store's file; a missing file is an empty store.
func (st *Store) Load(path string) (int, error) {
	raw, err := os.ReadFile(path)
	if errors.Is(err, os.ErrNotExist) {
		return 0, nil
	}
	if err != nil {
		return 0, err
	}
	var f file
	if err := json.Unmarshal(raw, &f); err != nil {
		return 0, fmt.Errorf("%s: %w", path, err)
	}
	if f.Version != 1 {
		return 0, fmt.Errorf("%s: %w (version %d)", path, errBadSnapshot, f.Version)
	}
	st.mu.Lock()
	defer st.mu.Unlock()
	for _, s := range f.Subscriptions {
		if s == nil || s.ID == "" {
			continue
		}
		s.bounds()
		st.subs[s.ID] = s
	}
	st.pruneLocked()
	return len(st.subs), nil
}

// Save writes the file when something changed, through a temporary file
// and a rename. The file holds push endpoints and phone numbers, so it is
// readable by the owner only.
func (st *Store) Save(path string) error {
	st.mu.Lock()
	if !st.dirty {
		st.mu.Unlock()
		return nil
	}
	f := file{Version: 1}
	for _, s := range st.subs {
		c := *s
		f.Subscriptions = append(f.Subscriptions, &c)
	}
	st.dirty = false
	st.mu.Unlock()
	sort.Slice(f.Subscriptions, func(i, j int) bool { return f.Subscriptions[i].ID < f.Subscriptions[j].ID })
	raw, err := json.Marshal(f)
	if err == nil {
		if err = os.MkdirAll(filepath.Dir(path), 0o755); err == nil {
			err = writeFileAtomic(path, raw, 0o600)
		}
	}
	if err != nil {
		st.mu.Lock()
		st.dirty = true
		st.mu.Unlock()
	}
	return err
}
