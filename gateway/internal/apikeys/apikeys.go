// Package apikeys holds the developer API's keys and their usage.
//
// Two files in the gateway's data directory keep writers apart:
//
//   - apikeys.json is written only by the admin CLI (cmd/neermattam-keys).
//     It holds each key's id (its public prefix), name, SHA-256, rate limit
//     and whether it is disabled. The key itself is printed once at
//     creation and stored nowhere.
//   - apikey-usage.json is written only by the gateway: requests per key
//     and per UTC day, for billing and for each key's own /api/v1/usage.
//
// The gateway re-reads apikeys.json when it changes, so a new or revoked
// key takes effect within seconds without a restart.
package apikeys

import (
	"crypto/rand"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/base32"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"time"
)

// File names in the data directory.
const (
	KeysFile  = "apikeys.json"
	UsageFile = "apikey-usage.json"
)

// Prefix starts every key.
const Prefix = "nm_live_"

// idLen is the number of characters after Prefix that form the public id.
const idLen = 8

// UsageDays is how many UTC days of usage are kept.
const UsageDays = 90

// Key is one stored key (never the secret itself).
type Key struct {
	ID         string    `json:"id"`
	Name       string    `json:"name"`
	Hash       string    `json:"sha256"`
	RatePerMin int       `json:"rate_per_min"`
	CreatedAt  time.Time `json:"created_at"`
	Disabled   bool      `json:"disabled"`
}

type keysFile struct {
	Version int    `json:"version"`
	Keys    []*Key `json:"keys"`
}

// Counts are requests by kind.
type Counts struct {
	Routes int64 `json:"routes"`
	Cells  int64 `json:"cells"`
}

// Usage is one key's record.
type Usage struct {
	Total    Counts            `json:"total"`
	Days     map[string]Counts `json:"days"`
	LastUsed time.Time         `json:"last_used"`
}

type usageFile struct {
	Version int               `json:"version"`
	Keys    map[string]*Usage `json:"keys"`
}

var b32 = base32.StdEncoding.WithPadding(base32.NoPadding)

// New makes a key: Prefix, then 32 characters of base32 (160 random bits).
// It returns the secret, to show once, and the record to store.
func New(name string, ratePerMin int, now time.Time) (string, *Key, error) {
	b := make([]byte, 20)
	if _, err := rand.Read(b); err != nil {
		return "", nil, err
	}
	secret := Prefix + strings.ToLower(b32.EncodeToString(b))
	return secret, &Key{ID: secret[len(Prefix) : len(Prefix)+idLen], Name: name, Hash: Hash(secret), RatePerMin: ratePerMin, CreatedAt: now.UTC()}, nil
}

// Hash is the stored form of a key.
func Hash(secret string) string {
	h := sha256.Sum256([]byte(secret))
	return hex.EncodeToString(h[:])
}

// IDOf is the public id of a key string, or "" when it is not one.
func IDOf(secret string) string {
	if !strings.HasPrefix(secret, Prefix) || len(secret) < len(Prefix)+idLen {
		return ""
	}
	return secret[len(Prefix) : len(Prefix)+idLen]
}

// ReadKeys reads apikeys.json; a missing file is no keys.
func ReadKeys(dir string) ([]*Key, error) {
	raw, err := os.ReadFile(filepath.Join(dir, KeysFile))
	if errors.Is(err, os.ErrNotExist) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	var f keysFile
	if err := json.Unmarshal(raw, &f); err != nil {
		return nil, fmt.Errorf("%s: %w", KeysFile, err)
	}
	if f.Version != 1 {
		return nil, fmt.Errorf("%s: unknown version %d", KeysFile, f.Version)
	}
	return f.Keys, nil
}

// WriteKeys replaces apikeys.json (mode 0600) through a rename.
func WriteKeys(dir string, keys []*Key) error {
	sort.Slice(keys, func(i, j int) bool { return keys[i].CreatedAt.Before(keys[j].CreatedAt) })
	raw, err := json.MarshalIndent(keysFile{Version: 1, Keys: keys}, "", "  ")
	if err != nil {
		return err
	}
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return err
	}
	return writeAtomic(filepath.Join(dir, KeysFile), append(raw, '\n'), 0o600)
}

