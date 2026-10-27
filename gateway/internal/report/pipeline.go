// Package report is the one pipeline every report goes through, whether it
// came from the web form, the JSON API or WhatsApp: check the upload, look
// for duplicates, measure with perception, verify, fuse in the engine, and
// tell the live map.
package report

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"log/slog"
	"math"
	"net/http"
	"time"

	"neermattam/gateway/internal/config"
	"neermattam/gateway/internal/live"
	"neermattam/gateway/internal/media"
	"neermattam/gateway/internal/metrics"
	"neermattam/gateway/internal/places"
	"neermattam/gateway/internal/upstream"
	"neermattam/gateway/internal/verify"
)

// Stage names reported while a report is processed.
const (
	StageReceived = "received"
	StageScene    = "scene"
	StageVerify   = "verify"
	StageFuse     = "fuse"
)

// Status of a finished report.
const (
	StatusFused    = "fused"
	StatusUnusable = "unusable"
	StatusRejected = "rejected"
)

// Error is a report that could not be processed, with an HTTP status and a
// message written for the reporter.
type Error struct {
	Status  int    `json:"-"`
	Code    string `json:"code"`
	Message string `json:"error"`
}

func (e *Error) Error() string { return e.Message }

func fail(status int, code, format string, args ...any) *Error {
	return &Error{Status: status, Code: code, Message: fmt.Sprintf(format, args...)}
}

// File is one uploaded part.
type File struct {
	Field    string
	Filename string
	Data     []byte
}

// Submission is a report before processing.
type Submission struct {
	Channel        string // web, api, whatsapp
	Files          []File
	Lat, Lon       float64
	HasLocation    bool
	HintReference  string
	HintDiameterCm *float64
}

// MediaInfo describes the upload that was measured.
type MediaInfo struct {
	Kind      media.Kind `json:"kind"`
	MIME      string     `json:"mime"`
	Bytes     int        `json:"bytes"`
	SHA256    string     `json:"sha256"`
	DurationS *float64   `json:"duration_s,omitempty"`
	Width     int        `json:"width,omitempty"`
	Height    int        `json:"height,omitempty"`
}

// Outcome is the result returned to the reporter.
type Outcome struct {
	Status       string                  `json:"status"`
	Summary      string                  `json:"summary"`
	Channel      string                  `json:"channel"`
	Source       string                  `json:"source"`
	Lat          float64                 `json:"lat"`
	Lon          float64                 `json:"lon"`
	Place        string                  `json:"place"`
	Media        MediaInfo               `json:"media"`
	Measurement  *upstream.Measurement   `json:"measurement,omitempty"`
	Transcript   *upstream.Transcript    `json:"transcript,omitempty"`
	Voice        *VoiceEstimate          `json:"voice_estimate,omitempty"`
	Verification *verify.Result          `json:"verification,omitempty"`
	ReadingID    string                  `json:"reading_id,omitempty"`
	DepthCm      *float64                `json:"depth_cm"`
	DepthSigmaCm *float64                `json:"depth_sigma_cm"`
	Cell         *upstream.ReadingResult `json:"cell,omitempty"`
	// CellHasSimulated is true when the fused cell also holds readings from
	// the simulated demo scenario.
	CellHasSimulated bool      `json:"cell_has_simulated,omitempty"`
	Advisory         string    `json:"advisory"`
	ProcessedMs      int64     `json:"processed_ms"`
	ReceivedAt       time.Time `json:"received_at"`
}

// Advisory is attached to every result.
const Advisory = "Advisory only. Check local news and official alerts before you travel."

// Pipeline wires the services together.
type Pipeline struct {
	Cfg        config.Config
	Perception *upstream.Perception
	Engine     *upstream.Engine
	Verifier   *verify.Verifier
	Dedupe     *Dedupe
	Origins    *live.Origins
	Hub        *live.Hub
	Log        *slog.Logger
	Metrics    *metrics.Set

	sem chan struct{}
	now func() time.Time
}

// Init prepares the concurrency limit. Call once before Run.
func (p *Pipeline) Init() {
	p.sem = make(chan struct{}, p.Cfg.MaxInflight)
	if p.now == nil {
		p.now = time.Now
	}
}

// StageStep carries one agent step from perception while it works.
const StageStep = "step"

