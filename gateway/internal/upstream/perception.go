package upstream

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"mime/multipart"
	"net/http"
	"net/textproto"
	"strconv"
	"strings"
	"sync/atomic"
	"time"

	"neermattam/gateway/internal/metrics"
)

// Perception is a client for the Python measurement service.
type Perception struct {
	base    string
	client  *http.Client
	timeout time.Duration
	metrics *metrics.Set

	// noStreamUntil holds a unix time before which /v1/measure/stream is
	// assumed missing, so an old perception build costs one probe, not one
	// extra upload per report.
	noStreamUntil atomic.Int64
}

// NewPerception returns a perception client.
func NewPerception(base string, timeout time.Duration, m *metrics.Set) *Perception {
	return &Perception{
		base:    strings.TrimRight(base, "/"),
		client:  &http.Client{Transport: newTransport()},
		timeout: timeout,
		metrics: m,
	}
}

// Reference is the object used as a ruler. A wheel has a diameter; a
// person (kind "person") has the stature prior the depth was scaled by,
// and null diameters.
type Reference struct {
	Kind            string   `json:"kind"`
	DiameterCm      *float64 `json:"diameter_cm"`
	DiameterSigmaCm *float64 `json:"diameter_sigma_cm"`
	Source          string   `json:"source"`
	HeightCm        *float64 `json:"height_cm"`
	HeightSigmaCm   *float64 `json:"height_sigma_cm"`
}

// Waterline describes how the water surface was located.
type Waterline struct {
	Method      string   `json:"method"`
	YPx         *float64 `json:"y_px"`
	AgreementCm *float64 `json:"agreement_cm"`
}

// Measurement is the response of POST /v1/measure (CONTRACTS.md 1).
type Measurement struct {
	MeasurementID    string             `json:"measurement_id"`
	Usable           bool               `json:"usable"`
	Reasons          []string           `json:"reasons"`
	DepthCm          *float64           `json:"depth_cm"`
	DepthSigmaCm     *float64           `json:"depth_sigma_cm"`
	Reference        *Reference         `json:"reference"`
	Waterline        *Waterline         `json:"waterline"`
	VelocityMps      *float64           `json:"velocity_mps"`
	VelocitySigmaMps *float64           `json:"velocity_sigma_mps"`
	Confidence       float64            `json:"confidence"`
	OverlayPNGB64    string             `json:"overlay_png_b64,omitempty"`
	Models           map[string]string  `json:"models"`
	TimingsMs        map[string]float64 `json:"timings_ms"`
	CostUSD          *float64           `json:"cost_usd"`
	// Method is "wheel_geometry" (measured from a wheel of known size),
	// "person_reference" (measured against an adult standing in the water,
	// sigma typically 10 to 15 cm) or "model_estimate" (the scene model's
	// visual estimate, wide sigma); null when the photo is unusable. Read it
	// through Kind or MethodIs, which treat a method this gateway does not
	// know as an estimate.
	Method        *string        `json:"method"`
	ModelEstimate *ModelEstimate `json:"model_estimate"`
	AgentTrace    []AgentStep    `json:"agent_trace"`
	// CapturedAt is the photo's EXIF DateTimeOriginal: RFC 3339 with a zone
	// when the camera recorded one, otherwise local time without an offset.
	CapturedAt *string `json:"captured_at"`
	// CapturedAtSent is true when the response had a captured_at member,
	// even null. Null then means the photo has no capture time.
	CapturedAtSent bool `json:"-"`
	// InputSHA256 is the SHA-256 of the file perception measured, as it
	// received it; null from an older perception.
	InputSHA256 *string `json:"input_sha256,omitempty"`
	// Certificate is the signed measurement certificate exactly as
	// perception sent it (CONTRACTS.md 1.2), kept raw so its bytes are
	// stored and served unchanged. Empty or "null" when there is none.
	Certificate json.RawMessage `json:"certificate,omitempty"`
}

// HasCertificate reports whether perception sent a certificate.
func (m *Measurement) HasCertificate() bool {
	if m == nil {
		return false
	}
	t := bytes.TrimSpace(m.Certificate)
	return len(t) > 0 && !bytes.Equal(t, []byte("null"))
}

