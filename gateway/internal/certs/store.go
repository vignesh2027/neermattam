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
}

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