// Event is a progress notice: a stage starting, or (with StageStep) one
// step of perception's agent as it happens.
type Event struct {
	Stage string              `json:"stage"`
	Step  *upstream.AgentStep `json:"step,omitempty"`
}

// Progress is told about stages and agent steps.
type Progress func(Event)

type checked struct {
	file    File
	typ     media.Type
	info    MediaInfo
	sum     [32]byte
	dhash   uint64
	hasHash bool
}

// Validate checks the location and files without calling any service, so
// handlers can reject a bad upload before a progress stream starts.
func (p *Pipeline) Validate(sub Submission) error {
	_, err := p.check(sub)
	return err
}

func (p *Pipeline) check(sub Submission) (*checked, error) {
	if !sub.HasLocation {
		return nil, fail(http.StatusBadRequest, "location_required", "Add a location: tap the map, use your device location, or type coordinates.")
	}
	if math.IsNaN(sub.Lat) || math.IsNaN(sub.Lon) || !p.Cfg.ServiceArea.Contains(sub.Lat, sub.Lon) {
		return nil, fail(http.StatusBadRequest, "outside_area", "That location (%.4f, %.4f) is outside the Chennai service area.", sub.Lat, sub.Lon)
	}
	if len(sub.Files) == 0 {
		return nil, fail(http.StatusBadRequest, "media_required", "Attach a photo, a short video or a voice note.")
	}
	var visual, audio *checked
	for _, f := range sub.Files {
		c, err := p.checkFile(f)
		if err != nil {
			return nil, err
		}
		switch c.typ.Kind {
		case media.Image, media.Video:
			if visual == nil || (visual.typ.Kind == media.Video && c.typ.Kind == media.Image) {
				visual = c
			}
		case media.Audio:
			audio = c
		}
	}
	if visual != nil {
		return visual, nil
	}
	return audio, nil
}

func (p *Pipeline) checkFile(f File) (*checked, error) {
	if len(f.Data) == 0 {
		return nil, fail(http.StatusBadRequest, "empty_file", "The file %q is empty.", f.Filename)
	}
	typ, err := media.Sniff(f.Data)
	if err != nil {
		msg := "That file is not a JPEG or PNG photo, an MP4 or MOV video, or an Ogg, WAV or M4A voice note."
		if errors.Is(err, media.ErrUnsupported) && err.Error() != media.ErrUnsupported.Error() {
			msg = "Unsupported file: " + err.Error()[len(media.ErrUnsupported.Error())+2:] + "."
		}
		return nil, fail(http.StatusUnsupportedMediaType, "unsupported_media", "%s", msg)
	}
	c := &checked{file: f, typ: typ, sum: sha256.Sum256(f.Data)}
	c.info = MediaInfo{Kind: typ.Kind, MIME: typ.MIME, Bytes: len(f.Data), SHA256: hex.EncodeToString(c.sum[:])}
	switch typ.Kind {
	case media.Image:
		if int64(len(f.Data)) > p.Cfg.MaxImageBytes {
			return nil, fail(http.StatusRequestEntityTooLarge, "too_large", "Photos can be up to %d MB.", p.Cfg.MaxImageBytes>>20)
		}
		cfg, err := media.CheckImage(f.Data, p.Cfg.MaxPixels)
		if err != nil {
			return nil, fail(http.StatusBadRequest, "bad_image", "%s.", capitalise(err.Error()))
		}
		c.info.Width, c.info.Height = cfg.Width, cfg.Height
		if h, err := media.ImageDHash(f.Data); err == nil {
			c.dhash, c.hasHash = h, true
		}
	case media.Video:
		if int64(len(f.Data)) > p.Cfg.MaxVideoBytes {
			return nil, fail(http.StatusRequestEntityTooLarge, "too_large", "Videos can be up to %d MB.", p.Cfg.MaxVideoBytes>>20)
		}
		d, err := media.MP4Duration(f.Data)
		if err != nil {
			return nil, fail(http.StatusBadRequest, "bad_video", "The video length could not be read. Please send an MP4 or MOV file.")
		}
		secs := math.Round(d.Seconds()*10) / 10
		c.info.DurationS = &secs
		if d.Seconds() > p.Cfg.MaxVideoSeconds {
			return nil, fail(http.StatusRequestEntityTooLarge, "too_long", "Videos can be up to %.0f seconds; this one is %.0f seconds.", p.Cfg.MaxVideoSeconds, d.Seconds())
		}
	case media.Audio:
		if int64(len(f.Data)) > p.Cfg.MaxAudioBytes {
			return nil, fail(http.StatusRequestEntityTooLarge, "too_large", "Voice notes can be up to %d MB.", p.Cfg.MaxAudioBytes>>20)
		}
	}
	return c, nil
}

