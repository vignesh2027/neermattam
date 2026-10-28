// Package whatsapp receives reports through the WhatsApp Cloud API. A
// reporter sends a photo, video or voice note and shares a location; the two
// messages are paired per sender within ten minutes, run through the same
// report pipeline as the web form, and answered with the measured depth.
package whatsapp

import (
	"bytes"
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"strings"
	"sync"
	"time"

	"neermattam/gateway/internal/config"
	"neermattam/gateway/internal/metrics"
	"neermattam/gateway/internal/report"
	"neermattam/gateway/internal/upstream"
)

// PairWindow is how long a photo waits for its location, or the reverse.
const PairWindow = 10 * time.Minute

// Runner is the part of the report pipeline the webhook uses.
type Runner interface {
	Run(ctx context.Context, sub report.Submission, progress report.Progress) (*report.Outcome, error)
}

// Handler serves GET and POST /webhooks/whatsapp.
type Handler struct {
	cfg       config.WhatsApp
	publicURL string
	limits    map[string]int64
	pipeline  Runner
	client    *http.Client
	log       *slog.Logger
	metrics   *metrics.Set
	now       func() time.Time
	base      context.Context

	mu      sync.Mutex
	pending map[string]*pending
	seen    map[string]time.Time
	wg      sync.WaitGroup
	sem     chan struct{}
}

type mediaRef struct {
	ID       string `json:"id"`
	MimeType string `json:"mime_type"`
	Caption  string `json:"caption"`
}

type location struct {
	Latitude  float64 `json:"latitude"`
	Longitude float64 `json:"longitude"`
	Name      string  `json:"name"`
	Address   string  `json:"address"`
}

type message struct {
	From      string    `json:"from"`
	ID        string    `json:"id"`
	Timestamp string    `json:"timestamp"`
	Type      string    `json:"type"`
	Image     *mediaRef `json:"image"`
	Video     *mediaRef `json:"video"`
	Audio     *mediaRef `json:"audio"`
	Location  *location `json:"location"`
	Text      *struct {
		Body string `json:"body"`
	} `json:"text"`
}

// Payload is the webhook body (the fields Neermattam uses).
type Payload struct {
	Object string `json:"object"`
	Entry  []struct {
		Changes []struct {
			Field string `json:"field"`
			Value struct {
				Metadata struct {
					PhoneNumberID string `json:"phone_number_id"`
				} `json:"metadata"`
				Messages []message `json:"messages"`
			} `json:"value"`
		} `json:"changes"`
	} `json:"entry"`
}

type pending struct {
	media   *mediaRef
	kind    string
	mediaAt time.Time
	loc     *location
	locAt   time.Time
	phoneID string
}

// New returns a handler. base bounds the background work started by
// webhook calls, so shutdown can cancel it.
func New(base context.Context, cfg config.WhatsApp, publicURL string, limits map[string]int64, p Runner, log *slog.Logger, m *metrics.Set) *Handler {
	return &Handler{
		cfg: cfg, publicURL: publicURL, limits: limits, pipeline: p, log: log, metrics: m,
		client: &http.Client{Timeout: 30 * time.Second}, now: time.Now, base: base,
		pending: map[string]*pending{}, seen: map[string]time.Time{}, sem: make(chan struct{}, 4),
	}
}

// Wait blocks until background work has finished.
func (h *Handler) Wait() { h.wg.Wait() }

// Verify answers Meta's subscription handshake.
func (h *Handler) Verify(w http.ResponseWriter, r *http.Request) {
	q := r.URL.Query()
	token := q.Get("hub.verify_token")
	if q.Get("hub.mode") != "subscribe" || subtle.ConstantTimeCompare([]byte(token), []byte(h.cfg.VerifyToken)) != 1 {
		http.Error(w, "verification failed", http.StatusForbidden)
		return
	}
	w.Header().Set("Content-Type", "text/plain; charset=utf-8")
	w.Header().Set("X-Content-Type-Options", "nosniff")
	_, _ = io.WriteString(w, q.Get("hub.challenge"))
}

