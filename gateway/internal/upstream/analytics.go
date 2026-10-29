package upstream

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/url"
	"strconv"
	"time"
)

// EngineCity is one entry of GET /v1/cities.
type EngineCity struct {
	ID     string     `json:"id"`
	Name   string     `json:"name"`
	BBox   [4]float64 `json:"bbox"`
	Centre struct {
		Lat float64 `json:"lat"`
		Lon float64 `json:"lon"`
	} `json:"centre"`
	Cells int `json:"cells"`
	Graph struct {
		Status string `json:"status"`
		Error  string `json:"error"`
	} `json:"graph"`
}

// Cities lists the covered cities.
func (e *Engine) Cities(ctx context.Context) ([]EngineCity, error) {
	raw, err := e.do(ctx, http.MethodGet, "/v1/cities", nil, e.timeout)
	if err != nil {
		return nil, err
	}
	var out struct {
		Cities []EngineCity `json:"cities"`
	}
	if err := json.Unmarshal(raw, &out); err != nil {
		return nil, fmt.Errorf("engine cities: %w", err)
	}
	return out.Cities, nil
}

// CellsQuery returns the raw cells FeatureCollection for a query with
// `city`, `bbox` or both.
func (e *Engine) CellsQuery(ctx context.Context, q url.Values) ([]byte, error) {
	return e.do(ctx, http.MethodGet, "/v1/cells?"+q.Encode(), nil, e.timeout)
}

// HotspotParts are the four parts of a hotspot score.
type HotspotParts struct {
	Depth     float64 `json:"depth"`
	Hazard    float64 `json:"hazard"`
	Freshness float64 `json:"freshness"`
	Reports   float64 `json:"reports"`
}

// Hotspot is one ranked cell.
type Hotspot struct {
	Cell               string       `json:"cell"`
	Lat                float64      `json:"lat"`
	Lon                float64      `json:"lon"`
	Score              float64      `json:"score"`
	Parts              HotspotParts `json:"parts"`
	DepthBlockCm       float64      `json:"depth_block_cm"`
	DepthCm            float64      `json:"depth_cm"`
	DepthSigmaCm       float64      `json:"depth_sigma_cm"`
	HazardClassUpper   string       `json:"hazard_class_upper"`
	AgeH               float64      `json:"age_h"`
	IndependentReports int          `json:"independent_reports"`
	Unknown            bool         `json:"unknown"`
	Blocks             []string     `json:"blocks"`
	Reasons            []string     `json:"reasons"`
}

// Hotspots is GET /v1/hotspots.
type Hotspots struct {
	City        string    `json:"city"`
	Score       string    `json:"score"`
	Count       int       `json:"count"`
	Hotspots    []Hotspot `json:"hotspots"`
	GeneratedAt time.Time `json:"generated_at"`
}

// Hotspots ranks a city's cells.
func (e *Engine) Hotspots(ctx context.Context, city string, limit int) (*Hotspots, error) {
	q := url.Values{"limit": {strconv.Itoa(limit)}}
	if city != "" {
		q.Set("city", city)
	}
	raw, err := e.do(ctx, http.MethodGet, "/v1/hotspots?"+q.Encode(), nil, e.timeout)
	if err != nil {
		return nil, err
	}
	var out Hotspots
	if err := json.Unmarshal(raw, &out); err != nil {
		return nil, fmt.Errorf("engine hotspots: %w", err)
	}
	return &out, nil
}

// FreshCell is a cell that needs a fresh report.
type FreshCell struct {
	Cell            string  `json:"cell"`
	Lat             float64 `json:"lat"`
	Lon             float64 `json:"lon"`
	LastDepthCm     float64 `json:"last_depth_cm"`
	LastReadingAgeH float64 `json:"last_reading_age_h"`
	Expired         bool    `json:"expired"`
}

// Summary is GET /v1/summary.
type Summary struct {
	City                  string         `json:"city"`
	Cells                 int            `json:"cells"`
	ByClass               map[string]int `json:"by_class"`
	Unknown               int            `json:"unknown"`
	Stale                 int            `json:"stale"`
	Expired               int            `json:"expired"`
	CellsInConflict       int            `json:"cells_in_conflict"`
	ConflictingReadings   int            `json:"conflicting_readings"`
	Blocked               map[string]int `json:"blocked"`
	NeedsFreshReport      int            `json:"needs_fresh_report"`
	NeedsFreshReportCells []FreshCell    `json:"needs_fresh_report_cells"`
	Readings              int            `json:"readings"`
	NewestReadingAt       *time.Time     `json:"newest_reading_at"`
	GeneratedAt           time.Time      `json:"generated_at"`
}

