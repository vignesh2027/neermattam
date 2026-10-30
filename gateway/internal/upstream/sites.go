package upstream

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/url"
	"time"
)

// Critical sites: the engine's /v1/sites, /v1/sites/{id} and /v1/siting
// (CONTRACTS.md section 2, "Critical sites").

// SiteDeepest is the deepest cell of a watch zone.
type SiteDeepest struct {
	Cell         string  `json:"cell"`
	DepthBlockCm float64 `json:"depth_block_cm"`
	DepthCm      float64 `json:"depth_cm"`
	DepthSigmaCm float64 `json:"depth_sigma_cm"`
	Unknown      bool    `json:"unknown"`
	AgeH         float64 `json:"age_h"`
}

// SiteZone is one ring around a site.
type SiteZone struct {
	RadiusM            float64      `json:"radius_m"`
	Cells              int          `json:"cells"`
	CellsReported      int          `json:"cells_reported"`
	CellsKnown         int          `json:"cells_known"`
	Coverage           float64      `json:"coverage"`
	Status             string       `json:"status"` // over, under, unknown
	Deepest            *SiteDeepest `json:"deepest"`
	OldDeepCells       int          `json:"old_deep_cells"`
	OldDeepMaxCm       *float64     `json:"old_deep_max_cm"`
	OldDeepAgeH        *float64     `json:"old_deep_age_h"`
	NewestEvidenceAgeH *float64     `json:"newest_evidence_age_h"`
	Trend              *string      `json:"trend"`
	TrendCells         struct {
		Rising  int `json:"rising"`
		Falling int `json:"falling"`
		Steady  int `json:"steady"`
	} `json:"trend_cells"`
}

// Site is one critical site with its zones.
type Site struct {
	ID       string     `json:"id"`
	Kind     string     `json:"kind"`
	Name     string     `json:"name"`
	Operator string     `json:"operator"`
	Lat      float64    `json:"lat"`
	Lon      float64    `json:"lon"`
	City     string     `json:"city"`
	Cell     string     `json:"cell"`
	Source   string     `json:"source"` // osm or operator
	OSM      string     `json:"osm"`
	Owner    string     `json:"owner"`
	Status   string     `json:"status"` // over, nearby, under, unknown
	Zones    []SiteZone `json:"zones"`
}

// SiteList is GET /v1/sites.
type SiteList struct {
	City        string  `json:"city"`
	Total       int     `json:"total"`
	Count       int     `json:"count"`
	ThresholdCm float64 `json:"threshold_cm"`
	Counts      map[string]struct {
		OSM      int `json:"osm"`
		Operator int `json:"operator"`
	} `json:"counts"`
	OSM struct {
		Attribution string  `json:"attribution"`
		DataUntil   *string `json:"data_until"`
	} `json:"osm"`
	Sites []Site `json:"sites"`
}

// SiteRoute is one access route.
type SiteRoute struct {
	Target      string  `json:"target"`  // main_road or fuel
	Vehicle     string  `json:"vehicle"` // truck, two_wheeler, car
	Status      string  `json:"status"`  // open, no_safe_route, unavailable
	Reason      *string `json:"reason"`
	Destination *struct {
		ID   string  `json:"id"`
		Name *string `json:"name"`
		Lat  float64 `json:"lat"`
		Lon  float64 `json:"lon"`
	} `json:"destination"`
	DistanceM              *float64     `json:"distance_m"`
	BaselineDistanceM      *float64     `json:"baseline_distance_m"`
	DetourM                *float64     `json:"detour_m"`
	EtaMin                 *float64     `json:"eta_min"`
	CellsOnRoute           *int         `json:"cells_on_route"`
	UnknownCellsCrossed    *int         `json:"unknown_cells_crossed"`
	ImpassableCellsCrossed *int         `json:"impassable_cells_crossed"`
	MaxKnownDepthCm        *float64     `json:"max_known_depth_cm"`
	Explanation            *string      `json:"explanation"`
	Coordinates            [][2]float64 `json:"coordinates"`
}

