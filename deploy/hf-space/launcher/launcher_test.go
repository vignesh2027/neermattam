package main

import (
	"bufio"
	"bytes"
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

func quietLog() *slog.Logger { return slog.New(slog.NewTextHandler(io.Discard, nil)) }

func TestBudgetCapsAndResetsAtIndianMidnight(t *testing.T) {
	now := time.Date(2026, 10, 28, 18, 0, 0, 0, time.UTC) // 23:30 IST
	b := &Budget{Limit: 2, Now: func() time.Time { return now }}
	for i := 1; i <= 2; i++ {
		ok, used, _ := b.Take()
		if !ok || used != i {
			t.Fatalf("call %d: ok=%v used=%d", i, ok, used)
		}
	}
	ok, used, reset := b.Take()
	if ok || used != 2 {
		t.Fatalf("third call allowed: ok=%v used=%d", ok, used)
	}
	if want := time.Date(2026, 10, 28, 18, 30, 0, 0, time.UTC); !reset.Equal(want) {
		t.Fatalf("reset %v, want %v", reset, want)
	}
	now = now.Add(31 * time.Minute) // 00:01 IST the next day
	if ok, used, _ := b.Take(); !ok || used != 1 {
		t.Fatalf("after midnight: ok=%v used=%d", ok, used)
	}
}

func TestBudgetZeroRefusesEverything(t *testing.T) {
	b := &Budget{Limit: 0, Now: time.Now}
	if ok, _, _ := b.Take(); ok {
		t.Fatal("a zero cap allowed a call")
	}
}

func fakePerception(t *testing.T, calls *atomic.Int64) *httptest.Server {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		switch r.URL.Path {
		case "/v1/measure/stream":
			w.Header().Set("Content-Type", "text/event-stream")
			f := w.(http.Flusher)
			io.WriteString(w, "event: step\ndata: {\"step\":\"scene\"}\n\n")
			f.Flush()
			time.Sleep(300 * time.Millisecond)
			io.WriteString(w, "event: result\ndata: {\"usable\":false}\n\n")
		default:
			w.Header().Set("Content-Type", "application/json")
			io.WriteString(w, `{"status":"ok"}`)
		}
	}))
	t.Cleanup(srv.Close)
	return srv
}

