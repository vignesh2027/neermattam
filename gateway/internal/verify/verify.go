// Package verify decides how much the engine should trust one report. Each
// rule is explicit, returns a reason a person can read, and multiplies a
// trust factor. Only two things reject a report outright: a measurement the
// model itself is unsure of, and a photo too old to describe the water now.
// Everything else lowers trust, because a strange report can still be the
// first sign of real water, and the engine's fusion already discounts
// low-trust readings by inflating their variance (R = sigma^2 / trust).
package verify

import (
	"context"
	"fmt"
	"log/slog"
	"math"
	"time"

	"neermattam/gateway/internal/metrics"
)

// Outcome of one rule.
type Outcome string

// Rule outcomes.
const (
	Pass   Outcome = "pass"   // evidence supports the report
	Lower  Outcome = "lower"  // trust reduced
	Reject Outcome = "reject" // the report is not fused
	Skip   Outcome = "skip"   // the rule could not run; no effect
)

// RuleResult is one rule's verdict.
type RuleResult struct {
	Rule    string  `json:"rule"`
	Outcome Outcome `json:"outcome"`
	Factor  float64 `json:"factor"`
	Reason  string  `json:"reason"`
	// Key and Args give the reason in other languages; Reason is English.
	Key  string `json:"-"`
	Args []any  `json:"-"`
	// ZoneAssumed says the camera time was read as IST.
	ZoneAssumed bool `json:"-"`
}

// Age is a duration shown to people, formatted in their language.
type Age time.Duration

// Result is the full verdict.
type Result struct {
	Accepted   bool         `json:"accepted"`
	Trust      float64      `json:"trust"`
	ObservedAt time.Time    `json:"observed_at"`
	TimeSource string       `json:"time_source"` // "exif" or "received"
	Rules      []RuleResult `json:"rules"`
}

// Input is what the rules look at.
type Input struct {
	Lat, Lon     float64
	DepthCm      float64
	DepthSigmaCm float64
	// Confidence is perception's confidence, or negative when the report
	// has none (a voice note).
	Confidence float64
	Source     string // photo, video, voice
	// Method is perception's method: wheel_geometry or model_estimate.
	Method string
	// CapturedAt is the EXIF DateTimeOriginal in UTC, when present.
	CapturedAt *time.Time
	ZoneKnown  bool
	ReceivedAt time.Time
}

// NearbyCell is a known engine cell near the report.
type NearbyCell struct {
	DepthCm      float64
	DepthSigmaCm float64
	DistanceM    float64
}

// Neighbourhood returns known, real (never simulated) cells within radiusM.
type Neighbourhood interface {
	Nearby(ctx context.Context, lat, lon, radiusM float64) ([]NearbyCell, error)
}

// Rainfall returns modelled rain before a time.
type Rainfall interface {
	Rain(ctx context.Context, lat, lon float64, at time.Time) (RainSummary, error)
}

// Thresholds are the tunable numbers behind the rules. They are engineering
// judgements, documented in docs/GATEWAY.md, not fitted values.
type Thresholds struct {
	ConfidenceReject float64 // below this the report is not fused
	ConfidenceFull   float64 // at or above this, no reduction
	// EstimateConfidenceReject is the floor for model estimates, whose
	// confidence perception keeps low by design.
	EstimateConfidenceReject float64
	FreshFor                 time.Duration // full trust
	StaleAfter               time.Duration // heavier reduction
	MaxAge                   time.Duration // reject; the engine refuses older readings too
	FutureSkew               time.Duration // photo clock ahead of server by more than this
	NearbyRadiusM            float64
	SpatialSigmaPer100m      float64 // cm of expected depth difference per 100 m
	DryRainMM                float64 // 48-hour rain below this counts as dry
	WetRainMM                float64 // 48-hour rain at or above this is heavy
	DeepWithoutRainCm        float64 // depth above this is surprising on a dry day
}

// DefaultThresholds are the values the gateway runs with.
func DefaultThresholds() Thresholds {
	return Thresholds{
		ConfidenceReject:         0.35,
		ConfidenceFull:           0.6,
		EstimateConfidenceReject: 0.15,
		FreshFor:                 2 * time.Hour,
		StaleAfter:               6 * time.Hour,
		MaxAge:                   24 * time.Hour,
		FutureSkew:               15 * time.Minute,
		NearbyRadiusM:            300,
		SpatialSigmaPer100m:      5,
		DryRainMM:                1,
		WetRainMM:                10,
		DeepWithoutRainCm:        10,
	}
}

