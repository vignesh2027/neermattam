package certs

import (
	"crypto/ed25519"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"sync"
	"time"
)

// Key sources.
const (
	SourceConfig     = "config"     // CERTIFICATE_PUBLIC_KEYS
	SourcePerception = "perception" // fetched from perception's /v1/keys
)

// KeyFileName holds the keys fetched from perception, so certificates
// signed before a key change still verify after a restart.
const KeyFileName = "certificate-keys.json"

// Key is one trusted public key.
type Key struct {
	ID      string            `json:"key_id"`
	Alg     string            `json:"alg"`
	Public  ed25519.PublicKey `json:"-"`
	B64     string            `json:"public_key"`
	Source  string            `json:"source"`
	AddedAt time.Time         `json:"added_at"`
}

// KeySet is the set of trusted keys. It is safe for concurrent use.
type KeySet struct {
	mu   sync.RWMutex
	keys map[string]Key
	path string // empty: not persisted
}

// NewKeySet returns a key set kept in dir (empty: memory only), loading the
// keys fetched before.
func NewKeySet(dir string) (*KeySet, error) {
	k := &KeySet{keys: map[string]Key{}}
	if dir == "" {
		return k, nil
	}
	k.path = filepath.Join(dir, KeyFileName)
	b, err := os.ReadFile(k.path)
	if os.IsNotExist(err) {
		return k, nil
	}
	if err != nil {
		return k, err
	}
	var doc struct {
		Keys []Key `json:"keys"`
	}
	if err := json.Unmarshal(b, &doc); err != nil {
		return k, fmt.Errorf("%s: %w", k.path, err)
	}
	for _, e := range doc.Keys {
		if pub, ok := checkEntry(e.ID, e.Alg, e.B64); ok {
			e.Public = pub
			k.keys[e.ID] = e
		}
	}
	return k, nil
}

func checkEntry(id, alg, b64 string) (ed25519.PublicKey, bool) {
	if alg != Alg {
		return nil, false
	}
	raw, err := DecodeB64URL(b64)
	if err != nil || len(raw) != ed25519.PublicKeySize {
		return nil, false
	}
	pub := ed25519.PublicKey(raw)
	return pub, KeyID(pub) == id
}

// AddPublic trusts one base64url public key from the given source.
func (k *KeySet) AddPublic(b64, source string) (string, error) {
	raw, err := DecodeB64URL(b64)
	if err != nil || len(raw) != ed25519.PublicKeySize {
		return "", fmt.Errorf("not a base64url Ed25519 public key")
	}
	pub := ed25519.PublicKey(raw)
	id := KeyID(pub)
	k.mu.Lock()
	defer k.mu.Unlock()
	if old, ok := k.keys[id]; ok && old.Source == SourceConfig {
		return id, nil
	}
	k.keys[id] = Key{ID: id, Alg: Alg, Public: pub, B64: base64.RawURLEncoding.EncodeToString(pub), Source: source, AddedAt: time.Now().UTC()}
	return id, nil
}

// AddDocument trusts every well-formed key in a /v1/keys document and
// reports how many were new. An entry whose id does not match its key is
// skipped.
func (k *KeySet) AddDocument(doc []byte, source string) (int, error) {
	var d struct {
		Keys []struct {
			ID     string `json:"key_id"`
			Alg    string `json:"alg"`
			Public string `json:"public_key"`
		} `json:"keys"`
	}
	if err := json.Unmarshal(doc, &d); err != nil {
		return 0, err
	}
	added := 0
	for _, e := range d.Keys {
		if _, ok := checkEntry(e.ID, e.Alg, e.Public); !ok {
			continue
		}
		if _, known := k.Lookup(e.ID); known {
			continue
		}
		if _, err := k.AddPublic(e.Public, source); err == nil {
			added++
		}
	}
	return added, nil
}

// Lookup implements Keys.
func (k *KeySet) Lookup(id string) (ed25519.PublicKey, bool) {
	k.mu.RLock()
	defer k.mu.RUnlock()
	e, ok := k.keys[id]
	return e.Public, ok
}

// Len is the number of trusted keys.
func (k *KeySet) Len() int {
	k.mu.RLock()
	defer k.mu.RUnlock()
	return len(k.keys)
}

// List returns the keys, oldest first.
func (k *KeySet) List() []Key {
	k.mu.RLock()
	out := make([]Key, 0, len(k.keys))
	for _, e := range k.keys {
		out = append(out, e)
	}
	k.mu.RUnlock()
	sort.Slice(out, func(i, j int) bool {
		if !out[i].AddedAt.Equal(out[j].AddedAt) {
			return out[i].AddedAt.Before(out[j].AddedAt)
		}
		return out[i].ID < out[j].ID
	})
	return out
}

// Document is the well-known keys document: perception's /v1/keys shape
// plus each key's source.
func (k *KeySet) Document() []byte {
	type entry struct {
		ID     string `json:"key_id"`
		Alg    string `json:"alg"`
		Public string `json:"public_key"`
		Source string `json:"source"`
	}
	doc := struct {
		Keys []entry `json:"keys"`
	}{Keys: []entry{}}
	for _, e := range k.List() {
		doc.Keys = append(doc.Keys, entry{e.ID, e.Alg, e.B64, e.Source})
	}
	b, _ := json.MarshalIndent(doc, "", "  ")
	return b
}

// Save writes the keys fetched from perception (not the pinned ones, which
// come from the configuration each start), through a temporary file.
func (k *KeySet) Save() error {
	if k.path == "" {
		return nil
	}
	var keep []Key
	for _, e := range k.List() {
		if e.Source == SourcePerception {
			keep = append(keep, e)
		}
	}
	b, err := json.MarshalIndent(struct {
		Keys []Key `json:"keys"`
	}{keep}, "", "  ")
	if err != nil {
		return err
	}
	if err := os.MkdirAll(filepath.Dir(k.path), 0o755); err != nil {
		return err
	}
	tmp := k.path + ".tmp"
	if err := os.WriteFile(tmp, b, 0o644); err != nil {
		return err
	}
	return os.Rename(tmp, k.path)
}
