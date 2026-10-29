package alerts

import (
	"context"
	"crypto/aes"
	"crypto/cipher"
	"crypto/ecdh"
	"crypto/hkdf"
	"crypto/rand"
	"crypto/sha256"
	"encoding/binary"
	"encoding/json"
	"fmt"
	"io"
	"math"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"neermattam/gateway/internal/upstream"
)

// decrypt is the receiving browser's side of RFC 8291, for the tests.
func decrypt(t *testing.T, body []byte, ua *ecdh.PrivateKey, auth []byte) []byte {
	t.Helper()
	salt, idlen := body[:16], int(body[20])
	asPub, sealed := body[21:21+idlen], body[21+idlen:]
	if binary.BigEndian.Uint32(body[16:20]) != 4096 {
		t.Fatal("record size")
	}
	as, err := ecdh.P256().NewPublicKey(asPub)
	if err != nil {
		t.Fatal(err)
	}
	secret, _ := ua.ECDH(as)
	prkKey, _ := hkdf.Extract(sha256.New, secret, auth)
	ikm, _ := hkdf.Expand(sha256.New, prkKey, "WebPush: info\x00"+string(ua.PublicKey().Bytes())+string(asPub), 32)
	prk, _ := hkdf.Extract(sha256.New, ikm, salt)
	cek, _ := hkdf.Expand(sha256.New, prk, "Content-Encoding: aes128gcm\x00", 16)
	nonce, _ := hkdf.Expand(sha256.New, prk, "Content-Encoding: nonce\x00", 12)
	block, _ := aes.NewCipher(cek)
	gcm, _ := cipher.NewGCM(block)
	plain, err := gcm.Open(nil, nonce, sealed, nil)
	if err != nil {
		t.Fatalf("decrypt: %v", err)
	}
	if plain[len(plain)-1] != 0x02 {
		t.Fatal("missing last-record delimiter")
	}
	return plain[:len(plain)-1]
}

// hexCell makes a hexagon of radius r degrees around a point.
func hexCell(id string, lat, lon, depth, upper float64, class string) upstream.CellFeature {
	const r = 0.0006
	var ring [][2]float64
	for i := 0; i <= 6; i++ {
		a := float64(i%6) * math.Pi / 3
		ring = append(ring, [2]float64{lon + r*math.Cos(a), lat + r*math.Sin(a)})
	}
	u := upper
	return upstream.CellFeature{ID: id, Lat: lat, Lon: lon, RawPolygon: [][][2]float64{ring},
		Props: upstream.CellProps{Cell: id, City: "chennai", DepthCm: depth, DepthSigmaCm: 3, DepthUpperCm: &u, HazardClass: class, HazardClassUpper: class}}
}

func cellJSON(c upstream.CellFeature) []byte {
	b, _ := json.Marshal(map[string]any{"type": "Feature", "id": c.ID,
		"geometry":   map[string]any{"type": "Polygon", "coordinates": c.RawPolygon},
		"properties": c.Props})
	return b
}

type pushLog struct {
	mu   sync.Mutex
	msgs []map[string]string
	hdrs []http.Header
}

func (p *pushLog) all() []map[string]string {
	p.mu.Lock()
	defer p.mu.Unlock()
	return append([]map[string]string(nil), p.msgs...)
}

