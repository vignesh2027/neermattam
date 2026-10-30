package sites

import (
	"context"
	"encoding/json"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"neermattam/gateway/internal/alerts"
	"neermattam/gateway/internal/upstream"
)

// fakeSite is the engine's answer for one site, changed by the test.
type fakeSite struct {
	mu     sync.Mutex
	status string
	depth  float64
	routes map[string]string // "main_road:truck" -> status
	calls  int
}

func (f *fakeSite) fetch(_ context.Context, site string, threshold float64) (*upstream.SiteDetail, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.calls++
	d := &upstream.SiteDetail{ThresholdCm: threshold}
	d.ID, d.Name, d.City, d.Status = site, "CtrlS Data Center Chennai", "chennai", f.status
	z := upstream.SiteZone{RadiusM: 300, Status: "unknown"}
	if f.depth > 0 {
		z.Deepest = &upstream.SiteDeepest{DepthBlockCm: f.depth}
	}
	d.Zones = []upstream.SiteZone{z}
	d.Access = &struct {
		ComputedAt time.Time            `json:"computed_at"`
		Routes     []upstream.SiteRoute `json:"routes"`
	}{}
	for id, st := range f.routes {
		t, v := RouteLabel(id)
		d.Access.Routes = append(d.Access.Routes, upstream.SiteRoute{Target: t, Vehicle: v, Status: st})
	}
	return d, nil
}

func (f *fakeSite) set(status string, depth float64, routes map[string]string) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.status, f.depth, f.routes = status, depth, routes
}

type sent struct {
	mu   sync.Mutex
	msgs []map[string]string
}

func (s *sent) send(_ context.Context, _ Sub, payload []byte, _ string) error {
	var m map[string]string
	_ = json.Unmarshal(payload, &m)
	s.mu.Lock()
	s.msgs = append(s.msgs, m)
	s.mu.Unlock()
	return nil
}

func (s *sent) all() []map[string]string {
	s.mu.Lock()
	defer s.mu.Unlock()
	return append([]map[string]string(nil), s.msgs...)
}

func (s *sent) wait(t *testing.T, n int) []map[string]string {
	t.Helper()
	for deadline := time.Now().Add(2 * time.Second); time.Now().Before(deadline); time.Sleep(5 * time.Millisecond) {
		if m := s.all(); len(m) >= n {
			return m
		}
	}
	t.Fatalf("wanted %d messages, got %v", n, s.all())
	return nil
}

func newService(f *fakeSite, out *sent, now *time.Time) *Service {
	st := NewStore(100, 10, 14*24*time.Hour)
	st.now = func() time.Time { return *now }
	return &Service{Store: st, Fetch: f.fetch, Send: out.send, PublicURL: "https://example.org", MinGap: 2 * time.Minute, now: func() time.Time { return *now }}
}

func sub(lang string) *Sub {
	return &Sub{Endpoint: "https://fcm.googleapis.com/fcm/send/x", P256dh: "p", Auth: "a", Site: "osm:way:1536001837", City: "chennai", Lang: lang, ThresholdCm: 15, SiteName: "CtrlS Data Center Chennai"}
}

func TestDecideCrossingsAndRoutes(t *testing.T) {
	under := State{Zone: ZoneUnder, Open: []string{"fuel:car", "main_road:truck"}}
	over := State{Zone: ZoneOver, DepthCm: 40, Open: []string{"fuel:car"}, Closed: []string{"main_road:truck"}}
	c := Decide(under, over)
	if c.Zone != KindOver || len(c.Closed) != 1 || c.Closed[0] != "main_road:truck" || len(c.Opened) != 0 {
		t.Fatalf("under to over: %+v", c)
	}
	c = Decide(over, under)
	if c.Zone != KindUnder || len(c.Opened) != 1 || len(c.Closed) != 0 {
		t.Fatalf("over to under: %+v", c)
	}
	if c := Decide(over, over); !c.Empty() {
		t.Fatalf("no change: %+v", c)
	}
	if c := Decide(State{Zone: ZoneUnknown}, State{Zone: ZoneNearby}); c.Zone != KindNearby {
		t.Fatalf("nearby: %+v", c)
	}
	if c := Decide(State{Zone: ZoneNearby}, State{Zone: ZoneOver}); c.Zone != KindOver {
		t.Fatalf("nearby to over: %+v", c)
	}
	// Water that ages out is never called clear.
	if c := Decide(over, State{Zone: ZoneUnknown}); c.Zone != KindStale {
		t.Fatalf("over to unknown: %+v", c)
	}
	if c := Decide(State{Zone: ZoneUnknown}, State{Zone: ZoneUnder}); !c.Empty() {
		t.Fatalf("unknown to under is not news: %+v", c)
	}
	// A route that was only unavailable did not close.
	if c := Decide(State{}, State{Zone: ZoneUnknown, Closed: []string{"fuel:truck"}}); len(c.Closed) != 1 {
		t.Fatalf("first closure: %+v", c)
	}
	if c := Decide(State{Closed: []string{"fuel:truck"}}, State{}); len(c.Opened) != 0 {
		t.Fatalf("closed to unavailable is not open: %+v", c)
	}
}

