package upstream

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"mime/multipart"
	"net/http"
	"net/textproto"
	"strconv"
	"strings"
	"time"

	"neermattam/gateway/internal/metrics"
)

// Perception is a client for the Python measurement service.
type Perception struct {
	base    string
	client  *http.Client
	timeout time.Duration
	metrics *metrics.Set
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

// Reference is the object used as a ruler.
type Reference struct {
	Kind            string   `json:"kind"`
	DiameterCm      *float64 `json:"diameter_cm"`
	DiameterSigmaCm *float64 `json:"diameter_sigma_cm"`
	Source          string   `json:"source"`
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

// Measure sends a photo or video for measurement.
func (p *Perception) Measure(ctx context.Context, req MeasureRequest) (*Measurement, error) {
	var buf bytes.Buffer
	w := multipart.NewWriter(&buf)
	for _, u := range []*Upload{req.Image, req.Video} {
		if u == nil {
			continue
		}
		if err := writeFile(w, u); err != nil {
			return nil, err
		}
	}
	if req.HintReference != "" {
		_ = w.WriteField("hint_reference", req.HintReference)
	}
	if req.HintDiameterCm != nil {
		_ = w.WriteField("hint_diameter_cm", strconv.FormatFloat(*req.HintDiameterCm, 'f', -1, 64))
	}
	if err := w.Close(); err != nil {
		return nil, err
	}
	raw, err := p.post(ctx, "/v1/measure", w.FormDataContentType(), &buf)
	if err != nil {
		return nil, err
	}
	var m Measurement
	if err := json.Unmarshal(raw, &m); err != nil {
		return nil, fmt.Errorf("perception measure response: %w", err)
	}
	if m.Usable && (m.DepthCm == nil || m.DepthSigmaCm == nil) {
		return nil, fmt.Errorf("perception said usable but sent no depth or sigma")
	}
	return &m, nil
}

// Transcript is the response of POST /v1/transcribe.
type Transcript struct {
	Text       string `json:"text"`
	Language   string `json:"language"`
	PlaceText  string `json:"place_text"`
	DepthWords string `json:"depth_words"`
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
