package certs

import (
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"time"

	"neermattam/gateway/internal/upstream"
)

// DirName is the folder under the data directory that holds certificates.
const DirName = "certificates"

// Stored is one report's certificate as the gateway keeps it: the
// certificate exactly as perception sent it, the SHA-256 of the file the
// gateway received, and when.
type Stored struct {
	Certificate  json.RawMessage `json:"certificate"`
	UploadSHA256 string          `json:"upload_sha256"`
	ReceivedAt   time.Time       `json:"received_at"`
	// Decision is what the gateway did with the measurement once it had
	// checked it: added it to the map, refused it, or found no depth. It is
	// the gateway's own record, outside the signed certificate, and is nil
	// for certificates kept before decisions were recorded.
	Decision *Decision `json:"decision,omitempty"`
	// ReasonCodes and StepCodes are perception's codes for the reasons and
	// step decisions the certificate records in English (CONTRACTS.md
	// 1.3), one per reason and per step, so its page can show them in the
	// visitor's language. They are outside the signed bytes, and empty for
	// certificates kept before codes were.
	ReasonCodes []upstream.Code  `json:"reason_codes,omitempty"`
	StepCodes   []*upstream.Code `json:"step_codes,omitempty"`
}

// Decision statuses.
const (
	DecisionFused    = "fused"
	DecisionRejected = "rejected"
	DecisionUnusable = "unusable"
	// DecisionArchival is an archival photo measured for the "Measured
	// photos" layer: shown as such, never added to the live map.
	DecisionArchival = "archival"
	// DecisionSample is one of the gateway's sample photos: measured live,
	// never added to the map.
	DecisionSample = "sample"
)

// Decision is the gateway's verdict on a measured report.
type Decision struct {
	Status    string    `json:"status"`
	Trust     float64   `json:"trust,omitempty"`
	ReadingID string    `json:"reading_id,omitempty"`
	Cell      string    `json:"cell,omitempty"`
	DecidedAt time.Time `json:"decided_at"`
	// Reasons are the checks that refused it (status rejected), or
	// perception's own reasons (status unusable).
	Reasons []DecisionReason `json:"reasons,omitempty"`
}

// DecisionReason is one reason, with its text in every page language
// (English under "en"), written when the decision was made because the
// arguments behind a translated sentence are not kept.
type DecisionReason struct {
	Rule    string            `json:"rule,omitempty"`
	Outcome string            `json:"outcome,omitempty"`
	Text    map[string]string `json:"text"`
	// Code is perception's code for one of its own reasons (status
	// unusable), rendered in the page language when shown; nil for the
	// gateway's checks, whose text is above.
	Code *upstream.Code `json:"code,omitempty"`
}

// ErrNoCertificate is returned when a decision names a certificate the
// store does not hold.
var ErrNoCertificate = errors.New("no certificate with that id")

// Store keeps certificates per measurement id: on disk under dir (the
// newest maxDisk), with the newest maxMem also in memory; in memory only
// when dir is empty. Measurement ids are ULIDs, so their order is the
// order they were made in.
type Store struct {
	dir     string
	maxMem  int
	maxDisk int

	mu    sync.Mutex
	mem   map[string]Stored
	order []string // ids in memory, oldest first
	puts  int
}

// NewStore returns a store. dir may be empty.
func NewStore(dir string, maxMem, maxDisk int) *Store {
	return &Store{dir: dir, maxMem: max(1, maxMem), maxDisk: max(1, maxDisk), mem: map[string]Stored{}}
}

// ErrBadID is returned for an id that is not a measurement id.
var ErrBadID = errors.New("not a measurement id")

// Put keeps a certificate.
func (s *Store) Put(id string, st Stored) error {
	if !ValidID(id) {
		return ErrBadID
	}
	s.mu.Lock()
	if _, ok := s.mem[id]; !ok {
		s.order = append(s.order, id)
	}
	s.mem[id] = st
	for len(s.order) > s.maxMem {
		delete(s.mem, s.order[0])
		s.order = s.order[1:]
	}
	s.puts++
	prune := s.puts%100 == 0
	s.mu.Unlock()
	if s.dir == "" {
		return nil
	}
	b, err := json.Marshal(st)
	if err != nil {
		return err
	}
	if err := os.MkdirAll(s.dir, 0o755); err != nil {
		return err
	}
	path := filepath.Join(s.dir, id+".json")
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, b, 0o644); err != nil {
		return err
	}
	if err := os.Rename(tmp, path); err != nil {
		return err
	}
	if prune {
		s.prune()
	}
	return nil
}

// SetDecision records the gateway's decision beside a stored certificate.
func (s *Store) SetDecision(id string, d Decision) error {
	st, ok := s.Get(id)
	if !ok {
		return ErrNoCertificate
	}
	st.Decision = &d
	return s.Put(id, st)
}

// Get returns a certificate, from memory or disk.
func (s *Store) Get(id string) (Stored, bool) {
	if !ValidID(id) {
		return Stored{}, false
	}
	s.mu.Lock()
	st, ok := s.mem[id]
	s.mu.Unlock()
	if ok || s.dir == "" {
		return st, ok
	}
	b, err := os.ReadFile(filepath.Join(s.dir, id+".json"))
	if err != nil {
		return Stored{}, false
	}
	if json.Unmarshal(b, &st) != nil || len(st.Certificate) == 0 {
		return Stored{}, false
	}
	return st, true
}

// prune removes the oldest files beyond maxDisk.
func (s *Store) prune() {
	entries, err := os.ReadDir(s.dir)
	if err != nil {
		return
	}
	var ids []string
	for _, e := range entries {
		if name := e.Name(); strings.HasSuffix(name, ".json") && ValidID(strings.TrimSuffix(name, ".json")) {
			ids = append(ids, name)
		}
	}
	if len(ids) <= s.maxDisk {
		return
	}
	sort.Strings(ids)
	for _, name := range ids[:len(ids)-s.maxDisk] {
		_ = os.Remove(filepath.Join(s.dir, name))
	}
}