// Methods perception reports.
const (
	MethodWheelGeometry   = "wheel_geometry"
	MethodPersonReference = "person_reference"
	MethodModelEstimate   = "model_estimate"
)

// NormalizeMethod maps perception's method to one this gateway handles. A
// method it does not know (a newer perception) is treated as an estimate,
// never as a measurement, so it can only ever count for less. The empty
// string (an older perception that sent no method) stays empty.
func NormalizeMethod(method string) string {
	switch method {
	case "", MethodWheelGeometry, MethodPersonReference, MethodModelEstimate:
		return method
	}
	return MethodModelEstimate
}

// KnownMethod reports whether this gateway knows the method by name.
func KnownMethod(method string) bool {
	return NormalizeMethod(method) == method
}

// Kind is the measurement's method after NormalizeMethod: empty when there
// is none.
func (m *Measurement) Kind() string {
	if m == nil || m.Method == nil {
		return ""
	}
	return NormalizeMethod(*m.Method)
}

// MethodIs reports whether the measurement used the given method, reading
// an unknown method as a model estimate.
func (m *Measurement) MethodIs(method string) bool {
	return m != nil && m.Method != nil && m.Kind() == method
}

// IsPerson reports whether the ruler was a person.
func (r *Reference) IsPerson() bool { return r != nil && r.Kind == "person" }

// ModelEstimate is the scene model's own visual estimate of the depth.
type ModelEstimate struct {
	DepthCm      float64 `json:"depth_cm"`
	DepthSigmaCm float64 `json:"depth_sigma_cm"`
	Basis        string  `json:"basis"`
	Model        string  `json:"model"`
}

// AgentStep is one model step in perception's verify-and-refine agent.
type AgentStep struct {
	Step      string `json:"step"`
	Target    string `json:"target"`
	Model     string `json:"model"`
	Input     string `json:"input"`
	Output    string `json:"output"`
	Decision  string `json:"decision"`
	LatencyMs int    `json:"latency_ms"`
	// PromptVersion names the prompt a language-model step was sent, such
	// as "verify-wheel@1d51d73f5037"; empty for other steps and from an
	// older perception.
	PromptVersion string `json:"prompt_version,omitempty"`
}

// Upload is one file part.
type Upload struct {
	Field    string // "image", "video" or "audio"
	Filename string
	MIME     string
	Data     []byte
}

// MeasureRequest is the multipart body of /v1/measure.
type MeasureRequest struct {
	Image          *Upload
	Video          *Upload
	HintReference  string
	HintDiameterCm *float64
}

func measureBody(req MeasureRequest) (*bytes.Buffer, string, error) {
	var buf bytes.Buffer
	w := multipart.NewWriter(&buf)
	for _, u := range []*Upload{req.Image, req.Video} {
		if u == nil {
			continue
		}
		if err := writeFile(w, u); err != nil {
			return nil, "", err
		}
	}
	if req.HintReference != "" {
		_ = w.WriteField("hint_reference", req.HintReference)
	}
	if req.HintDiameterCm != nil {
		_ = w.WriteField("hint_diameter_cm", strconv.FormatFloat(*req.HintDiameterCm, 'f', -1, 64))
	}
	if err := w.Close(); err != nil {
		return nil, "", err
	}
	return &buf, w.FormDataContentType(), nil
}

// Measure sends a photo or video for measurement.
func (p *Perception) Measure(ctx context.Context, req MeasureRequest) (*Measurement, error) {
	body, ct, err := measureBody(req)
	if err != nil {
		return nil, err
	}
	raw, err := p.post(ctx, "/v1/measure", ct, body)
	if err != nil {
		return nil, err
	}
	return decodeMeasurement(raw)
}

func decodeMeasurement(raw []byte) (*Measurement, error) {
	var m Measurement
	if err := json.Unmarshal(raw, &m); err != nil {
		return nil, fmt.Errorf("perception measure response: %w", err)
	}
	if m.Usable && (m.DepthCm == nil || m.DepthSigmaCm == nil) {
		return nil, fmt.Errorf("perception said usable but sent no depth or sigma")
	}
	var probe map[string]json.RawMessage
	if json.Unmarshal(raw, &probe) == nil {
		_, m.CapturedAtSent = probe["captured_at"]
	}
	return &m, nil
}