// ValidSignature checks X-Hub-Signature-256 ("sha256=<hex>") against an
// HMAC-SHA256 of the raw body, in constant time.
func ValidSignature(secret string, body []byte, header string) bool {
	hexSig, ok := strings.CutPrefix(header, "sha256=")
	if !ok {
		return false
	}
	got, err := hex.DecodeString(hexSig)
	if err != nil || len(got) != sha256.Size {
		return false
	}
	mac := hmac.New(sha256.New, []byte(secret))
	mac.Write(body)
	return hmac.Equal(got, mac.Sum(nil))
}

// Receive handles a webhook delivery. It answers 200 quickly and processes
// messages in the background, as Meta retries slow deliveries.
func (h *Handler) Receive(w http.ResponseWriter, r *http.Request) {
	body, err := io.ReadAll(http.MaxBytesReader(w, r.Body, 1<<20))
	if err != nil {
		http.Error(w, "body too large", http.StatusRequestEntityTooLarge)
		return
	}
	if !ValidSignature(h.cfg.AppSecret, body, r.Header.Get("X-Hub-Signature-256")) {
		h.log.Warn("whatsapp webhook signature rejected")
		h.count("bad_signature")
		http.Error(w, "invalid signature", http.StatusUnauthorized)
		return
	}
	var p Payload
	if err := json.Unmarshal(body, &p); err != nil {
		http.Error(w, "invalid JSON", http.StatusBadRequest)
		return
	}
	for _, e := range p.Entry {
		for _, c := range e.Changes {
			for _, m := range c.Value.Messages {
				if h.firstSighting(m.ID) {
					h.count(m.Type)
					h.handle(m, c.Value.Metadata.PhoneNumberID)
				}
			}
		}
	}
	w.WriteHeader(http.StatusOK)
}

func (h *Handler) count(t string) {
	if h.metrics != nil {
		h.metrics.WhatsApp.Inc(t)
	}
}

// firstSighting drops redelivered messages.
func (h *Handler) firstSighting(id string) bool {
	if id == "" {
		return true
	}
	now := h.now()
	h.mu.Lock()
	defer h.mu.Unlock()
	for k, t := range h.seen {
		if now.Sub(t) > time.Hour {
			delete(h.seen, k)
		}
	}
	if _, dup := h.seen[id]; dup {
		return false
	}
	h.seen[id] = now
	return true
}

// handle pairs media with a location and starts processing when both exist.
func (h *Handler) handle(m message, phoneID string) {
	now := h.now()
	var ref *mediaRef
	switch m.Type {
	case "image":
		ref = m.Image
	case "video":
		ref = m.Video
	case "audio":
		ref = m.Audio
	case "location":
		if m.Location == nil {
			return
		}
	default:
		h.replyAsync(m.From, phoneID, "Send a photo or short video of the flooded street with a wheel in view, then share your location. Neermattam will measure the depth.")
		return
	}
	h.mu.Lock()
	for k, pe := range h.pending {
		if now.Sub(pe.mediaAt) > PairWindow && now.Sub(pe.locAt) > PairWindow {
			delete(h.pending, k)
		}
	}
	pe := h.pending[m.From]
	if pe == nil {
		pe = &pending{}
		h.pending[m.From] = pe
	}
	pe.phoneID = phoneID
	if ref != nil {
		if ref.ID == "" {
			h.mu.Unlock()
			return
		}
		pe.media, pe.kind, pe.mediaAt = ref, m.Type, now
	} else {
		pe.loc, pe.locAt = m.Location, now
	}
	ready := pe.media != nil && pe.loc != nil && now.Sub(pe.mediaAt) <= PairWindow && now.Sub(pe.locAt) <= PairWindow
	var job pending
	if ready {
		job = *pe
		delete(h.pending, m.From)
	}
	h.mu.Unlock()

	switch {
	case ready:
		h.wg.Add(1)
		go func() {
			defer h.wg.Done()
			h.process(m.From, job)
		}()
	case ref != nil:
		h.replyAsync(m.From, phoneID, "Got it. Now share your location (attach, then Location) within 10 minutes so the reading lands on the right street.")
	default:
		h.replyAsync(m.From, phoneID, "Location saved. Now send a photo or short video of the water with a wheel in view, within 10 minutes.")
	}
}