// SiteArea is one area of the risk ledger.
type SiteArea struct {
	Area             string       `json:"area"`
	Lat              float64      `json:"lat"`
	Lon              float64      `json:"lon"`
	Boundary         [][2]float64 `json:"boundary"`
	Readings         int          `json:"readings"`
	WetReadings      int          `json:"wet_readings"`
	DaysWithWater    int          `json:"days_with_water"`
	PeakCm           float64      `json:"peak_cm"`
	PeakSigmaCm      float64      `json:"peak_sigma_cm"`
	PeakAt           *time.Time   `json:"peak_at"`
	Episodes         int          `json:"episodes"`
	MedianRecessionH *float64     `json:"median_recession_h"`
	MaxRecessionH    *float64     `json:"max_recession_h"`
	OpenEpisodes     int          `json:"open_episodes"`
	Unresolved       int          `json:"unresolved_episodes"`
	FirstAt          *time.Time   `json:"first_at"`
	LastAt           *time.Time   `json:"last_at"`
}

// SiteDetail is GET /v1/sites/{id}.
type SiteDetail struct {
	Site
	Access *struct {
		ComputedAt time.Time   `json:"computed_at"`
		Routes     []SiteRoute `json:"routes"`
	} `json:"access"`
	Risk *struct {
		Since          time.Time  `json:"since"`
		WetThresholdCm float64    `json:"wet_threshold_cm"`
		Areas          []SiteArea `json:"areas"`
		Note           string     `json:"note"`
	} `json:"risk"`
	ThresholdCm float64 `json:"threshold_cm"`
}

// Siting is GET /v1/siting.
type Siting struct {
	City           string     `json:"city"`
	Bucket         string     `json:"bucket"`
	Since          time.Time  `json:"since"`
	HistoryDays    float64    `json:"history_days"`
	WetThresholdCm float64    `json:"wet_threshold_cm"`
	Total          int        `json:"total"`
	Note           string     `json:"note"`
	Areas          []SiteArea `json:"areas"`
}

// SitesRaw is GET /v1/sites with a query, as the engine sent it.
func (e *Engine) SitesRaw(ctx context.Context, q url.Values) ([]byte, error) {
	return e.do(ctx, http.MethodGet, "/v1/sites?"+q.Encode(), nil, e.routeTimeout)
}

// SiteRaw is GET /v1/sites/{id}; it plans six routes, so it has the route
// timeout.
func (e *Engine) SiteRaw(ctx context.Context, id string, q url.Values) ([]byte, error) {
	return e.do(ctx, http.MethodGet, "/v1/sites/"+url.PathEscape(id)+"?"+q.Encode(), nil, e.routeTimeout)
}

// SitingRaw is GET /v1/siting.
func (e *Engine) SitingRaw(ctx context.Context, q url.Values) ([]byte, error) {
	return e.do(ctx, http.MethodGet, "/v1/siting?"+q.Encode(), nil, e.timeout)
}

// AddSite is POST /v1/sites with the body as given.
func (e *Engine) AddSite(ctx context.Context, body []byte) ([]byte, error) {
	return e.do(ctx, http.MethodPost, "/v1/sites", bytes.NewReader(body), e.timeout)
}

// DeleteSite is DELETE /v1/sites/{id}?owner=.
func (e *Engine) DeleteSite(ctx context.Context, id, owner string) error {
	_, err := e.do(ctx, http.MethodDelete, "/v1/sites/"+url.PathEscape(id)+"?"+url.Values{"owner": {owner}}.Encode(), nil, e.timeout)
	return err
}

// Sites decodes GET /v1/sites.
func (e *Engine) Sites(ctx context.Context, q url.Values) (*SiteList, error) {
	raw, err := e.SitesRaw(ctx, q)
	if err != nil {
		return nil, err
	}
	var out SiteList
	if err := json.Unmarshal(raw, &out); err != nil {
		return nil, fmt.Errorf("engine sites: %w", err)
	}
	return &out, nil
}

// Site decodes GET /v1/sites/{id}.
func (e *Engine) Site(ctx context.Context, id string, q url.Values) (*SiteDetail, error) {
	raw, err := e.SiteRaw(ctx, id, q)
	if err != nil {
		return nil, err
	}
	var out SiteDetail
	if err := json.Unmarshal(raw, &out); err != nil {
		return nil, fmt.Errorf("engine site: %w", err)
	}
	return &out, nil
}

// SitingAreas decodes GET /v1/siting.
func (e *Engine) SitingAreas(ctx context.Context, q url.Values) (*Siting, error) {
	raw, err := e.SitingRaw(ctx, q)
	if err != nil {
		return nil, err
	}
	var out Siting
	if err := json.Unmarshal(raw, &out); err != nil {
		return nil, fmt.Errorf("engine siting: %w", err)
	}
	return &out, nil
}