// ParseCapturedAt reads perception's captured_at: RFC 3339 with a zone, or
// local camera time without one, which is read as India Standard Time.
func ParseCapturedAt(s string) (time.Time, bool, bool) {
	if t, err := time.Parse(time.RFC3339, s); err == nil {
		return t.UTC(), true, true
	}
	for _, layout := range []string{"2006-01-02T15:04:05", "2006-01-02T15:04:05.999999999"} {
		if t, err := time.ParseInLocation(layout, s, media.IST); err == nil {
			return t.UTC(), false, true
		}
	}
	return time.Time{}, false, false
}

// Run processes a submission end to end.
func (p *Pipeline) Run(ctx context.Context, sub Submission, progress Progress) (*Outcome, error) {
	if progress == nil {
		progress = func(Event) {}
	}
	start := p.now()
	c, err := p.check(sub)
	if err != nil {
		p.count(sub.Channel, "invalid")
		return nil, err
	}
	if m, dup := p.Dedupe.Check(c.sum, c.dhash, c.hasHash); dup {
		p.count(sub.Channel, "duplicate")
		if m.Exact {
			return nil, fail(http.StatusConflict, "duplicate", "This exact file was already reported %s ago, so it is not counted twice.", ago(m.Ago))
		}
		return nil, fail(http.StatusConflict, "near_duplicate", "This photo looks the same as one reported %s ago (hash distance %d of 64), so it is not counted twice.", ago(m.Ago), m.Distance)
	}

	select {
	case p.sem <- struct{}{}:
		defer func() { <-p.sem }()
	case <-ctx.Done():
		return nil, ctx.Err()
	default:
		p.count(sub.Channel, "busy")
		return nil, fail(http.StatusServiceUnavailable, "busy", "Many reports are being measured right now. Please try again in a minute.")
	}

	out := &Outcome{
		Channel: sub.Channel, Lat: sub.Lat, Lon: sub.Lon, Place: places.Near(sub.Lat, sub.Lon),
		Media: c.info, Advisory: Advisory, ReceivedAt: start.UTC(),
	}
	progress(Event{Stage: StageScene})

	var in verify.Input
	switch c.typ.Kind {
	case media.Image, media.Video:
		out.Source = "photo"
		req := upstream.MeasureRequest{HintReference: sub.HintReference, HintDiameterCm: sub.HintDiameterCm}
		up := &upstream.Upload{Filename: "report" + c.typ.Ext, MIME: c.typ.MIME, Data: c.file.Data}
		if c.typ.Kind == media.Image {
			up.Field = "image"
			req.Image = up
		} else {
			out.Source = "video"
			up.Field = "video"
			req.Video = up
		}
		// Prefer the stream so the agent's steps reach the page live; an
		// older perception without it gets the plain request.
		m, err := p.Perception.MeasureStream(ctx, req, func(st upstream.AgentStep) {
			progress(Event{Stage: StageStep, Step: &st})
		})
		if errors.Is(err, upstream.ErrStreamUnsupported) {
			m, err = p.Perception.Measure(ctx, req)
		}
		if err != nil {
			p.count(sub.Channel, "error")
			return nil, p.upstreamFail("perception", err)
		}
		out.Measurement = m
		if !m.Usable {
			out.Status = StatusUnusable
			out.Summary = "The photo could not be measured."
			p.Dedupe.Remember(c.sum, c.dhash, c.hasHash)
			p.finish(out, start)
			return out, nil
		}
		in = verify.Input{DepthCm: *m.DepthCm, DepthSigmaCm: *m.DepthSigmaCm, Confidence: m.Confidence}
		if m.Method != nil {
			in.Method = *m.Method
		}
		// Perception reads the capture time from EXIF before it strips
		// metadata, and null means there was none; the gateway's own reader
		// is only a fallback for an older perception without the field.
		if m.CapturedAt != nil {
			if t, known, ok := ParseCapturedAt(*m.CapturedAt); ok {
				in.CapturedAt, in.ZoneKnown = &t, known
			}
		} else if !m.CapturedAtSent && c.typ.MIME == "image/jpeg" {
			if capt, ok := media.ExifCapture(c.file.Data); ok {
				t := capt.Time
				in.CapturedAt, in.ZoneKnown = &t, capt.ZoneKnown
			}
		}
	case media.Audio:
		out.Source = "voice"
		t, err := p.Perception.Transcribe(ctx, upstream.Upload{Filename: "voice" + c.typ.Ext, MIME: c.typ.MIME, Data: c.file.Data})
		if err != nil {
			p.count(sub.Channel, "error")
			return nil, p.upstreamFail("perception", err)
		}
		out.Transcript = t
		est, ok := FromTranscript(t)
		if !ok {
			out.Status = StatusUnusable
			out.Summary = "No depth was mentioned in the voice note. Try saying how deep, such as knee deep or 30 centimetres."
			p.Dedupe.Remember(c.sum, 0, false)
			p.finish(out, start)
			return out, nil
		}
		out.Voice = &est
		in = verify.Input{DepthCm: est.DepthCm, DepthSigmaCm: est.SigmaCm, Confidence: -1}
	}
	in.Lat, in.Lon, in.Source, in.ReceivedAt = sub.Lat, sub.Lon, out.Source, start

	progress(Event{Stage: StageVerify})
	verdict := p.Verifier.Verify(ctx, in)
	out.Verification = &verdict
	depth, sigma := in.DepthCm, in.DepthSigmaCm
	out.DepthCm, out.DepthSigmaCm = &depth, &sigma
	if !verdict.Accepted {
		out.Status = StatusRejected
		out.Summary = "Measured, but not added to the map."
		p.Dedupe.Remember(c.sum, c.dhash, c.hasHash)
		p.finish(out, start)
		return out, nil
	}

	progress(Event{Stage: StageFuse})
	reading := upstream.Reading{
		ReadingID:    "r_" + hex.EncodeToString(c.sum[:10]),
		Lat:          sub.Lat,
		Lon:          sub.Lon,
		DepthCm:      depth,
		DepthSigmaCm: sigma,
		ObservedAt:   verdict.ObservedAt,
		Source:       out.Source,
		Trust:        verdict.Trust,
	}
	if m := out.Measurement; m != nil && m.VelocityMps != nil && m.VelocitySigmaMps != nil && *m.VelocitySigmaMps > 0 {
		reading.VelocityMps, reading.VelocitySigmaMps = m.VelocityMps, m.VelocitySigmaMps
	}
	p.Origins.Expect(sub.Lat, sub.Lon, false)
	cell, err := p.Engine.PostReading(ctx, reading)
	if err != nil {
		p.count(sub.Channel, "error")
		return nil, p.upstreamFail("engine", err)
	}
	out.ReadingID = reading.ReadingID
	out.Cell = cell
	p.Origins.Mark(cell.Cell, false)
	out.CellHasSimulated = p.Origins.HasSimulatedEvidence(cell.Cell)
	go p.markNearby(sub.Lat, sub.Lon, false)
	p.Dedupe.Remember(c.sum, c.dhash, c.hasHash)
	out.Status = StatusFused
	out.Summary = fmt.Sprintf("About %.0f cm of water, plus or minus %.0f cm.", depth, sigma)
	if p.Metrics != nil {
		p.Metrics.Trust.Observe(verdict.Trust)
	}
	p.Hub.PublishFeed(live.FeedItem{
		Source: out.Source, DepthCm: depth, DepthSigmaCm: sigma,
		Place: out.Place, Lat: sub.Lat, Lon: sub.Lon, At: start.UTC(),
	})
	p.finish(out, start)
	return out, nil
}