// Summary counts a city's cells.
func (e *Engine) Summary(ctx context.Context, city string) (*Summary, error) {
	path := "/v1/summary"
	if city != "" {
		path += "?city=" + url.QueryEscape(city)
	}
	raw, err := e.do(ctx, http.MethodGet, path, nil, e.timeout)
	if err != nil {
		return nil, err
	}
	var out Summary
	if err := json.Unmarshal(raw, &out); err != nil {
		return nil, fmt.Errorf("engine summary: %w", err)
	}
	return &out, nil
}

// HistoryPoint is one direct reading in a cell's history.
type HistoryPoint struct {
	At             time.Time `json:"at"`
	ReadingCm      float64   `json:"reading_cm"`
	ReadingSigmaCm float64   `json:"reading_sigma_cm"`
	FusedCm        float64   `json:"fused_cm"`
	FusedSigmaCm   float64   `json:"fused_sigma_cm"`
	Migrated       bool      `json:"migrated"`
}

// Drain is a fitted recession rate.
type Drain struct {
	RateCmPerH      float64   `json:"rate_cm_per_h"`
	RateSigmaCmPerH float64   `json:"rate_sigma_cm_per_h"`
	Trend           string    `json:"trend"`
	PointsUsed      int       `json:"points_used"`
	From            time.Time `json:"from"`
	To              time.Time `json:"to"`
	HoursTo15Cm     *float64  `json:"hours_to_15_cm"`
}

// Recession is GET /v1/recession.
type Recession struct {
	Lat       float64        `json:"lat"`
	Lon       float64        `json:"lon"`
	History   []HistoryPoint `json:"history"`
	Current   *RecessionCell `json:"current"`
	Drain     *Drain         `json:"drain"`
	DrainNote string         `json:"drain_note"`
}

// RecessionCell is a cell's current properties, as /v1/cells has them.
type RecessionCell struct {
	CellProps
	Conflicts int `json:"conflicts"`
}

// Recession returns a cell's depth history and drain rate.
func (e *Engine) Recession(ctx context.Context, cell string) (*Recession, error) {
	raw, err := e.do(ctx, http.MethodGet, "/v1/recession?cell="+url.QueryEscape(cell), nil, e.timeout)
	if err != nil {
		return nil, err
	}
	var out Recession
	if err := json.Unmarshal(raw, &out); err != nil {
		return nil, fmt.Errorf("engine recession: %w", err)
	}
	return &out, nil
}

// Export opens GET /v1/export. The caller copies and closes the body.
func (e *Engine) Export(ctx context.Context, city, format string) (*http.Response, error) {
	q := url.Values{"city": {city}, "format": {format}}
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, e.base+"/v1/export?"+q.Encode(), nil)
	if err != nil {
		return nil, err
	}
	start := time.Now()
	resp, err := e.client.Do(req)
	if err != nil {
		observe(e.metrics, "engine", start, "unreachable")
		return nil, fmt.Errorf("engine: %w: %v", ErrUnavailable, err)
	}
	if resp.StatusCode/100 != 2 {
		observe(e.metrics, "engine", start, fmt.Sprintf("http_%d", resp.StatusCode))
		defer resp.Body.Close()
		return nil, readError("engine", resp)
	}
	observe(e.metrics, "engine", start, "ok")
	return resp, nil
}

// Readiness is the engine's /readyz answer.
type Readiness struct {
	Status string `json:"status"` // ready or degraded (or the reason it is not ready)
	Reason string `json:"reason"`
}

// Readiness checks /readyz and reads its status. A 503 is an error.
func (e *Engine) Readiness(ctx context.Context) (Readiness, error) {
	raw, err := e.do(ctx, http.MethodGet, "/readyz", nil, 2*time.Second)
	if err != nil {
		return Readiness{}, err
	}
	var r Readiness
	_ = json.Unmarshal(raw, &r)
	if r.Status == "" {
		r.Status = "ready"
	}
	return r, nil
}