func TestWaterCrossingTheThresholdAndAClosedRouteSendMessages(t *testing.T) {
	now := time.Date(2026, 11, 2, 10, 0, 0, 0, time.UTC)
	f := &fakeSite{}
	f.set("under", 8, map[string]string{"main_road:truck": "open", "main_road:car": "open"})
	out := &sent{}
	sv := newService(f, out, &now)
	token, st, err := sv.Watch(context.Background(), sub("en"))
	if err != nil || token == "" || st.Zone != ZoneUnder {
		t.Fatalf("watch: %v %q %+v", err, token, st)
	}
	first := out.wait(t, 1)[0]
	if first["kind"] != KindWatching || !strings.Contains(first["title"], "Watching CtrlS") ||
		!strings.Contains(first["body"], "below 15 cm") || !strings.Contains(first["body"], "not safe") ||
		!strings.HasSuffix(first["body"], "Advisory only.") || !strings.Contains(first["url"], "/sites?") {
		t.Fatalf("first message %v", first)
	}

	// Water over the threshold within 300 m, and the car's route closes.
	now = now.Add(5 * time.Minute)
	f.set("over", 34, map[string]string{"main_road:truck": "open", "main_road:car": "no_safe_route"})
	sv.CheckAll(context.Background())
	m := out.wait(t, 2)[1]
	if m["kind"] != KindOver || !strings.Contains(m["title"], "Water at CtrlS") ||
		!strings.Contains(m["body"], "up to 34 cm within 300 m, over your 15 cm threshold") ||
		!strings.Contains(m["body"], "No route avoids deep water for: Car to the main road") {
		t.Fatalf("over message %v", m)
	}

	// No change, no message. A change inside the gap is held.
	now = now.Add(time.Minute)
	sv.CheckAll(context.Background())
	f.set("under", 10, map[string]string{"main_road:truck": "open", "main_road:car": "open"})
	sv.CheckAll(context.Background())
	if n := len(out.all()); n != 2 {
		t.Fatalf("held messages were sent: %d", n)
	}
	now = now.Add(2 * time.Minute)
	sv.CheckAll(context.Background())
	m = out.wait(t, 3)[2]
	if m["kind"] != KindUnder || !strings.Contains(m["body"], "below 15 cm") || !strings.Contains(m["body"], "open again") && !strings.Contains(m["body"], "exists again for: Car to the main road") {
		t.Fatalf("under message %v", m)
	}

	// Evidence ages out after water was over: stale, never clear.
	now = now.Add(3 * time.Minute)
	f.set("over", 30, nil)
	sv.CheckAll(context.Background())
	now = now.Add(3 * time.Minute)
	f.set("unknown", 0, nil)
	sv.CheckAll(context.Background())
	msgs := out.wait(t, 5)
	if msgs[4]["kind"] != KindStale || !strings.Contains(msgs[4]["body"], "Treat the area as flooded") {
		t.Fatalf("stale message %v", msgs[4])
	}
	// One engine call per check, however many watches share the site.
	if _, _, err := sv.Watch(context.Background(), sub("ta")); err != nil {
		t.Fatal(err)
	}
	before := f.calls
	sv.CheckAll(context.Background())
	if f.calls-before != 1 {
		t.Fatalf("%d engine calls for one site", f.calls-before)
	}
}

func TestMessagesInTamilAndTheStoreSurvivesARestart(t *testing.T) {
	s := *sub("ta")
	n := Compose(s, Change{Zone: KindOver, Closed: []string{"fuel:truck"}}, State{Zone: ZoneOver, DepthCm: 40}, "https://example.org")
	if !strings.Contains(n.Title, "நீர்") || !strings.Contains(n.Body, "டீசல் டேங்கர்") || !strings.Contains(n.Body, "ஆலோசனை மட்டுமே") {
		t.Fatalf("Tamil message %+v", n)
	}
	if strings.ContainsAny(n.Body+n.Title, "—→") {
		t.Fatal("em dash or arrow in a message")
	}
	st := NewStore(2, 1, time.Hour)
	tok, err := st.Add(sub("en"))
	if err != nil {
		t.Fatal(err)
	}
	if _, err := st.Add(sub("en")); err != ErrTooMany {
		t.Fatalf("second watch from one browser: %v", err)
	}
	path := filepath.Join(t.TempDir(), FileName)
	if err := st.Save(path); err != nil {
		t.Fatal(err)
	}
	back := NewStore(2, 1, time.Hour)
	if n, err := back.Load(path); err != nil || n != 1 {
		t.Fatalf("load: %d %v", n, err)
	}
	var id string
	for k := range back.subs {
		id = k
	}
	if _, err := back.Get(id, tok); err != nil {
		t.Fatalf("token after restart: %v", err)
	}
	if _, err := back.Get(id, "wrong"); err != ErrNotFound {
		t.Fatal("a wrong token reads the watch")
	}
	if err := back.Delete(id, tok); err != nil || back.Len() != 0 {
		t.Fatalf("delete: %v", err)
	}
	if !ValidThreshold(15) || ValidThreshold(2) || ValidThreshold(150) {
		t.Fatal("threshold bounds")
	}
	_ = alerts.ErrGone
}