func (h *Handler) process(from string, job pending) {
	select {
	case h.sem <- struct{}{}:
		defer func() { <-h.sem }()
	case <-h.base.Done():
		return
	}
	ctx, cancel := context.WithTimeout(h.base, 3*time.Minute)
	defer cancel()
	data, err := h.download(ctx, job.media.ID, h.limits[job.kind])
	if err != nil {
		h.log.Warn("whatsapp media download failed", "error", err.Error())
		h.reply(from, job.phoneID, "Sorry, the file could not be downloaded from WhatsApp. Please send it again.")
		return
	}
	sub := report.Submission{
		Channel: "whatsapp",
		Files:   []report.File{{Field: job.kind, Filename: job.kind, Data: data}},
		Lat:     job.loc.Latitude, Lon: job.loc.Longitude, HasLocation: true,
	}
	out, err := h.pipeline.Run(ctx, sub, nil)
	if err != nil {
		var re *report.Error
		if errors.As(err, &re) {
			h.reply(from, job.phoneID, re.Message)
		} else {
			h.reply(from, job.phoneID, "Sorry, something went wrong while measuring. Please try again shortly.")
		}
		return
	}
	h.reply(from, job.phoneID, ReplyText(out, h.publicURL))
}

// ReplyText formats a result for WhatsApp. Plain sentences, no symbols.
func ReplyText(o *report.Outcome, publicURL string) string {
	var b strings.Builder
	switch o.Status {
	case report.StatusUnusable:
		b.WriteString("We could not measure this one.")
		if o.Measurement != nil && len(o.Measurement.Reasons) > 0 {
			b.WriteString(" Reason: " + strings.Join(o.Measurement.Reasons, "; ") + ".")
		} else if o.Summary != "" {
			b.WriteString(" " + o.Summary)
		}
		b.WriteString(" Tip: include a whole scooter or car wheel, side on, where it meets the water.")
	case report.StatusRejected:
		fmt.Fprintf(&b, "Measured about %.0f cm (plus or minus %.0f cm), but it was not added to the map:", deref(o.DepthCm), deref(o.DepthSigmaCm))
		for _, r := range o.Verification.Rules {
			if r.Outcome == "reject" {
				b.WriteString(" " + r.Reason + ".")
			}
		}
	default:
		switch {
		case o.Source == "voice":
			fmt.Fprintf(&b, "From your voice note, about %.0f cm of water %s (plus or minus %.0f cm, a rough spoken estimate)", deref(o.DepthCm), o.Place, deref(o.DepthSigmaCm))
		case o.Measurement.MethodIs(upstream.MethodModelEstimate):
			fmt.Fprintf(&b, "No wheel could be measured, so this is the scene model's visual estimate: about %.0f cm of water %s (plus or minus %.0f cm)", deref(o.DepthCm), o.Place, deref(o.DepthSigmaCm))
		default:
			fmt.Fprintf(&b, "Neermattam measured about %.0f cm of water %s (plus or minus %.0f cm)", deref(o.DepthCm), o.Place, deref(o.DepthSigmaCm))
		}
		if o.Measurement.MethodIs(upstream.MethodModelEstimate) {
			// No ruler to name.
		} else if o.Measurement != nil && o.Measurement.Reference != nil {
			fmt.Fprintf(&b, ", using a %s as the ruler", strings.ReplaceAll(o.Measurement.Reference.Kind, "_", " "))
			fmt.Fprintf(&b, ". Confidence %.2f", o.Measurement.Confidence)
		}
		b.WriteString(".")
		if o.Cell != nil {
			fmt.Fprintf(&b, " The map cell there now reads %.0f cm, hazard: %s.", o.Cell.DepthCm, HazardWords(o.Cell.Hazard.Class))
		}
	}
	b.WriteString(" " + report.Advisory)
	if publicURL != "" && o.Status == report.StatusFused {
		fmt.Fprintf(&b, " Live map: %s/map?at=%.5f,%.5f", publicURL, o.Lat, o.Lon)
	}
	return b.String()
}