// Verifier runs the rules.
type Verifier struct {
	T       Thresholds
	Cells   Neighbourhood
	Rain    Rainfall
	Log     *slog.Logger
	Metrics *metrics.Set
}

// Verify runs every rule and combines them. It never returns an error: a
// rule that cannot run is skipped and says why.
func (v *Verifier) Verify(ctx context.Context, in Input) Result {
	res := Result{ObservedAt: in.ReceivedAt.UTC(), TimeSource: "received", Trust: 1}
	add := func(r RuleResult) {
		res.Rules = append(res.Rules, r)
	}
	add(v.confidence(in))
	fr, observed, src := v.freshness(in)
	add(fr)
	res.ObservedAt, res.TimeSource = observed, src
	add(v.source(in))
	add(v.agreement(ctx, in))
	add(v.rainfall(ctx, in, observed))

	res.Accepted = true
	for _, r := range res.Rules {
		if r.Outcome == Reject {
			res.Accepted = false
		}
		if r.Outcome == Lower || r.Outcome == Pass {
			res.Trust *= r.Factor
		}
		if v.Metrics != nil {
			v.Metrics.Rules.Inc(r.Rule, string(r.Outcome))
		}
		if v.Log != nil {
			v.Log.LogAttrs(ctx, slog.LevelInfo, "verification rule",
				slog.String("rule", r.Rule), slog.String("outcome", string(r.Outcome)),
				slog.Float64("factor", r.Factor), slog.String("reason", r.Reason))
		}
	}
	res.Trust = math.Round(math.Max(0.05, math.Min(1, res.Trust))*1000) / 1000
	if v.Log != nil {
		v.Log.LogAttrs(ctx, slog.LevelInfo, "verification result",
			slog.Bool("accepted", res.Accepted), slog.Float64("trust", res.Trust),
			slog.String("time_source", res.TimeSource))
	}
	return res
}

func (v *Verifier) confidence(in Input) RuleResult {
	r := RuleResult{Rule: "confidence_floor"}
	c := in.Confidence
	estimate := in.Method == "model_estimate"
	switch {
	case estimate && c >= v.T.EstimateConfidenceReject:
		// Perception gives its visual estimates a low confidence on purpose.
		// Their weight is already cut by the wide sigma (at least 20 cm) and
		// by the source rule, so scaling again would count the doubt twice.
		r.Outcome, r.Factor = Pass, 1
		r.Reason = fmt.Sprintf("confidence %.2f, as expected for a visual estimate; its wide uncertainty limits its weight", c)
		r.Key, r.Args = "conf_estimate", []any{c}
		return r
	case estimate:
		r.Outcome, r.Factor = Reject, 0
		r.Reason = fmt.Sprintf("estimate confidence %.2f is below the floor of %.2f", c, v.T.EstimateConfidenceReject)
		r.Key, r.Args = "conf_estimate_low", []any{c, v.T.EstimateConfidenceReject}
		return r
	case c < 0:
		r.Outcome, r.Factor = Skip, 1
		r.Reason = "spoken report, so there is no model confidence to check"
		r.Key = "conf_voice"
	case math.IsNaN(c) || c < v.T.ConfidenceReject:
		r.Outcome, r.Factor = Reject, 0
		r.Reason = fmt.Sprintf("measurement confidence %.2f is below the floor of %.2f", c, v.T.ConfidenceReject)
		r.Key, r.Args = "conf_low", []any{c, v.T.ConfidenceReject}
	case c < v.T.ConfidenceFull:
		r.Outcome, r.Factor = Lower, c/v.T.ConfidenceFull
		r.Reason = fmt.Sprintf("measurement confidence %.2f is under %.2f, so trust is scaled down", c, v.T.ConfidenceFull)
		r.Key, r.Args = "conf_scaled", []any{c, v.T.ConfidenceFull}
	default:
		r.Outcome, r.Factor = Pass, 1
		r.Reason = fmt.Sprintf("measurement confidence %.2f", c)
		r.Key, r.Args = "conf_ok", []any{c}
	}
	return r
}

