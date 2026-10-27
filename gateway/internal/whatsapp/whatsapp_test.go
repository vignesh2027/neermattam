package whatsapp

import (
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"neermattam/gateway/internal/config"
	"neermattam/gateway/internal/report"
	"neermattam/gateway/internal/upstream"
	"neermattam/gateway/internal/verify"
)

const secret = "app-secret"

func sign(body []byte) string {
	m := hmac.New(sha256.New, []byte(secret))
	m.Write(body)
	return "sha256=" + hex.EncodeToString(m.Sum(nil))
}

func TestValidSignature(t *testing.T) {
	body := []byte(`{"object":"whatsapp_business_account"}`)
	if !ValidSignature(secret, body, sign(body)) {
		t.Fatal("good signature rejected")
	}
	bad := []string{
		"",
		"sha256=",
		"sha256=zz",
		sign([]byte("other body")),
		strings.Replace(sign(body), "sha256=", "sha1=", 1),
		sign(body)[:len(sign(body))-2],
	}
	for _, h := range bad {
		if ValidSignature(secret, body, h) {
			t.Errorf("accepted %q", h)
		}
	}
	if ValidSignature("wrong-secret", body, sign(body)) {
		t.Error("accepted a signature made with another secret")
	}
}

type fakeRunner struct {
	mu   sync.Mutex
	subs []report.Submission
}

func (f *fakeRunner) Run(_ context.Context, s report.Submission, _ report.Progress) (*report.Outcome, error) {
	f.mu.Lock()
	f.subs = append(f.subs, s)
	f.mu.Unlock()
	d, sg := 34.0, 4.0
	return &report.Outcome{
		Status: report.StatusFused, Place: "near Velachery", Lat: s.Lat, Lon: s.Lon, DepthCm: &d, DepthSigmaCm: &sg,
		Measurement:  &upstream.Measurement{Confidence: 0.82, Reference: &upstream.Reference{Kind: "two_wheeler_wheel"}},
		Verification: &verify.Result{Accepted: true, Trust: 0.9},
		Cell:         &upstream.ReadingResult{DepthCm: 31, Hazard: upstream.Hazard{Class: "danger_some"}},
		Advisory:     report.Advisory,
	}, nil
}

type graph struct {
	srv     *httptest.Server
	mu      sync.Mutex
	replies []string
	auth    []string
}

func newGraph(t *testing.T) *graph {
	g := &graph{}
	g.srv = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		g.mu.Lock()
		g.auth = append(g.auth, r.Header.Get("Authorization"))
		g.mu.Unlock()
		switch {
		case r.Method == http.MethodGet && r.URL.Path == "/media-1":
			json.NewEncoder(w).Encode(map[string]any{"url": g.srv.URL + "/files/media-1", "mime_type": "image/jpeg", "file_size": 4})
		case r.Method == http.MethodGet && r.URL.Path == "/files/media-1":
			w.Write([]byte("\xff\xd8\xff\xe0"))
		case r.Method == http.MethodPost && strings.HasSuffix(r.URL.Path, "/messages"):
			var m struct {
				To   string `json:"to"`
				Text struct {
					Body string `json:"body"`
				} `json:"text"`
			}
			json.NewDecoder(r.Body).Decode(&m)
			g.mu.Lock()
			g.replies = append(g.replies, m.Text.Body)
			g.mu.Unlock()
			w.Write([]byte(`{"messages":[{"id":"wamid.x"}]}`))
		default:
			http.NotFound(w, r)
		}
	}))
	t.Cleanup(g.srv.Close)
	return g
}

func newHandler(t *testing.T, g *graph, run Runner) *Handler {
	cfg := config.WhatsApp{VerifyToken: "verify-me", AppSecret: secret, AccessToken: "token-1", PhoneNumberID: "555", GraphURL: g.srv.URL}
	log := slog.New(slog.NewTextHandler(io.Discard, nil))
	return New(context.Background(), cfg, "https://neermattam.example", map[string]int64{"image": 1 << 20}, run, log, nil)
}

func deliver(t *testing.T, h *Handler, body string, sig string) int {
	t.Helper()
	req := httptest.NewRequest(http.MethodPost, "/webhooks/whatsapp", strings.NewReader(body))
	req.Header.Set("X-Hub-Signature-256", sig)
	rec := httptest.NewRecorder()
	h.Receive(rec, req)
	return rec.Code
}