// HazardWords explains a hazard class in plain words.
func HazardWords(class string) string {
	switch class {
	case "low":
		return "caution"
	case "danger_some":
		return "danger for some, including children and the elderly"
	case "danger_most":
		return "danger for most people"
	case "danger_all":
		return "danger for everyone, including the emergency services"
	default:
		return "unknown"
	}
}

func deref(p *float64) float64 {
	if p == nil {
		return 0
	}
	return *p
}

// download resolves a media id to a URL and fetches the bytes.
func (h *Handler) download(ctx context.Context, id string, limit int64) ([]byte, error) {
	var meta struct {
		URL      string `json:"url"`
		MimeType string `json:"mime_type"`
		FileSize int64  `json:"file_size"`
	}
	raw, err := h.graph(ctx, http.MethodGet, h.cfg.GraphURL+"/"+id, nil, 64<<10)
	if err != nil {
		return nil, err
	}
	if err := json.Unmarshal(raw, &meta); err != nil || meta.URL == "" {
		return nil, fmt.Errorf("media lookup returned no url")
	}
	if limit > 0 && meta.FileSize > limit {
		return nil, fmt.Errorf("media is %d bytes, over the %d limit", meta.FileSize, limit)
	}
	return h.graph(ctx, http.MethodGet, meta.URL, nil, limit)
}

func (h *Handler) graph(ctx context.Context, method, url string, body []byte, limit int64) ([]byte, error) {
	start := time.Now()
	var rd io.Reader
	if body != nil {
		rd = bytes.NewReader(body)
	}
	req, err := http.NewRequestWithContext(ctx, method, url, rd)
	if err != nil {
		return nil, err
	}
	req.Header.Set("Authorization", "Bearer "+h.cfg.AccessToken)
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	resp, err := h.client.Do(req)
	if err != nil {
		h.observe(start, "unreachable")
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode/100 != 2 {
		h.observe(start, fmt.Sprintf("http_%d", resp.StatusCode))
		return nil, fmt.Errorf("graph api returned %d", resp.StatusCode)
	}
	if limit <= 0 {
		limit = 64 << 20
	}
	data, err := io.ReadAll(io.LimitReader(resp.Body, limit+1))
	if err != nil {
		h.observe(start, "read_error")
		return nil, err
	}
	if int64(len(data)) > limit {
		h.observe(start, "too_large")
		return nil, fmt.Errorf("response over %d bytes", limit)
	}
	h.observe(start, "ok")
	return data, nil
}

func (h *Handler) observe(start time.Time, outcome string) {
	if h.metrics == nil {
		return
	}
	h.metrics.Upstream.Inc("graph_api", outcome)
	h.metrics.UpstreamDuration.Observe(time.Since(start).Seconds(), "graph_api")
}

// replyAsync sends a reply without holding up the webhook response.
func (h *Handler) replyAsync(to, phoneID, text string) {
	h.wg.Add(1)
	go func() {
		defer h.wg.Done()
		h.reply(to, phoneID, text)
	}()
}

// reply sends a text message. Failures are logged, never retried in a loop.
func (h *Handler) reply(to, phoneID, text string) {
	if phoneID == "" {
		phoneID = h.cfg.PhoneNumberID
	}
	if phoneID == "" || to == "" {
		h.log.Warn("whatsapp reply skipped: no phone number id")
		return
	}
	body, _ := json.Marshal(map[string]any{
		"messaging_product": "whatsapp",
		"to":                to,
		"type":              "text",
		"text":              map[string]any{"body": text, "preview_url": false},
	})
	ctx, cancel := context.WithTimeout(h.base, 15*time.Second)
	defer cancel()
	if _, err := h.graph(ctx, http.MethodPost, h.cfg.GraphURL+"/"+phoneID+"/messages", body, 64<<10); err != nil {
		h.log.Warn("whatsapp reply failed", "error", err.Error())
	}
}