func TestWatchPointOverWorseClearAndStale(t *testing.T) {
	ua, _ := ecdh.P256().GenerateKey(rand.Reader)
	auth := make([]byte, 16)
	_, _ = rand.Read(auth)
	got := &pushLog{}
	push := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		var m map[string]string
		if err := json.Unmarshal(decrypt(t, body, ua, auth), &m); err != nil {
			t.Errorf("payload: %v", err)
		}
		got.mu.Lock()
		got.msgs = append(got.msgs, m)
		got.hdrs = append(got.hdrs, r.Header.Clone())
		got.mu.Unlock()
		w.WriteHeader(http.StatusCreated)
	}))
	defer push.Close()

	const lat, lon = 12.9791, 80.2209
	var mu sync.Mutex
	cells := []upstream.CellFeature{}
	v, _ := LoadVAPID("", "", "", "mailto:ops@example.org")
	sv := &Service{
		Store:  NewStore(100, 10, 24*time.Hour),
		Pusher: &Pusher{VAPID: v, Client: push.Client(), AllowHost: func(string) bool { return true }},
		Cells: func(context.Context, string) ([]upstream.CellFeature, error) {
			mu.Lock()
			defer mu.Unlock()
			return append([]upstream.CellFeature(nil), cells...), nil
		},
		PublicURL: "https://neermattam.example", Sweep: time.Hour,
	}
	sv.Start()
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go sv.Run(ctx)

	sub := &Subscription{Channel: ChannelPush, Endpoint: push.URL + "/push/abc", P256dh: b64.EncodeToString(ua.PublicKey().Bytes()), Auth: b64.EncodeToString(auth),
		City: "chennai", Vehicle: "car", Lang: "en", Point: &Point{lat, lon}, Label: "near Velachery"}
	token, st, err := sv.Watch(ctx, sub)
	if err != nil || token == "" || st.State != StateUnknown {
		t.Fatalf("watch: %v %q %+v", err, token, st)
	}
	wait := func(n int) []map[string]string {
		t.Helper()
		deadline := time.Now().Add(5 * time.Second)
		for time.Now().Before(deadline) {
			if m := got.all(); len(m) >= n {
				return m
			}
			time.Sleep(10 * time.Millisecond)
		}
		t.Fatalf("waited for %d messages, have %d: %v", n, len(got.all()), got.all())
		return nil
	}
	m := wait(1)
	if m[0]["kind"] != KindWatching || !strings.Contains(m[0]["body"], "unknown, not safe") || !strings.Contains(m[0]["title"], "near Velachery") {
		t.Fatalf("first message %v", m[0])
	}
	if a := got.hdrs[0].Get("Authorization"); !strings.HasPrefix(a, "vapid t=") || got.hdrs[0].Get("Content-Encoding") != "aes128gcm" {
		t.Fatalf("headers %v", got.hdrs[0])
	}

	// A neighbouring cell changing does not concern this point.
	sv.OnCell("chennai", cellJSON(hexCell("far", lat+0.01, lon, 60, 65, "danger_most")))
	// The point's own cell goes over the car limit.
	sv.OnCell("chennai", cellJSON(hexCell("here", lat, lon, 22, 26, "danger_some")))
	m = wait(2)
	if m[1]["kind"] != KindOver || !strings.Contains(m[1]["body"], "26 cm deep, over the 15 cm limit for cars") || !strings.Contains(m[1]["body"], "Advisory") {
		t.Fatalf("over message %v", m[1])
	}
	if got.hdrs[1].Get("Urgency") != "high" {
		t.Fatal("an over-the-limit alert is urgent")
	}
	// Deeper and more dangerous.
	sv.OnCell("chennai", cellJSON(hexCell("here", lat, lon, 45, 50, "danger_most")))
	m = wait(3)
	if m[2]["kind"] != KindWorse {
		t.Fatalf("worse message %v", m[2])
	}
	// Below the limit again.
	sv.OnCell("chennai", cellJSON(hexCell("here", lat, lon, 6, 9, "low")))
	m = wait(4)
	if m[3]["kind"] != KindClear || !strings.Contains(m[3]["body"], "below the 15 cm limit for cars") {
		t.Fatalf("clear message %v", m[3])
	}
	// Over again, then the evidence expires: out of date, never "clear".
	sv.OnCell("chennai", cellJSON(hexCell("here", lat, lon, 30, 34, "danger_some")))
	wait(5)
	gone := hexCell("here", lat, lon, 30, 34, "danger_some")
	gone.Props.Expired = true
	sv.OnCell("chennai", cellJSON(gone))
	m = wait(6)
	if m[5]["kind"] != KindStale || strings.Contains(m[5]["body"], "below") {
		t.Fatalf("stale message %v", m[5])
	}
	for _, x := range m {
		if !strings.Contains(x["body"], "Advisory") {
			t.Errorf("%s message is not marked advisory: %s", x["kind"], x["body"])
		}
		if strings.Contains(strings.ToLower(x["body"]), " is safe") {
			t.Errorf("a message calls water safe: %s", x["body"])
		}
	}
	// The token reads and deletes it.
	if _, err := sv.Store.Get(sub.ID, "wrong"); err != ErrWrongToken {
		t.Fatalf("wrong token: %v", err)
	}
	if err := sv.Store.Delete(sub.ID, token); err != nil || sv.Store.Len() != 0 {
		t.Fatalf("delete: %v", err)
	}
}

