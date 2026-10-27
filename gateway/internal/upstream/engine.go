// Package upstream holds typed clients for the engine (CONTRACTS.md
// section 2) and perception (section 1). Every call has a timeout and is
// counted in the metrics.
package upstream

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
	"time"

	"neermattam/gateway/internal/metrics"
)

// Error is a non-2xx answer from an upstream service.
type Error struct {
	Service string
	Status  int
	Message string
}

func (e *Error) Error() string {
	return fmt.Sprintf("%s returned %d: %s", e.Service, e.Status, e.Message)
}

// IsStatus reports whether err is an upstream Error with this status.
func IsStatus(err error, status int) bool {
	var ue *Error
	return errors.As(err, &ue) && ue.Status == status
}

// ErrUnavailable wraps network failures and timeouts.
var ErrUnavailable = errors.New("service unavailable")

func observe(m *metrics.Set, service string, start time.Time, outcome string) {
	if m == nil {
		return
	}
	m.Upstream.Inc(service, outcome)
	m.UpstreamDuration.Observe(time.Since(start).Seconds(), service)
}

// readError turns an error body ({"error": "..."} or anything else) into an Error.
func readError(service string, resp *http.Response) error {
	body, _ := io.ReadAll(io.LimitReader(resp.Body, 4096))
	var e struct {
		Error  string `json:"error"`
		Detail any    `json:"detail"`
	}
	msg := strings.TrimSpace(string(body))
	if json.Unmarshal(body, &e) == nil {
		switch {
		case e.Error != "":
			msg = e.Error
		case e.Detail != nil:
			msg = fmt.Sprint(e.Detail)
		}
	}
	if msg == "" {
		msg = http.StatusText(resp.StatusCode)
	}
	return &Error{Service: service, Status: resp.StatusCode, Message: msg}
}

// Engine is a client for the Rust engine.
type Engine struct {
	base         string
	client       *http.Client
	timeout      time.Duration
	routeTimeout time.Duration
	metrics      *metrics.Set
}

// NewEngine returns an engine client. The http.Client has no overall timeout
// because the stream is long-lived; each call sets its own deadline.
func NewEngine(base string, timeout, routeTimeout time.Duration, m *metrics.Set) *Engine {
	return &Engine{
		base:         strings.TrimRight(base, "/"),
		client:       &http.Client{Transport: newTransport()},
		timeout:      timeout,
		routeTimeout: routeTimeout,
		metrics:      m,
	}
}

func newTransport() *http.Transport {
	t := http.DefaultTransport.(*http.Transport).Clone()
	t.MaxIdleConnsPerHost = 32
	t.ResponseHeaderTimeout = 0 // per-call contexts bound every request
	return t
}

// Reading is the body of POST /v1/readings.
type Reading struct {
	ReadingID        string    `json:"reading_id"`
	Lat              float64   `json:"lat"`
	Lon              float64   `json:"lon"`
	DepthCm          float64   `json:"depth_cm"`
	DepthSigmaCm     float64   `json:"depth_sigma_cm"`
	VelocityMps      *float64  `json:"velocity_mps,omitempty"`
	VelocitySigmaMps *float64  `json:"velocity_sigma_mps,omitempty"`
	ObservedAt       time.Time `json:"observed_at"`
	Source           string    `json:"source,omitempty"`
	Trust            float64   `json:"trust"`
}

// Hazard is the engine's hazard block.
type Hazard struct {
	Rating          float64 `json:"rating"`
	Class           string  `json:"class"`
	ClassUpper      string  `json:"class_upper"`
	DebrisFactor    float64 `json:"debris_factor"`
	VelocityMps     float64 `json:"velocity_mps"`
	VelocityAssumed bool    `json:"velocity_assumed"`
}

