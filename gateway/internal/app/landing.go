package app

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"strings"
	"sync"
	"time"

	"neermattam/gateway/internal/report"
	"neermattam/gateway/internal/samples"
	"neermattam/gateway/internal/web"
)

// The landing page's data beyond the preview map: the recorded showcase
// run, the sample photos, and the live numbers for every city from
// GET /api/india.

var (
	showcaseOnce sync.Once
	showcase     *web.Showcase
)

func loadShowcase() *web.Showcase {
	showcaseOnce.Do(func() {
		s, err := web.LoadShowcase()
		if err == nil {
			showcase = s
		}
	})
	return showcase
}

// indiaCache keeps the last answer of GET /api/india for a short while, so
// the landing page asks for it at most twice a minute.
type indiaCache struct {
	mu   sync.Mutex
	at   time.Time
	data *web.India
	err  error
}

const indiaTTL = 30 * time.Second

// india reads GET /api/india from this gateway's own routes, in process.
// Any status but 200, or a body that is not the contract's JSON, counts as
// no data: the section then shows every city without live numbers.
func (a *App) india(ctx context.Context) (*web.India, error) {
	c := &a.indiaMem
	c.mu.Lock()
	defer c.mu.Unlock()
	if time.Since(c.at) < indiaTTL {
		return c.data, c.err
	}
	data, err := a.fetchIndia(ctx)
	c.at, c.data, c.err = time.Now(), data, err
	return data, err
}

type indiaRecorder struct {
	h      http.Header
	status int
	buf    bytes.Buffer
}

func (r *indiaRecorder) Header() http.Header { return r.h }
func (r *indiaRecorder) WriteHeader(s int) {
	if r.status == 0 {
		r.status = s
	}
}
func (r *indiaRecorder) Write(b []byte) (int, error) {
	if r.status == 0 {
		r.status = http.StatusOK
	}
	return r.buf.Write(b)
}

func (a *App) fetchIndia(ctx context.Context) (*web.India, error) {
	if a.mux == nil {
		return nil, errNoIndia
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, "/api/india", nil)
	if err != nil {
		return nil, err
	}
	req.RemoteAddr = "127.0.0.1:0"
	req.Header.Set("Accept", "application/json")
	// Only the real route may answer. If /api/india is not registered the
	// mux falls through to "/", the landing page itself, which would wait on
	// this cache's lock forever.
	if _, pattern := a.mux.Handler(req); !strings.HasSuffix(pattern, "/api/india") {
		return nil, errNoIndia
	}
	rec := &indiaRecorder{h: http.Header{}}
	a.mux.ServeHTTP(rec, req)
	if rec.status != http.StatusOK || rec.h.Get("Content-Encoding") != "" {
		return nil, errNoIndia
	}
	var d web.India
	if err := json.Unmarshal(rec.buf.Bytes(), &d); err != nil || len(d.Cities) == 0 {
		return nil, errNoIndia
	}
	return &d, nil
}

type indiaError struct{}

func (indiaError) Error() string { return "GET /api/india gave no data" }

var errNoIndia error = indiaError{}

// indiaView builds the section for a page.
func (a *App) indiaView(ctx context.Context, lang, current string) *web.IndiaView {
	var refs []web.IndiaCityRef
	for _, c := range a.Cities.All() {
		refs = append(refs, web.IndiaCityRef{ID: c.ID, Routing: c.Routing, Simulated: a.simulatedIn(c.ID), Cells: c.Cells})
	}
	ctx, cancel := context.WithTimeout(ctx, 900*time.Millisecond)
	defer cancel()
	data, err := a.india(ctx)
	if err != nil {
		data = nil
	}
	return web.NewIndiaView(lang, refs, current, data, time.Now())
}

// applySample replaces a submission's file and place with one of the
// gateway's sample photos. The sample is measured live like any photo, but
// the pipeline never adds it to the map.
func applySample(sub *report.Submission, id string) *report.Error {
	s, ok := samples.Get(id)
	if !ok {
		return &report.Error{Status: http.StatusBadRequest, Code: "unknown_sample", Key: "unknown_sample", Message: "There is no sample photo with that name."}
	}
	b, err := s.Photo()
	if err != nil {
		return &report.Error{Status: http.StatusInternalServerError, Code: "unknown_sample", Key: "unknown_sample", Message: "There is no sample photo with that name."}
	}
	sub.Files = []report.File{{Field: "image", Filename: s.ID + ".jpg", Data: b}}
	sub.Lat, sub.Lon, sub.HasLocation = s.Lat, s.Lon, true
	sub.City = s.City
	sub.HintReference, sub.HintDiameterCm = "", nil
	sub.Sample = &report.SampleInfo{ID: s.ID, Place: s.Place, Taken: s.Taken}
	return nil
}