// ReadUsage reads apikey-usage.json; a missing file is no usage.
func ReadUsage(dir string) (map[string]*Usage, error) {
	raw, err := os.ReadFile(filepath.Join(dir, UsageFile))
	if errors.Is(err, os.ErrNotExist) {
		return map[string]*Usage{}, nil
	}
	if err != nil {
		return nil, err
	}
	var f usageFile
	if err := json.Unmarshal(raw, &f); err != nil {
		return nil, fmt.Errorf("%s: %w", UsageFile, err)
	}
	if f.Keys == nil {
		f.Keys = map[string]*Usage{}
	}
	return f.Keys, nil
}

// Errors from Check.
var (
	ErrMissing  = errors.New("missing API key")
	ErrInvalid  = errors.New("invalid API key")
	ErrDisabled = errors.New("this API key is disabled")
)

// bucket is a token bucket refilled at the key's rate.
type bucket struct {
	tokens float64
	at     time.Time
}

// Store is the gateway's view: keys (reloaded when the file changes),
// rate limits and usage counters.
type Store struct {
	Dir string

	mu      sync.Mutex
	keys    map[string]*Key
	modTime time.Time
	checked time.Time
	usage   map[string]*Usage
	buckets map[string]*bucket
	dirty   bool
	now     func() time.Time
	loadErr error
}

// Open loads the keys and usage from dir.
func Open(dir string) (*Store, error) {
	s := &Store{Dir: dir, keys: map[string]*Key{}, buckets: map[string]*bucket{}, now: time.Now}
	u, err := ReadUsage(dir)
	if err != nil {
		return nil, err
	}
	s.usage = u
	s.mu.Lock()
	err = s.reloadLocked(true)
	s.mu.Unlock()
	return s, err
}

// SetClock replaces the clock, for tests.
func (s *Store) SetClock(now func() time.Time) { s.now = now }

// reloadLocked re-reads the keys file when its modification time changed,
// at most every two seconds.
func (s *Store) reloadLocked(force bool) error {
	now := s.now()
	if !force && now.Sub(s.checked) < 2*time.Second {
		return s.loadErr
	}
	s.checked = now
	fi, err := os.Stat(filepath.Join(s.Dir, KeysFile))
	if errors.Is(err, os.ErrNotExist) {
		s.keys, s.loadErr = map[string]*Key{}, nil
		return nil
	}
	if err != nil {
		s.loadErr = err
		return err
	}
	if !force && fi.ModTime().Equal(s.modTime) {
		return s.loadErr
	}
	list, err := ReadKeys(s.Dir)
	if err != nil {
		s.loadErr = err // keep the last good keys
		return err
	}
	m := make(map[string]*Key, len(list))
	for _, k := range list {
		m[k.ID] = k
	}
	s.keys, s.modTime, s.loadErr = m, fi.ModTime(), nil
	return nil
}

// Count is the number of keys loaded.
func (s *Store) Count() int {
	s.mu.Lock()
	defer s.mu.Unlock()
	_ = s.reloadLocked(false)
	return len(s.keys)
}

// Check finds the key for a presented secret.
func (s *Store) Check(secret string) (Key, error) {
	if secret == "" {
		return Key{}, ErrMissing
	}
	id := IDOf(secret)
	s.mu.Lock()
	defer s.mu.Unlock()
	_ = s.reloadLocked(false)
	k, ok := s.keys[id]
	// The hash is compared in constant time; an unknown id is compared
	// against a dummy so the timing does not reveal which ids exist.
	want := strings.Repeat("0", 64)
	if ok {
		want = k.Hash
	}
	if subtle.ConstantTimeCompare([]byte(Hash(secret)), []byte(want)) != 1 || !ok {
		return Key{}, ErrInvalid
	}
	if k.Disabled {
		return *k, ErrDisabled
	}
	return *k, nil
}