// ReadingResult is the engine's answer to a reading: the fused cell state.
type ReadingResult struct {
	ReadingID        string    `json:"reading_id"`
	Cell             string    `json:"cell"`
	DepthCm          float64   `json:"depth_cm"`
	DepthSigmaCm     float64   `json:"depth_sigma_cm"`
	DepthUpperCm     float64   `json:"depth_upper_cm"`
	VelocityMps      *float64  `json:"velocity_mps"`
	VelocitySigmaMps *float64  `json:"velocity_sigma_mps"`
	Unknown          bool      `json:"unknown"`
	NReadings        int       `json:"n_readings"`
	UpdatedAt        time.Time `json:"updated_at"`
	Hazard           Hazard    `json:"hazard"`
	Duplicate        bool      `json:"duplicate"`
}

// PostReading fuses one reading.
func (e *Engine) PostReading(ctx context.Context, r Reading) (*ReadingResult, error) {
	body, err := json.Marshal(r)
	if err != nil {
		return nil, err
	}
	raw, err := e.do(ctx, http.MethodPost, "/v1/readings", bytes.NewReader(body), e.timeout)
	if err != nil {
		return nil, err
	}
	var out ReadingResult
	if err := json.Unmarshal(raw, &out); err != nil {
		return nil, fmt.Errorf("engine reading response: %w", err)
	}
	return &out, nil
}

// Cells returns the raw GeoJSON FeatureCollection for a bbox.
func (e *Engine) Cells(ctx context.Context, bbox string) ([]byte, error) {
	return e.do(ctx, http.MethodGet, "/v1/cells?bbox="+url.QueryEscape(bbox), nil, e.timeout)
}

// CellProps are the properties of one engine cell feature.
type CellProps struct {
	Cell         string    `json:"cell"`
	DepthCm      float64   `json:"depth_cm"`
	DepthSigmaCm float64   `json:"depth_sigma_cm"`
	Unknown      bool      `json:"unknown"`
	NReadings    int       `json:"n_readings"`
	HazardClass  string    `json:"hazard_class"`
	UpdatedAt    time.Time `json:"updated_at"`
	Origin       string    `json:"origin,omitempty"`
}

// CellFeature is a decoded cell with its centroid.
type CellFeature struct {
	ID         string
	Props      CellProps
	Lat, Lon   float64
	RawPolygon [][][2]float64
}

type rawCell struct {
	ID       string `json:"id"`
	Geometry struct {
		Coordinates [][][2]float64 `json:"coordinates"`
	} `json:"geometry"`
	Properties CellProps `json:"properties"`
}

// DecodeCells parses a cells FeatureCollection into typed features with
// centroids (the mean of the ring's distinct vertices).
func DecodeCells(raw []byte) ([]CellFeature, error) {
	var fc struct {
		Features []rawCell `json:"features"`
	}
	if err := json.Unmarshal(raw, &fc); err != nil {
		return nil, err
	}
	out := make([]CellFeature, 0, len(fc.Features))
	for _, f := range fc.Features {
		cf := CellFeature{ID: f.ID, Props: f.Properties, RawPolygon: f.Geometry.Coordinates}
		if cf.ID == "" {
			cf.ID = f.Properties.Cell
		}
		cf.Lat, cf.Lon = Centroid(f.Geometry.Coordinates)
		out = append(out, cf)
	}
	return out, nil
}

// Centroid averages the distinct vertices of the outer ring.
func Centroid(rings [][][2]float64) (lat, lon float64) {
	if len(rings) == 0 || len(rings[0]) == 0 {
		return 0, 0
	}
	ring := rings[0]
	n := len(ring)
	if n > 1 && ring[0] == ring[n-1] {
		n--
	}
	for _, p := range ring[:n] {
		lon += p[0]
		lat += p[1]
	}
	return lat / float64(n), lon / float64(n)
}

// Route returns the raw route Feature (with its `baseline` member).
func (e *Engine) Route(ctx context.Context, from, to, vehicle string) ([]byte, error) {
	q := url.Values{"from": {from}, "to": {to}, "vehicle": {vehicle}}
	return e.do(ctx, http.MethodGet, "/v1/route?"+q.Encode(), nil, e.routeTimeout)
}