// Transcript is the response of POST /v1/transcribe.
type Transcript struct {
	Text       string `json:"text"`
	Language   string `json:"language"`
	PlaceText  string `json:"place_text"`
	DepthWords string `json:"depth_words"`
	// DepthCm and DepthSigmaCm are the model's reading of the spoken depth
	// words, or null when no depth was spoken.
	DepthCm      *float64 `json:"depth_cm"`
	DepthSigmaCm *float64 `json:"depth_sigma_cm"`
	Model        string   `json:"model"`
	CostUSD      float64  `json:"cost_usd"`
	// InputSHA256, PromptVersion and Validation are absent from an older
	// perception: the audio's hash, the prompt version, and what the checks
	// of the model's answer dropped.
	InputSHA256   string   `json:"input_sha256,omitempty"`
	PromptVersion string   `json:"prompt_version,omitempty"`
	Validation    []string `json:"validation,omitempty"`
}

// Transcribe sends a voice note.
func (p *Perception) Transcribe(ctx context.Context, audio Upload) (*Transcript, error) {
	var buf bytes.Buffer
	w := multipart.NewWriter(&buf)
	audio.Field = "audio"
	if err := writeFile(w, &audio); err != nil {
		return nil, err
	}
	if err := w.Close(); err != nil {
		return nil, err
	}
	raw, err := p.post(ctx, "/v1/transcribe", w.FormDataContentType(), &buf)
	if err != nil {
		return nil, err
	}
	var t Transcript
	if err := json.Unmarshal(raw, &t); err != nil {
		return nil, fmt.Errorf("perception transcribe response: %w", err)
	}
	return &t, nil
}

// Keys fetches perception's certificate public keys (GET /v1/keys), as
// the raw JSON document.
func (p *Perception) Keys(ctx context.Context) ([]byte, error) {
	ctx, cancel := context.WithTimeout(ctx, 5*time.Second)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, p.base+"/v1/keys", nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("Accept", "application/json")
	resp, err := p.client.Do(req)
	if err != nil {
		return nil, fmt.Errorf("perception: %w: %v", ErrUnavailable, err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, readError("perception", resp)
	}
	return io.ReadAll(io.LimitReader(resp.Body, 1<<16))
}

// Ready checks perception's /readyz.
func (p *Perception) Ready(ctx context.Context) error {
	ctx, cancel := context.WithTimeout(ctx, 2*time.Second)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, p.base+"/readyz", nil)
	if err != nil {
		return err
	}
	resp, err := p.client.Do(req)
	if err != nil {
		return fmt.Errorf("perception: %w: %v", ErrUnavailable, err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return readError("perception", resp)
	}
	return nil
}

func writeFile(w *multipart.Writer, u *Upload) error {
	if u.Field == "" {
		return errors.New("upload has no field name")
	}
	h := make(textproto.MIMEHeader)
	h.Set("Content-Disposition", fmt.Sprintf(`form-data; name=%q; filename=%q`, u.Field, u.Filename))
	h.Set("Content-Type", u.MIME)
	part, err := w.CreatePart(h)
	if err != nil {
		return err
	}
	_, err = part.Write(u.Data)
	return err
}

func (p *Perception) post(ctx context.Context, path, contentType string, body io.Reader) ([]byte, error) {
	ctx, cancel := context.WithTimeout(ctx, p.timeout)
	defer cancel()
	start := time.Now()
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, p.base+path, body)
	if err != nil {
		return nil, err
	}
	req.Header.Set("Content-Type", contentType)
	req.Header.Set("Accept", "application/json")
	resp, err := p.client.Do(req)
	if err != nil {
		observe(p.metrics, "perception", start, "unreachable")
		return nil, fmt.Errorf("perception: %w: %v", ErrUnavailable, err)
	}
	defer resp.Body.Close()
	if resp.StatusCode/100 != 2 {
		observe(p.metrics, "perception", start, fmt.Sprintf("http_%d", resp.StatusCode))
		return nil, readError("perception", resp)
	}
	raw, err := io.ReadAll(io.LimitReader(resp.Body, 32<<20))
	if err != nil {
		observe(p.metrics, "perception", start, "read_error")
		return nil, fmt.Errorf("perception: %w: %v", ErrUnavailable, err)
	}
	observe(p.metrics, "perception", start, "ok")
	return raw, nil
}