func TestGuardCountsOnlyCostlyCallsAndRefusesOverCap(t *testing.T) {
	var calls atomic.Int64
	up := fakePerception(t, &calls)
	u, _ := url.Parse(up.URL)
	b := &Budget{Limit: 2, Now: time.Now}
	g := httptest.NewServer(NewGuard(u, b, quietLog()))
	defer g.Close()

	for range 5 {
		resp, err := http.Get(g.URL + "/readyz")
		if err != nil || resp.StatusCode != 200 {
			t.Fatalf("readyz: %v %v", err, resp)
		}
		resp.Body.Close()
	}
	for _, p := range []string{"/v1/measure", "/v1/transcribe"} {
		resp, err := http.Post(g.URL+p, "multipart/form-data", strings.NewReader("x"))
		if err != nil || resp.StatusCode != 200 {
			t.Fatalf("%s: %v %v", p, err, resp)
		}
		resp.Body.Close()
	}
	resp, err := http.Post(g.URL+"/v1/measure/stream", "multipart/form-data", strings.NewReader("x"))
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusTooManyRequests {
		t.Fatalf("status %d, want 429", resp.StatusCode)
	}
	if resp.Header.Get("Retry-After") == "" {
		t.Fatal("no Retry-After")
	}
	var body struct {
		Detail string `json:"detail"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&body); err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(body.Detail, "paused") || !strings.Contains(body.Detail, "routes still work") {
		t.Fatalf("detail %q", body.Detail)
	}
	if strings.Contains(body.Detail, "\u2014") {
		t.Fatal("em dash in visitor text")
	}
	if got := calls.Load(); got != 7 { // 5 readyz + 2 allowed posts
		t.Fatalf("upstream saw %d calls, want 7", got)
	}

	q, err := http.Get(g.URL + "/quota")
	if err != nil {
		t.Fatal(err)
	}
	defer q.Body.Close()
	var quota struct{ Used, Limit int }
	_ = json.NewDecoder(q.Body).Decode(&quota)
	if quota.Used != 2 || quota.Limit != 2 {
		t.Fatalf("quota %+v", quota)
	}
}

func TestGuardStreamsEventsAsTheyArrive(t *testing.T) {
	var calls atomic.Int64
	up := fakePerception(t, &calls)
	u, _ := url.Parse(up.URL)
	g := httptest.NewServer(NewGuard(u, &Budget{Limit: 10, Now: time.Now}, quietLog()))
	defer g.Close()

	start := time.Now()
	resp, err := http.Post(g.URL+"/v1/measure/stream", "multipart/form-data", strings.NewReader("x"))
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	line, err := bufio.NewReader(resp.Body).ReadString('\n')
	if err != nil || line != "event: step\n" {
		t.Fatalf("first line %q, %v", line, err)
	}
	if d := time.Since(start); d > 250*time.Millisecond {
		t.Fatalf("first event took %v; the proxy is buffering", d)
	}
}

func TestGuardAnswersWhenPerceptionIsDown(t *testing.T) {
	u, _ := url.Parse("http://127.0.0.1:1")
	g := httptest.NewServer(NewGuard(u, &Budget{Limit: 10, Now: time.Now}, quietLog()))
	defer g.Close()
	resp, err := http.Post(g.URL+"/v1/measure", "multipart/form-data", strings.NewReader("x"))
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	// Never 404, 405 or 501: the gateway would retry on the other endpoint.
	if resp.StatusCode != http.StatusServiceUnavailable {
		t.Fatalf("status %d", resp.StatusCode)
	}
}

func TestChildEnvDropsSecretsAndOverrides(t *testing.T) {
	s := &Service{Env: map[string]string{"A": "new"}, Drop: []string{"BASETEN_API_KEY"}}
	got := childEnv([]string{"A=old", "BASETEN_API_KEY=secret", "PATH=/bin"}, s)
	joined := strings.Join(got, " ")
	if strings.Contains(joined, "secret") || strings.Contains(joined, "A=old") {
		t.Fatalf("env %v", got)
	}
	if !strings.Contains(joined, "A=new") || !strings.Contains(joined, "PATH=/bin") {
		t.Fatalf("env %v", got)
	}
}

func TestBackoffDoublesCapsAndResets(t *testing.T) {
	var b Backoff
	var got []time.Duration
	for range 7 {
		got = append(got, b.After(time.Second))
	}
	want := []time.Duration{1, 2, 4, 8, 16, 30, 30}
	for i, w := range want {
		if got[i] != w*time.Second {
			t.Fatalf("step %d: %v, want %v", i, got[i], w*time.Second)
		}
	}
	if d := b.After(2 * time.Minute); d != time.Second {
		t.Fatalf("after a long run: %v", d)
	}
}

func TestPrefixWriterTagsWholeLines(t *testing.T) {
	var out bytes.Buffer
	p := newPrefixWriter("x")
	p.out = &out
	for _, s := range []string{"hel", "lo\nwor", "ld\n", "tail"} {
		p.Write([]byte(s))
	}
	if out.String() != "[x] hello\n[x] world\n" {
		t.Fatalf("got %q", out.String())
	}
	p.Flush()
	if !strings.HasSuffix(out.String(), "[x] tail\n") {
		t.Fatalf("partial line not flushed: %q", out.String())
	}
}

func TestClientIPTrustsOnlyTheProxyHops(t *testing.T) {
	cases := []struct {
		xff  []string
		hops int
		want string
	}{
		{nil, 1, "10.1.1.1"},                                   // no header: the connection
		{[]string{"203.0.113.7"}, 1, "203.0.113.7"},            // proxy appended the visitor
		{[]string{"6.6.6.6, 203.0.113.7"}, 1, "203.0.113.7"},   // forged entry ignored
		{[]string{"6.6.6.6", "203.0.113.7"}, 1, "203.0.113.7"}, // split over two headers
		{[]string{"6.6.6.6, 203.0.113.7, 10.0.0.2"}, 2, "203.0.113.7"},
		{[]string{"203.0.113.7"}, 2, "10.1.1.1"}, // fewer entries than hops
		{[]string{"not-an-ip"}, 1, "10.1.1.1"},
		{[]string{"6.6.6.6, 203.0.113.7"}, 0, "10.1.1.1"}, // trust no header
	}
	for _, c := range cases {
		if got := ClientIP(c.xff, "10.1.1.1:5555", c.hops); got != c.want {
			t.Errorf("ClientIP(%q, hops %d) = %q, want %q", c.xff, c.hops, got, c.want)
		}
	}
}

func TestFrontRewritesForwardedFor(t *testing.T) {
	var seen atomic.Value
	gw := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		seen.Store(strings.Join(r.Header.Values("X-Forwarded-For"), "|"))
		w.Header().Set("Content-Type", "text/event-stream")
		io.WriteString(w, "event: hello\n\n")
		w.(http.Flusher).Flush()
		time.Sleep(300 * time.Millisecond)
	}))
	defer gw.Close()
	u, _ := url.Parse(gw.URL)
	front := httptest.NewServer(NewFront(u, 1, 0, quietLog()))
	defer front.Close()

	req, _ := http.NewRequest("GET", front.URL+"/api/stream", nil)
	req.Header.Set("X-Forwarded-For", "6.6.6.6, 203.0.113.7")
	start := time.Now()
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	line, _ := bufio.NewReader(resp.Body).ReadString('\n')
	if line != "event: hello\n" || time.Since(start) > 250*time.Millisecond {
		t.Fatalf("stream buffered: %q after %v", line, time.Since(start))
	}
	if got := seen.Load(); got != "203.0.113.7" {
		t.Fatalf("gateway saw X-Forwarded-For %q", got)
	}
}

func TestBudgetSurvivesRestartSameDayOnly(t *testing.T) {
	file := t.TempDir() + "/state/budget.json"
	now := time.Date(2026, 10, 28, 6, 0, 0, 0, time.UTC)
	clock := func() time.Time { return now }
	b := &Budget{Limit: 3, Now: clock, File: file}
	if err := b.Load(); err != nil {
		t.Fatal(err)
	}
	b.Take()
	b.Take()
	again := &Budget{Limit: 3, Now: clock, File: file}
	if err := again.Load(); err != nil {
		t.Fatal(err)
	}
	if _, used := again.Status(); used != 2 {
		t.Fatalf("restored %d, want 2", used)
	}
	if ok, _, _ := again.Take(); !ok {
		t.Fatal("third call refused")
	}
	if ok, _, _ := again.Take(); ok {
		t.Fatal("cap not enforced after restore")
	}
	now = now.Add(24 * time.Hour)
	next := &Budget{Limit: 3, Now: clock, File: file}
	_ = next.Load()
	if _, used := next.Status(); used != 0 {
		t.Fatalf("yesterday's count carried over: %d", used)
	}
}