// Ready checks the engine's /readyz.
func (e *Engine) Ready(ctx context.Context) error {
	_, err := e.do(ctx, http.MethodGet, "/readyz", nil, 2*time.Second)
	return err
}

func (e *Engine) do(ctx context.Context, method, path string, body io.Reader, timeout time.Duration) ([]byte, error) {
	ctx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	start := time.Now()
	req, err := http.NewRequestWithContext(ctx, method, e.base+path, body)
	if err != nil {
		return nil, err
	}
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	req.Header.Set("Accept", "application/json")
	resp, err := e.client.Do(req)
	if err != nil {
		observe(e.metrics, "engine", start, "unreachable")
		return nil, fmt.Errorf("engine: %w: %v", ErrUnavailable, err)
	}
	defer resp.Body.Close()
	if resp.StatusCode/100 != 2 {
		observe(e.metrics, "engine", start, fmt.Sprintf("http_%d", resp.StatusCode))
		return nil, readError("engine", resp)
	}
	raw, err := io.ReadAll(io.LimitReader(resp.Body, 64<<20))
	if err != nil {
		observe(e.metrics, "engine", start, "read_error")
		return nil, fmt.Errorf("engine: %w: %v", ErrUnavailable, err)
	}
	observe(e.metrics, "engine", start, "ok")
	return raw, nil
}

// Event is one server-sent event from the engine stream.
type Event struct {
	Name    string
	Data    string
	Comment string
}

// Stream connects to /v1/stream and calls fn for each event until the
// stream ends or ctx is cancelled. Only the connection has a timeout.
func (e *Engine) Stream(ctx context.Context, fn func(Event)) error {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, e.base+"/v1/stream", nil)
	if err != nil {
		return err
	}
	req.Header.Set("Accept", "text/event-stream")
	connectCtx, cancel := context.WithTimeout(ctx, e.timeout)
	defer cancel()
	type result struct {
		resp *http.Response
		err  error
	}
	ch := make(chan result, 1)
	go func() {
		resp, err := e.client.Do(req)
		ch <- result{resp, err}
	}()
	var resp *http.Response
	select {
	case r := <-ch:
		if r.err != nil {
			return fmt.Errorf("engine stream: %w: %v", ErrUnavailable, r.err)
		}
		resp = r.resp
	case <-connectCtx.Done():
		go func() {
			if r := <-ch; r.resp != nil {
				r.resp.Body.Close()
			}
		}()
		return fmt.Errorf("engine stream: %w: connect timeout", ErrUnavailable)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return readError("engine", resp)
	}
	return ParseSSE(resp.Body, fn)
}

// ParseSSE reads text/event-stream framing and calls fn per event.
func ParseSSE(r io.Reader, fn func(Event)) error {
	sc := bufio.NewScanner(r)
	sc.Buffer(make([]byte, 64<<10), 4<<20)
	var ev Event
	var data []string
	flush := func() {
		if len(data) > 0 || ev.Name != "" {
			ev.Data = strings.Join(data, "\n")
			if ev.Name == "" {
				ev.Name = "message"
			}
			fn(ev)
		}
		ev = Event{}
		data = data[:0]
	}
	for sc.Scan() {
		line := sc.Text()
		switch {
		case line == "":
			flush()
		case strings.HasPrefix(line, ":"):
			c := strings.TrimSpace(line[1:])
			if c != "" {
				fn(Event{Comment: c})
			}
		default:
			field, value, _ := strings.Cut(line, ":")
			value = strings.TrimPrefix(value, " ")
			switch field {
			case "event":
				ev.Name = value
			case "data":
				data = append(data, value)
			}
		}
	}
	flush()
	if err := sc.Err(); err != nil {
		return err
	}
	return io.EOF
}