func TestDrainedWaterIsCaughtBySweepAndGoneSubscriptionsAreDropped(t *testing.T) {
	ua, _ := ecdh.P256().GenerateKey(rand.Reader)
	auth := make([]byte, 16)
	status := http.StatusCreated
	var sent int
	var mu sync.Mutex
	push := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		mu.Lock()
		sent++
		code := status
		mu.Unlock()
		w.WriteHeader(code)
	}))
	defer push.Close()
	cells := []upstream.CellFeature{hexCell("here", 13.0, 80.2, 40, 44, "danger_most")}
	v, _ := LoadVAPID("", "", "", "mailto:ops@example.org")
	sv := &Service{Store: NewStore(100, 10, time.Hour),
		Pusher: &Pusher{VAPID: v, Client: push.Client(), AllowHost: func(string) bool { return true }},
		Cells: func(context.Context, string) ([]upstream.CellFeature, error) {
			mu.Lock()
			defer mu.Unlock()
			return append([]upstream.CellFeature(nil), cells...), nil
		}, Sweep: time.Hour}
	sv.Start()
	sub := &Subscription{Channel: ChannelPush, Endpoint: push.URL + "/x", P256dh: b64.EncodeToString(ua.PublicKey().Bytes()), Auth: b64.EncodeToString(auth),
		City: "chennai", Vehicle: "bus", Lang: "en", Point: &Point{13.0, 80.2}, Label: "near Guindy"}
	_, st, err := sv.Watch(context.Background(), sub)
	if err != nil || st.State != StateOver {
		t.Fatalf("a bus at 44 cm is over its 30 cm limit: %+v %v", st, err)
	}
	// The water drains with no new report: only the sweep notices.
	mu.Lock()
	cells = []upstream.CellFeature{hexCell("here", 13.0, 80.2, 10, 14, "low")}
	mu.Unlock()
	sv.SweepAll(context.Background())
	s, _ := sv.Store.Get(sub.ID, "x")
	_ = s
	sv.Store.each("chennai", func(x *Subscription) bool {
		if x.Last.State != StateUnder {
			t.Errorf("after the sweep the state is %s", x.Last.State)
		}
		return false
	})
	// The push service says the subscription is gone: it is dropped.
	mu.Lock()
	status = http.StatusGone
	cells = []upstream.CellFeature{hexCell("here", 13.0, 80.2, 50, 55, "danger_most")}
	mu.Unlock()
	sv.SweepAll(context.Background())
	if sv.Store.Len() != 0 {
		t.Fatal("a 410 from the push service should drop the subscription")
	}
}

func TestRoutesAreWatchedAlongTheirLength(t *testing.T) {
	line := [][2]float64{{80.20, 13.00}, {80.21, 13.00}, {80.21, 13.01}}
	pts := SampleRoute(line, 25, 800)
	if len(pts) < 80 || pts[0] != line[0] || pts[len(pts)-1] != line[2] {
		t.Fatalf("%d samples, first %v last %v", len(pts), pts[0], pts[len(pts)-1])
	}
	for i := 1; i < len(pts); i++ {
		if d := distM(pts[i-1], pts[i]); d > 26 {
			t.Fatalf("gap of %.0f m between samples", d)
		}
	}
	long := SampleRoute([][2]float64{{80.0, 13.0}, {80.5, 13.0}}, 25, 100)
	if len(long) > 100 {
		t.Fatalf("cap: %d", len(long))
	}
	// Water in the middle of the route, far from either end, is found.
	sub := &Subscription{Vehicle: "car", Route: pts}
	sub.bounds()
	ix := newIndex([]upstream.CellFeature{hexCell("mid", 13.0, 80.21, 30, 35, "danger_some")})
	if st := evaluate(sub, ix); st.State != StateOver || st.DepthCm != 35 {
		t.Fatalf("route status %+v", st)
	}
	// People on foot are kept out of "danger for some", not a depth.
	walk := &Subscription{Vehicle: "pedestrian", Point: &Point{13.0, 80.21}}
	if st := evaluate(walk, newIndex([]upstream.CellFeature{hexCell("a", 13.0, 80.21, 12, 14, "danger_some")})); st.State != StateOver {
		t.Fatalf("walking: %+v", st)
	}
	if st := evaluate(walk, newIndex([]upstream.CellFeature{hexCell("a", 13.0, 80.21, 12, 14, "low")})); st.State != StateUnder {
		t.Fatalf("walking, low: %+v", st)
	}
}

func TestStoreCapsAndPersists(t *testing.T) {
	st := NewStore(3, 2, time.Hour)
	mk := func(ep string) *Subscription {
		return &Subscription{Channel: ChannelPush, Endpoint: ep, City: "chennai", Vehicle: "car", Point: &Point{13, 80.2}}
	}
	if _, err := st.Add(mk("https://a")); err != nil {
		t.Fatal(err)
	}
	if _, err := st.Add(mk("https://a")); err != nil {
		t.Fatal(err)
	}
	if _, err := st.Add(mk("https://a")); err != ErrTooMany {
		t.Fatalf("third for one browser: %v", err)
	}
	tok, _ := st.Add(mk("https://b"))
	if _, err := st.Add(mk("https://c")); err != ErrFull {
		t.Fatalf("over the total: %v", err)
	}
	path := filepath.Join(t.TempDir(), FileName)
	if err := st.Save(path); err != nil {
		t.Fatal(err)
	}
	again := NewStore(3, 2, time.Hour)
	if n, err := again.Load(path); err != nil || n != 3 {
		t.Fatalf("load: %d %v", n, err)
	}
	var id string
	again.each("", func(s *Subscription) bool {
		if s.Endpoint == "https://b" {
			id = s.ID
		}
		return false
	})
	if _, err := again.Get(id, tok); err != nil {
		t.Fatalf("token after reload: %v", err)
	}
	// Expired watches are pruned.
	again.now = func() time.Time { return time.Now().Add(2 * time.Hour) }
	if again.Len() != 3 {
		t.Fatal("Len does not prune")
	}
	again.each("", func(*Subscription) bool { return false })
	if again.Len() != 0 {
		t.Fatalf("expired watches remain: %d", again.Len())
	}
	_ = fmt.Sprint
}