func (v *Verifier) freshness(in Input) (RuleResult, time.Time, string) {
	r := RuleResult{Rule: "photo_freshness"}
	received := in.ReceivedAt.UTC()
	if in.CapturedAt == nil {
		r.Outcome, r.Factor = Lower, 0.9
		r.Reason = "no capture time in the file (WhatsApp and many apps remove it), so the time it arrived is used"
		r.Key = "fresh_none"
		return r, received, "received"
	}
	taken := in.CapturedAt.UTC()
	age := received.Sub(taken)
	zone := ""
	if !in.ZoneKnown {
		zone = " (camera time read as IST)"
	}
	switch {
	case age < -v.T.FutureSkew:
		r.Outcome, r.Factor = Lower, 0.6
		r.Reason = fmt.Sprintf("the photo clock is %s ahead of ours%s; the time it arrived is used", roundDur(-age), zone)
		r.Key, r.Args, r.ZoneAssumed = "fresh_future", []any{Age(-age)}, !in.ZoneKnown
		return r, received, "received"
	case age > v.T.MaxAge:
		r.Outcome, r.Factor = Reject, 0
		r.Reason = fmt.Sprintf("taken %s ago%s; water changes too fast for that to describe the street now", roundDur(age), zone)
		r.Key, r.Args, r.ZoneAssumed = "fresh_old", []any{Age(age)}, !in.ZoneKnown
		return r, taken, "exif"
	case age > v.T.StaleAfter:
		r.Outcome, r.Factor = Lower, 0.5
		r.Reason = fmt.Sprintf("taken %s ago%s; fused at its capture time so the map ages it", roundDur(age), zone)
		r.Key, r.Args, r.ZoneAssumed = "fresh_stale", []any{Age(age)}, !in.ZoneKnown
	case age > v.T.FreshFor:
		r.Outcome, r.Factor = Lower, 0.8
		r.Reason = fmt.Sprintf("taken %s ago%s; fused at its capture time", roundDur(age), zone)
		r.Key, r.Args, r.ZoneAssumed = "fresh_recent", []any{Age(age)}, !in.ZoneKnown
	default:
		if age < 0 {
			age = 0
		}
		r.Outcome, r.Factor = Pass, 1
		r.Reason = fmt.Sprintf("taken %s ago%s", roundDur(age), zone)
		r.Key, r.Args, r.ZoneAssumed = "fresh_ok", []any{Age(age)}, !in.ZoneKnown
	}
	return r, taken, "exif"
}

func (v *Verifier) source(in Input) RuleResult {
	r := RuleResult{Rule: "source", Outcome: Pass, Factor: 1}
	switch {
	case in.Source == "voice":
		r.Outcome, r.Factor = Lower, 0.6
		r.Reason = "a spoken depth is a rough estimate, so it counts for less than a measured photo"
		r.Key = "src_voice"
	case in.Method == "model_estimate":
		r.Outcome, r.Factor = Lower, 0.7
		r.Reason = "no wheel could be measured, so this is the scene model's visual estimate; it counts for less than a measurement"
		r.Key = "src_estimate"
	case in.Method == "wheel_geometry" && in.Source == "video":
		r.Reason = "video measured from a wheel of known size"
		r.Key = "src_video_geo"
	case in.Method == "wheel_geometry":
		r.Reason = "photo measured from a wheel of known size"
		r.Key = "src_photo_geo"
	case in.Source == "video":
		r.Reason = "video measurement"
		r.Key = "src_video"
	default:
		r.Reason = "photo measurement"
		r.Key = "src_photo"
	}
	return r
}

