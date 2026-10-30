package certs

import (
	"crypto/ed25519"
	"crypto/rand"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func testdata(t *testing.T, name string) []byte {
	t.Helper()
	b, err := os.ReadFile(filepath.Join("testdata", name))
	if err != nil {
		t.Fatal(err)
	}
	return b
}

// pythonKeys trusts the test key the Python fixtures were signed with
// (perception/tests/fixtures/make_certificates.py).
func pythonKeys(t *testing.T) *KeySet {
	t.Helper()
	k, _ := NewKeySet("")
	n, err := k.AddDocument(testdata(t, "certificate_keys.json"), SourcePerception)
	if err != nil || n != 1 {
		t.Fatalf("keys: %d, %v", n, err)
	}
	return k
}

// sign makes an envelope the way perception does, with a Go key.
func sign(t *testing.T, priv ed25519.PrivateKey, payload string) []byte {
	t.Helper()
	env := Envelope{
		Format: Format, Alg: Alg, KeyID: KeyID(priv.Public().(ed25519.PublicKey)),
		Payload:   payload,
		Signature: base64.RawURLEncoding.EncodeToString(ed25519.Sign(priv, []byte(payload))),
	}
	b, _ := json.Marshal(env)
	return b
}

func TestGoSignatureRoundTrip(t *testing.T) {
	pub, priv, _ := ed25519.GenerateKey(rand.Reader)
	keys, _ := NewKeySet("")
	if _, err := keys.AddPublic(base64.RawURLEncoding.EncodeToString(pub), SourceConfig); err != nil {
		t.Fatal(err)
	}
	payload := `{"decision":{"depth_cm":23.4,"method":"wheel_geometry","usable":true},"format":"neermattam-certificate/1","measurement_id":"m_01TESTTEST"}`
	r := Verify(sign(t, priv, payload), keys)
	if !r.Valid() || r.Record == nil || r.Record.MeasurementID != "m_01TESTTEST" || *r.Record.Decision.DepthCm != 23.4 {
		t.Fatalf("round trip: %+v", r)
	}
	if !strings.Contains(r.Pretty, "\n  \"decision\"") {
		t.Errorf("pretty form: %q", r.Pretty)
	}
	forged := sign(t, priv, payload)
	forged = []byte(strings.Replace(string(forged), `23.4`, `13.4`, 1))
	if r := Verify(forged, keys); r.Status != Invalid || r.Reason != "the signature does not match the certificate" {
		t.Fatalf("tampered payload: %+v", r)
	}
}

func TestVerifiesCertificatesPythonSigned(t *testing.T) {
	keys := pythonKeys(t)
	for _, c := range []struct{ file, method string }{
		{"certificate_wheel.json", "wheel_geometry"},
		{"certificate_person.json", "person_reference"},
	} {
		r := Verify(testdata(t, c.file), keys)
		if !r.Valid() {
			t.Fatalf("%s: %+v", c.file, r)
		}
		rec := r.Record
		if rec.Decision.Method == nil || *rec.Decision.Method != c.method {
			t.Errorf("%s: method %v", c.file, rec.Decision.Method)
		}
		if rec.Input.SHA256 == nil || len(*rec.Input.SHA256) != 64 || len(rec.Steps) == 0 || len(rec.Models) != 2 {
			t.Errorf("%s: record %+v", c.file, rec)
		}
		switch c.method {
		case "wheel_geometry":
			if w := rec.Geometry.Wheel; w == nil || w.Ellipse.A <= 0 || w.Reference.DiameterCm <= 0 || w.Waterline.NPoints == 0 {
				t.Errorf("wheel numbers: %+v", w)
			}
		case "person_reference":
			if p := rec.Geometry.Person; p == nil || p.BottomY <= p.TopY || p.HeightCm < 150 {
				t.Errorf("person numbers: %+v", p)
			}
		}
	}
}

func TestTamperedPythonCertificatesFail(t *testing.T) {
	keys := pythonKeys(t)
	raw := testdata(t, "certificate_wheel.json")
	var env Envelope
	if err := json.Unmarshal(raw, &env); err != nil {
		t.Fatal(err)
	}
	encode := func(e Envelope) []byte { b, _ := json.Marshal(e); return b }

	// A deeper depth written into the signed text.
	deeper := env
	var rec map[string]any
	_ = json.Unmarshal([]byte(env.Payload), &rec)
	rec["decision"].(map[string]any)["depth_cm"] = 45.0
	b, _ := json.Marshal(rec)
	deeper.Payload = string(b)
	if r := Verify(encode(deeper), keys); r.Status != Invalid {
		t.Errorf("deeper depth: %+v", r)
	}
	// One flipped bit of the signature.
	sig, _ := DecodeB64URL(env.Signature)
	sig[10] ^= 0x01
	flipped := env
	flipped.Signature = base64.RawURLEncoding.EncodeToString(sig)
	if r := Verify(encode(flipped), keys); r.Status != Invalid {
		t.Errorf("flipped signature: %+v", r)
	}
	// Re-signed by a key nobody trusts: the key id in the certificate is not enough.
	_, other, _ := ed25519.GenerateKey(rand.Reader)
	if r := Verify(sign(t, other, env.Payload), keys); r.Status != UnknownKey {
		t.Errorf("foreign key: %+v", r)
	}
	// A trusted key filed under another key's id.
	pub, _, _ := ed25519.GenerateKey(rand.Reader)
	if r := Verify(raw, lookup{env.KeyID: pub}); r.Status != Invalid {
		t.Errorf("key under the wrong id: %+v", r)
	}
	for _, bad := range []string{`{}`, `not json`, `{"format":"x","alg":"Ed25519","key_id":"k","payload":"{}","signature":"AA"}`} {
		if r := Verify([]byte(bad), keys); r.Valid() {
			t.Errorf("%s verified", bad)
		}
	}
	// What a failing certificate claims can still be read, marked as such by the caller.
	if rec, pretty := ReadUnverified(encode(deeper)); rec == nil || *rec.Decision.DepthCm != 45 || pretty == "" {
		t.Errorf("unverified read: %+v", rec)
	}
}

type lookup map[string]ed25519.PublicKey

func (l lookup) Lookup(id string) (ed25519.PublicKey, bool) { k, ok := l[id]; return k, ok }

func TestKeySetKeepsFetchedKeysAndPinsConfiguredOnes(t *testing.T) {
	dir := t.TempDir()
	k, err := NewKeySet(dir)
	if err != nil {
		t.Fatal(err)
	}
	if n, _ := k.AddDocument(testdata(t, "certificate_keys.json"), SourcePerception); n != 1 {
		t.Fatalf("added %d", n)
	}
	if n, _ := k.AddDocument(testdata(t, "certificate_keys.json"), SourcePerception); n != 0 {
		t.Fatalf("added %d again", n)
	}
	// An entry whose id does not match its key is ignored.
	pub, _, _ := ed25519.GenerateKey(rand.Reader)
	bad := fmt.Sprintf(`{"keys":[{"key_id":"ed25519:0000000000000000","alg":"Ed25519","public_key":%q}]}`, base64.RawURLEncoding.EncodeToString(pub))
	if n, _ := k.AddDocument([]byte(bad), SourcePerception); n != 0 {
		t.Fatal("a key under the wrong id was trusted")
	}
	pinned, err := k.AddPublic(base64.StdEncoding.EncodeToString(pub), SourceConfig)
	if err != nil || k.Len() != 2 {
		t.Fatalf("pin: %v, %d keys", err, k.Len())
	}
	if _, err := k.AddPublic("not a key", SourceConfig); err == nil {
		t.Fatal("garbage accepted")
	}
	if err := k.Save(); err != nil {
		t.Fatal(err)
	}
	again, err := NewKeySet(dir)
	if err != nil || again.Len() != 1 {
		t.Fatalf("reloaded %d keys (%v); only the fetched one is saved", again.Len(), err)
	}
	if _, ok := again.Lookup(pinned); ok {
		t.Fatal("a configured key was saved")
	}
	var doc struct {
		Keys []map[string]string `json:"keys"`
	}
	if err := json.Unmarshal(k.Document(), &doc); err != nil || len(doc.Keys) != 2 || doc.Keys[0]["alg"] != Alg || doc.Keys[0]["source"] == "" {
		t.Fatalf("document: %s", k.Document())
	}
}

func TestStoreMemoryDiskAndPrune(t *testing.T) {
	cert := testdata(t, "certificate_wheel.json")
	at := time.Date(2026, 10, 30, 9, 0, 0, 0, time.UTC)
	mem := NewStore("", 2, 10)
	for _, id := range []string{"m_01AAAAAAAA", "m_01BBBBBBBB", "m_01CCCCCCCC"} {
		if err := mem.Put(id, Stored{Certificate: cert, UploadSHA256: "ab", ReceivedAt: at}); err != nil {
			t.Fatal(err)
		}
	}
	if _, ok := mem.Get("m_01AAAAAAAA"); ok {
		t.Error("the oldest was not dropped from memory")
	}
	if st, ok := mem.Get("m_01CCCCCCCC"); !ok || st.UploadSHA256 != "ab" || string(st.Certificate) != string(cert) {
		t.Errorf("get: %+v", st)
	}
	for _, bad := range []string{"../etc/passwd", "m_", "x_01AAAAAAAA", "m_01AAAA/AAAA"} {
		if mem.Put(bad, Stored{Certificate: cert}) == nil {
			t.Errorf("%q accepted", bad)
		}
		if _, ok := mem.Get(bad); ok {
			t.Errorf("%q found", bad)
		}
	}

	dir := filepath.Join(t.TempDir(), DirName)
	disk := NewStore(dir, 5, 50)
	for i := range 130 {
		if err := disk.Put(fmt.Sprintf("m_01%08d", i), Stored{Certificate: cert, ReceivedAt: at}); err != nil {
			t.Fatal(err)
		}
	}
	// A fresh store (a restart) reads from disk. The envelope may be stored
	// compacted; the signed payload string inside it is unchanged.
	fresh := NewStore(dir, 5, 50)
	st, ok := fresh.Get("m_0100000129")
	if !ok || !Verify(st.Certificate, pythonKeys(t)).Valid() || !st.ReceivedAt.Equal(at) {
		t.Fatal("not read back from disk intact")
	}
	// The 100th put pruned the folder to the newest 50.
	if _, ok := fresh.Get("m_0100000000"); ok {
		t.Error("the oldest file was not pruned")
	}
	entries, _ := os.ReadDir(dir)
	if len(entries) != 80 {
		t.Errorf("%d files on disk, want 50 after the prune plus 30", len(entries))
	}
}