func (p *Pipeline) finish(out *Outcome, start time.Time) {
	out.ProcessedMs = p.now().Sub(start).Milliseconds()
	p.count(out.Channel, out.Status)
	p.Log.Info("report processed",
		"channel", out.Channel, "status", out.Status, "source", out.Source,
		"place", out.Place, "processed_ms", out.ProcessedMs, "reading_id", out.ReadingID)
}

func (p *Pipeline) count(channel, status string) {
	if p.Metrics != nil {
		p.Metrics.Reports.Inc(channel, status)
	}
}

func (p *Pipeline) upstreamFail(service string, err error) error {
	p.Log.Warn("upstream failed during report", "service", service, "error", err.Error())
	var ue *upstream.Error
	if errors.As(err, &ue) {
		if ue.Status >= 400 && ue.Status < 500 {
			return fail(http.StatusBadGateway, service+"_refused", "The %s service could not use this report: %s", serviceName(service), ue.Message)
		}
		return fail(http.StatusBadGateway, service+"_error", "The %s service had a problem. Please try again shortly.", serviceName(service))
	}
	return fail(http.StatusServiceUnavailable, service+"_unavailable", "The %s service is not reachable right now. Please try again shortly.", serviceName(service))
}

func serviceName(s string) string {
	if s == "engine" {
		return "map"
	}
	return "measurement"
}