func (v *Verifier) agreement(ctx context.Context, in Input) RuleResult {
	r := RuleResult{Rule: "neighbour_agreement"}
	if v.Cells == nil {
		r.Outcome, r.Factor, r.Reason = Skip, 1, "no map to compare against"
		r.Key = "near_no_map"
		return r
	}
	cells, err := v.Cells.Nearby(ctx, in.Lat, in.Lon, v.T.NearbyRadiusM)
	if err != nil {
		r.Outcome, r.Factor = Skip, 1
		r.Reason = "the map could not be read for comparison"
		r.Key = "near_unreadable"
		return r
	}
	if len(cells) == 0 {
		r.Outcome, r.Factor = Skip, 1
		r.Reason = fmt.Sprintf("no recent real reports within %.0f m to compare with", v.T.NearbyRadiusM)
		r.Key, r.Args = "near_none", []any{v.T.NearbyRadiusM}
		return r
	}
	// Inverse-variance mean of nearby cells, where each cell's variance
	// includes how much depth can differ over that distance.
	var wsum, mean float64
	for _, c := range cells {
		spatial := v.T.SpatialSigmaPer100m * c.DistanceM / 100
		varc := c.DepthSigmaCm*c.DepthSigmaCm + spatial*spatial
		if varc <= 0 {
			varc = 1
		}
		w := 1 / varc
		wsum += w
		mean += w * c.DepthCm
	}
	mean /= wsum
	sd := math.Sqrt(in.DepthSigmaCm*in.DepthSigmaCm + 1/wsum)
	z := math.Abs(in.DepthCm-mean) / sd
	n := len(cells)
	switch {
	case z > 3:
		r.Outcome, r.Factor = Lower, 0.4
		r.Reason = fmt.Sprintf("disagrees strongly with %d nearby %s (%.0f cm expected, %.1f standard deviations away); trust lowered, not rejected, because water can change fast", n, plural(n, "cell", "cells"), mean, z)
		r.Key, r.Args = "near_strong", []any{n, mean, z}
	case z > 2:
		r.Outcome, r.Factor = Lower, 0.75
		r.Reason = fmt.Sprintf("differs from %d nearby %s (%.0f cm expected, %.1f standard deviations away)", n, plural(n, "cell", "cells"), mean, z)
		r.Key, r.Args = "near_differs", []any{n, mean, z}
	default:
		r.Outcome, r.Factor = Pass, 1
		r.Reason = fmt.Sprintf("agrees with %d nearby %s (%.0f cm expected)", n, plural(n, "cell", "cells"), mean)
		r.Key, r.Args = "near_agrees", []any{n, mean}
	}
	return r
}

func (v *Verifier) rainfall(ctx context.Context, in Input, at time.Time) RuleResult {
	r := RuleResult{Rule: "rainfall_plausibility"}
	if v.Rain == nil {
		r.Outcome, r.Factor, r.Reason = Skip, 1, "rainfall check is not configured"
		r.Key = "rain_off"
		return r
	}
	s, err := v.Rain.Rain(ctx, in.Lat, in.Lon, at)
	if err != nil || s.Hours == 0 {
		r.Outcome, r.Factor = Skip, 1
		r.Reason = "rainfall data was not available"
		r.Key = "rain_unavailable"
		return r
	}
	switch {
	case s.Past48hMM >= v.T.WetRainMM:
		r.Outcome, r.Factor = Pass, 1
		r.Reason = fmt.Sprintf("%.0f mm of rain modelled here in the previous 48 hours (Open-Meteo)", s.Past48hMM)
		r.Key, r.Args = "rain_wet", []any{s.Past48hMM}
	case s.Past48hMM < v.T.DryRainMM && in.DepthCm > v.T.DeepWithoutRainCm:
		r.Outcome, r.Factor = Lower, 0.6
		r.Reason = fmt.Sprintf("almost no rain (%.1f mm) modelled here in 48 hours; deep water could still come from a reservoir release, a burst main or tides, so trust is lowered, not rejected", s.Past48hMM)
		r.Key, r.Args = "rain_dry", []any{s.Past48hMM}
	default:
		r.Outcome, r.Factor = Pass, 1
		r.Reason = fmt.Sprintf("%.1f mm of rain modelled here in the previous 48 hours (Open-Meteo)", s.Past48hMM)
		r.Key, r.Args = "rain_ok", []any{s.Past48hMM}
	}
	return r
}

func plural(n int, one, many string) string {
	if n == 1 {
		return one
	}
	return many
}

func roundDur(d time.Duration) string {
	switch {
	case d < time.Minute:
		return "under a minute"
	case d < time.Hour:
		m := int(d.Minutes())
		return fmt.Sprintf("%d %s", m, plural(m, "minute", "minutes"))
	case d < 48*time.Hour:
		h := d.Hours()
		if h < 10 {
			return fmt.Sprintf("%.1f hours", h)
		}
		return fmt.Sprintf("%.0f hours", h)
	case d < 2*365*24*time.Hour:
		return fmt.Sprintf("%.0f days", d.Hours()/24)
	default:
		return fmt.Sprintf("about %.0f years", d.Hours()/24/365.25)
	}
}
