package upstream

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"
)

// ErrStreamUnsupported means perception has no /v1/measure/stream.
var ErrStreamUnsupported = errors.New("perception has no measure stream")

// MeasureStream calls POST /v1/measure/stream and reports each agent step
// as it arrives, then returns the final measurement. The stream is
// server-sent events:
//
//	event: step     data: one agent_trace entry
//	event: result   data: the full /v1/measure response
//	event: error    data: {"detail": "..."} (optionally with "status")
//
// Unnamed events are recognised by shape. When perception answers 404, 405
// or 501 the caller should fall back to Measure; that answer is remembered
// for five minutes.
func (p *Perception) MeasureStream(ctx context.Context, req MeasureRequest, onStep func(AgentStep)) (*Measurement, error) {
	if time.Now().Unix() < p.noStreamUntil.Load() {
		return nil, ErrStreamUnsupported
	}
	body, ct, err := measureBody(req)
	if err != nil {
		return nil, err
	}
	ctx, cancel := context.WithTimeout(ctx, p.timeout)
	defer cancel()
	start := time.Now()
	hreq, err := http.NewRequestWithContext(ctx, http.MethodPost, p.base+"/v1/measure/stream", body)
	if err != nil {
		return nil, err
	}
	hreq.Header.Set("Content-Type", ct)
	hreq.Header.Set("Accept", "text/event-stream")
	resp, err := p.client.Do(hreq)
	if err != nil {
		observe(p.metrics, "perception_stream", start, "unreachable")
		return nil, fmt.Errorf("perception: %w: %v", ErrUnavailable, err)
	}
	defer resp.Body.Close()
	switch resp.StatusCode {
	case http.StatusNotFound, http.StatusMethodNotAllowed, http.StatusNotImplemented:
		observe(p.metrics, "perception_stream", start, "unsupported")
		p.noStreamUntil.Store(time.Now().Add(5 * time.Minute).Unix())
		return nil, ErrStreamUnsupported
	}
	if resp.StatusCode/100 != 2 {
		observe(p.metrics, "perception_stream", start, fmt.Sprintf("http_%d", resp.StatusCode))
		return nil, readError("perception", resp)
	}
	if ct := resp.Header.Get("Content-Type"); strings.HasPrefix(ct, "application/json") {
		// A plain JSON answer is the whole measurement, without live steps.
		raw, err := io.ReadAll(io.LimitReader(resp.Body, 32<<20))
		if err != nil {
			return nil, fmt.Errorf("perception: %w: %v", ErrUnavailable, err)
		}
		observe(p.metrics, "perception_stream", start, "ok_json")
		return decodeMeasurement(raw)
	}
	var result *Measurement
	var streamErr error
	perr := ParseSSE(io.LimitReader(resp.Body, 64<<20), func(ev Event) {
		if ev.Comment != "" || ev.Data == "" || result != nil || streamErr != nil {
			return
		}
		var probe map[string]json.RawMessage
		if json.Unmarshal([]byte(ev.Data), &probe) != nil {
			return
		}
		_, hasUsable := probe["usable"]
		_, hasStep := probe["step"]
		_, hasDetail := probe["detail"]
		switch {
		case ev.Name == "error" || (ev.Name == "message" && hasDetail && !hasUsable):
			var e struct {
				Detail string `json:"detail"`
				Status int    `json:"status"`
			}
			_ = json.Unmarshal([]byte(ev.Data), &e)
			if e.Status == 0 {
				e.Status = http.StatusBadGateway
			}
			streamErr = &Error{Service: "perception", Status: e.Status, Message: e.Detail}
		case ev.Name == "result" || ev.Name == "done" || ev.Name == "measurement" || (ev.Name == "message" && hasUsable):
			m, err := decodeMeasurement([]byte(ev.Data))
			if err != nil {
				streamErr = err
				return
			}
			result = m
		case ev.Name == "step" || (ev.Name == "message" && hasStep):
			var st AgentStep
			if json.Unmarshal([]byte(ev.Data), &st) == nil && onStep != nil {
				onStep(st)
			}
		}
	})
	switch {
	case streamErr != nil:
		observe(p.metrics, "perception_stream", start, "stream_error")
		return nil, streamErr
	case result != nil:
		observe(p.metrics, "perception_stream", start, "ok")
		return result, nil
	case ctx.Err() != nil:
		observe(p.metrics, "perception_stream", start, "timeout")
		return nil, fmt.Errorf("perception: %w: %v", ErrUnavailable, ctx.Err())
	default:
		observe(p.metrics, "perception_stream", start, "no_result")
		return nil, fmt.Errorf("perception: %w: stream ended without a result (%v)", ErrUnavailable, perr)
	}
}