// markNearby labels the cells a reading touched (its own and the ring of
// six), found by distance because the gateway does not compute H3 itself.
func (p *Pipeline) markNearby(lat, lon float64, simulated bool) {
	MarkNearby(context.Background(), p.Engine, p.Origins, lat, lon, simulated)
}

// MarkNearby fetches cells around a reading and labels those within the
// zone radius.
func MarkNearby(ctx context.Context, e *upstream.Engine, o *live.Origins, lat, lon float64, simulated bool) {
	raw, err := e.Cells(ctx, BBoxAround(lat, lon, live.ZoneRadiusM+20))
	if err != nil {
		return
	}
	cells, err := upstream.DecodeCells(raw)
	if err != nil {
		return
	}
	for _, c := range cells {
		if live.DistanceM(lat, lon, c.Lat, c.Lon) <= live.ZoneRadiusM {
			o.Mark(c.ID, simulated)
		}
	}
}

// BBoxAround is a minLon,minLat,maxLon,maxLat box reaching radiusM from a point.
func BBoxAround(lat, lon, radiusM float64) string {
	dLat := radiusM / 111_320
	dLon := radiusM / (111_320 * math.Cos(lat*math.Pi/180))
	return fmt.Sprintf("%.6f,%.6f,%.6f,%.6f", lon-dLon, lat-dLat, lon+dLon, lat+dLat)
}

// EngineNeighbourhood answers verify.Neighbourhood from the engine, leaving
// out unknown cells and any cell touched by simulated readings, so a real
// report is never judged against the demo scenario.
type EngineNeighbourhood struct {
	Engine  *upstream.Engine
	Origins *live.Origins
}

// Nearby implements verify.Neighbourhood.
func (n EngineNeighbourhood) Nearby(ctx context.Context, lat, lon, radiusM float64) ([]verify.NearbyCell, error) {
	raw, err := n.Engine.Cells(ctx, BBoxAround(lat, lon, radiusM))
	if err != nil {
		return nil, err
	}
	cells, err := upstream.DecodeCells(raw)
	if err != nil {
		return nil, err
	}
	var out []verify.NearbyCell
	for _, c := range cells {
		if c.Props.Unknown || n.Origins.HasSimulatedEvidence(c.ID) {
			continue
		}
		d := live.DistanceM(lat, lon, c.Lat, c.Lon)
		if d > radiusM {
			continue
		}
		out = append(out, verify.NearbyCell{DepthCm: c.Props.DepthCm, DepthSigmaCm: c.Props.DepthSigmaCm, DistanceM: d})
	}
	return out, nil
}

func ago(d time.Duration) string {
	switch {
	case d < time.Minute:
		return "under a minute"
	case d < time.Hour:
		return fmt.Sprintf("%d minutes", int(d.Minutes()))
	default:
		return fmt.Sprintf("%.0f hours", d.Hours())
	}
}

func capitalise(s string) string {
	if s == "" {
		return s
	}
	b := []byte(s)
	if b[0] >= 'a' && b[0] <= 'z' {
		b[0] -= 'a' - 'A'
	}
	return string(b)
}