// Allow takes one request from the key's bucket. The burst is the rate per
// minute divided by six (ten seconds' worth), at least five.
func (s *Store) Allow(k Key) (ok bool, remaining int, retry time.Duration) {
	s.mu.Lock()
	defer s.mu.Unlock()
	now := s.now()
	rate := float64(max(k.RatePerMin, 1)) / 60
	burst := math.Max(5, float64(k.RatePerMin)/6)
	b := s.buckets[k.ID]
	if b == nil {
		b = &bucket{tokens: burst, at: now}
		s.buckets[k.ID] = b
	}
	b.tokens = math.Min(burst, b.tokens+now.Sub(b.at).Seconds()*rate)
	b.at = now
	if b.tokens < 1 {
		return false, 0, time.Duration((1 - b.tokens) / rate * float64(time.Second))
	}
	b.tokens--
	return true, int(b.tokens), 0
}

// Record counts one request of a kind ("routes" or "cells").
func (s *Store) Record(id, kind string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	now := s.now().UTC()
	u := s.usage[id]
	if u == nil {
		u = &Usage{Days: map[string]Counts{}}
		s.usage[id] = u
	}
	day := now.Format("2006-01-02")
	d := u.Days[day]
	switch kind {
	case "routes":
		u.Total.Routes++
		d.Routes++
	case "cells":
		u.Total.Cells++
		d.Cells++
	}
	u.Days[day] = d
	u.LastUsed = now
	s.dirty = true
}

// Usage returns a copy of a key's usage, days newest first.
func (s *Store) Usage(id string) (Usage, []DayCount) {
	s.mu.Lock()
	defer s.mu.Unlock()
	u := s.usage[id]
	if u == nil {
		return Usage{}, nil
	}
	return *u, days(u)
}

// DayCount is one UTC day's counts.
type DayCount struct {
	Date string `json:"date"`
	Counts
}

func days(u *Usage) []DayCount {
	out := make([]DayCount, 0, len(u.Days))
	for d, c := range u.Days {
		out = append(out, DayCount{d, c})
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Date > out[j].Date })
	return out
}

// Days lists a usage record's days, newest first.
func Days(u *Usage) []DayCount {
	if u == nil {
		return nil
	}
	return days(u)
}

// Save writes the usage file when it changed, dropping days older than
// UsageDays.
func (s *Store) Save() error {
	s.mu.Lock()
	if !s.dirty {
		s.mu.Unlock()
		return nil
	}
	cut := s.now().UTC().AddDate(0, 0, -UsageDays).Format("2006-01-02")
	for _, u := range s.usage {
		for d := range u.Days {
			if d < cut {
				delete(u.Days, d)
			}
		}
	}
	raw, err := json.Marshal(usageFile{Version: 1, Keys: s.usage})
	s.dirty = false
	s.mu.Unlock()
	if err == nil {
		if err = os.MkdirAll(s.Dir, 0o755); err == nil {
			err = writeAtomic(filepath.Join(s.Dir, UsageFile), raw, 0o600)
		}
	}
	if err != nil {
		s.mu.Lock()
		s.dirty = true
		s.mu.Unlock()
	}
	return err
}

func writeAtomic(path string, data []byte, mode os.FileMode) error {
	tmp, err := os.CreateTemp(filepath.Dir(path), "."+filepath.Base(path)+".tmp-*")
	if err != nil {
		return err
	}
	name := tmp.Name()
	defer os.Remove(name)
	if _, err := tmp.Write(data); err != nil {
		tmp.Close()
		return err
	}
	if err := tmp.Chmod(mode); err != nil {
		tmp.Close()
		return err
	}
	if err := tmp.Sync(); err != nil {
		tmp.Close()
		return err
	}
	if err := tmp.Close(); err != nil {
		return err
	}
	return os.Rename(name, path)
}