func payload(msg string) string {
	return `{"object":"whatsapp_business_account","entry":[{"changes":[{"field":"messages","value":{"metadata":{"phone_number_id":"555"},"messages":[` + msg + `]}}]}]}`
}

func TestVerifyHandshake(t *testing.T) {
	h := newHandler(t, newGraph(t), &fakeRunner{})
	rec := httptest.NewRecorder()
	h.Verify(rec, httptest.NewRequest("GET", "/webhooks/whatsapp?hub.mode=subscribe&hub.verify_token=verify-me&hub.challenge=1158201444", nil))
	if rec.Code != 200 || rec.Body.String() != "1158201444" {
		t.Fatalf("got %d %q", rec.Code, rec.Body.String())
	}
	rec = httptest.NewRecorder()
	h.Verify(rec, httptest.NewRequest("GET", "/webhooks/whatsapp?hub.mode=subscribe&hub.verify_token=nope&hub.challenge=1", nil))
	if rec.Code != http.StatusForbidden {
		t.Fatalf("wrong token: %d", rec.Code)
	}
}

func TestBadSignatureIsRejectedBeforeParsing(t *testing.T) {
	run := &fakeRunner{}
	h := newHandler(t, newGraph(t), run)
	body := payload(`{"from":"9190000","id":"m1","type":"location","location":{"latitude":12.98,"longitude":80.22}}`)
	if code := deliver(t, h, body, sign([]byte(body+" "))); code != http.StatusUnauthorized {
		t.Fatalf("tampered body: %d", code)
	}
	if code := deliver(t, h, body, ""); code != http.StatusUnauthorized {
		t.Fatalf("no signature: %d", code)
	}
	h.Wait()
	if len(run.subs) != 0 {
		t.Fatal("a report ran from an unsigned delivery")
	}
}

func TestPhotoThenLocationRunsOneReportAndReplies(t *testing.T) {
	g := newGraph(t)
	run := &fakeRunner{}
	h := newHandler(t, g, run)
	img := payload(`{"from":"9190000","id":"m1","type":"image","image":{"id":"media-1","mime_type":"image/jpeg"}}`)
	loc := payload(`{"from":"9190000","id":"m2","type":"location","location":{"latitude":12.9791,"longitude":80.2209}}`)
	if code := deliver(t, h, img, sign([]byte(img))); code != 200 {
		t.Fatalf("image: %d", code)
	}
	// Meta redelivers; the duplicate must be ignored.
	deliver(t, h, img, sign([]byte(img)))
	if code := deliver(t, h, loc, sign([]byte(loc))); code != 200 {
		t.Fatalf("location: %d", code)
	}
	h.Wait()
	if len(run.subs) != 1 {
		t.Fatalf("%d reports ran, want 1", len(run.subs))
	}
	s := run.subs[0]
	if s.Channel != "whatsapp" || s.Lat != 12.9791 || s.Lon != 80.2209 || string(s.Files[0].Data) != "\xff\xd8\xff\xe0" {
		t.Fatalf("submission %+v", s)
	}
	g.mu.Lock()
	defer g.mu.Unlock()
	if len(g.replies) != 2 {
		t.Fatalf("replies %q", g.replies)
	}
	last := g.replies[1]
	for _, want := range []string{"34 cm", "plus or minus 4 cm", "two wheeler wheel", "Advisory only", "?at=12.97910,80.22090"} {
		if !strings.Contains(last, want) {
			t.Errorf("reply %q lacks %q", last, want)
		}
	}
	if strings.ContainsAny(last, "—→") {
		t.Errorf("reply has an em dash or arrow: %q", last)
	}
	for _, a := range g.auth {
		if a != "Bearer token-1" {
			t.Fatalf("graph call without the token: %q", a)
		}
	}
}

func TestStaleMediaIsNotPaired(t *testing.T) {
	g := newGraph(t)
	run := &fakeRunner{}
	h := newHandler(t, g, run)
	clock := time.Now()
	h.now = func() time.Time { return clock }
	img := payload(`{"from":"9190001","id":"a1","type":"image","image":{"id":"media-1"}}`)
	deliver(t, h, img, sign([]byte(img)))
	clock = clock.Add(PairWindow + time.Minute)
	loc := payload(`{"from":"9190001","id":"a2","type":"location","location":{"latitude":12.98,"longitude":80.22}}`)
	deliver(t, h, loc, sign([]byte(loc)))
	h.Wait()
	if len(run.subs) != 0 {
		t.Fatal("a photo older than the pairing window was used")
	}
}
