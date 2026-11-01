package verify

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math"
	"net/http"
	"net/url"
	"strconv"
	"sync"
	"time"

	"neermattam/gateway/internal/metrics"
)

// RainSummary is modelled rainfall before an observation.
type RainSummary struct {
	Past48hMM float64
	Past6hMM  float64
	Hours     int // hourly values that fell inside the 48-hour window
}

// OpenMeteo reads hourly precipitation from the Open-Meteo forecast API,
// which also serves the past few days. No key is needed. Its terms allow
// free non-commercial use under 10,000 calls a day with attribution
// (CC BY 4.0); the cache keeps the gateway far below that, because a series
// covers a 0.05 degree square (about 5 km) for 30 minutes.
type OpenMeteo struct {
	base    string
	client  *http.Client
	timeout time.Duration
	ttl     time.Duration
	now     func() time.Time
	metrics *metrics.Set

	mu    sync.Mutex
	cache map[string]rainEntry
}

type rainEntry struct {
	fetched time.Time
	hours   []time.Time
	mm      []float64
}

// NewOpenMeteo returns a client. base is normally https://api.open-meteo.com.
func NewOpenMeteo(base string, timeout time.Duration, m *metrics.Set) *OpenMeteo {
	return &OpenMeteo{
		base:    base,
		client:  &http.Client{Timeout: timeout},
		timeout: timeout,
		ttl:     30 * time.Minute,
		now:     time.Now,
		metrics: m,
		cache:   map[string]rainEntry{},
	}
}

func gridKey(lat, lon float64) (string, float64, float64) {
	glat := math.Round(lat*20) / 20
	glon := math.Round(lon*20) / 20
	return fmt.Sprintf("%.2f,%.2f", glat, glon), glat, glon
}

// Rain sums modelled precipitation in the 48 and 6 hours up to `at`.
func (o *OpenMeteo) Rain(ctx context.Context, lat, lon float64, at time.Time) (RainSummary, error) {
	key, glat, glon := gridKey(lat, lon)
	o.mu.Lock()
	e, ok := o.cache[key]
	o.mu.Unlock()
	if !ok || o.now().Sub(e.fetched) > o.ttl {
		var err error
		e, err = o.fetch(ctx, glat, glon)
		if err != nil {
			return RainSummary{}, err
		}
		o.mu.Lock()
		if len(o.cache) > 512 {
			for k, v := range o.cache {
				if o.now().Sub(v.fetched) > o.ttl {
					delete(o.cache, k)
				}
			}
		}
		o.cache[key] = e
		o.mu.Unlock()
	} else if o.metrics != nil {
		o.metrics.Upstream.Inc("open_meteo", "cache_hit")
	}
	return summarise(e, at), nil
}

// RecentRain is modelled rainfall for the hours up to now, and when it was
// read from Open-Meteo.
type RecentRain struct {
	Last1hMM  float64   // the latest complete hour
	Last24hMM float64   // the 24 hours up to now
	Hours     int       // hourly values in the 24-hour window
	HourEnd   time.Time // end of the latest complete hour
	ReadAt    time.Time // when the series was fetched
}

// Recent reads rainfall in the last hour and the last 24 hours at a point,
// from a series no older than maxAge (it is fetched again when older).
func (o *OpenMeteo) Recent(ctx context.Context, lat, lon float64, maxAge time.Duration) (RecentRain, error) {
	key, glat, glon := gridKey(lat, lon)
	o.mu.Lock()
	e, ok := o.cache[key]
	o.mu.Unlock()
	if !ok || o.now().Sub(e.fetched) > maxAge {
		var err error
		e, err = o.fetch(ctx, glat, glon)
		if err != nil {
			return RecentRain{}, err
		}
		o.mu.Lock()
		o.cache[key] = e
		o.mu.Unlock()
	}
	return recent(e, o.now())
}

// recent sums the hourly values in (now-24h, now]. Each value is the total
// of the hour before its time stamp.
func recent(e rainEntry, now time.Time) (RecentRain, error) {
	r := RecentRain{ReadAt: e.fetched}
	for i, t := range e.hours {
		if t.After(now) || !t.After(now.Add(-24*time.Hour)) {
			continue
		}
		r.Last24hMM += e.mm[i]
		r.Hours++
		if t.After(r.HourEnd) {
			r.HourEnd, r.Last1hMM = t, e.mm[i]
		}
	}
	if r.Hours < 20 {
		// Too little of the day is covered to give a 24-hour total.
		return RecentRain{}, errors.New("open-meteo returned too few recent hours")
	}
	r.Last1hMM = math.Round(r.Last1hMM*10) / 10
	r.Last24hMM = math.Round(r.Last24hMM*10) / 10
	return r, nil
}

// summarise adds the hourly values in (at-48h, at]. Open-Meteo's hourly
// precipitation is the total of the preceding hour, so the value stamped
// 10:00 covers 09:00 to 10:00.
func summarise(e rainEntry, at time.Time) RainSummary {
	var s RainSummary
	for i, t := range e.hours {
		if t.After(at) || !t.After(at.Add(-48*time.Hour)) {
			continue
		}
		s.Past48hMM += e.mm[i]
		s.Hours++
		if t.After(at.Add(-6 * time.Hour)) {
			s.Past6hMM += e.mm[i]
		}
	}
	return s
}

func (o *OpenMeteo) fetch(ctx context.Context, lat, lon float64) (rainEntry, error) {
	ctx, cancel := context.WithTimeout(ctx, o.timeout)
	defer cancel()
	q := url.Values{
		"latitude":      {strconv.FormatFloat(lat, 'f', 2, 64)},
		"longitude":     {strconv.FormatFloat(lon, 'f', 2, 64)},
		"hourly":        {"precipitation"},
		"past_days":     {"3"},
		"forecast_days": {"1"},
		"timezone":      {"GMT"},
	}
	start := time.Now()
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, o.base+"/v1/forecast?"+q.Encode(), nil)
	if err != nil {
		return rainEntry{}, err
	}
	req.Header.Set("User-Agent", "neermattam-gateway (flood report verification)")
	resp, err := o.client.Do(req)
	if err != nil {
		o.observe(start, "unreachable")
		return rainEntry{}, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		o.observe(start, fmt.Sprintf("http_%d", resp.StatusCode))
		return rainEntry{}, fmt.Errorf("open-meteo returned %d", resp.StatusCode)
	}
	var body struct {
		Hourly struct {
			Time          []string   `json:"time"`
			Precipitation []*float64 `json:"precipitation"`
		} `json:"hourly"`
	}
	if err := json.NewDecoder(io.LimitReader(resp.Body, 1<<20)).Decode(&body); err != nil {
		o.observe(start, "bad_body")
		return rainEntry{}, err
	}
	if len(body.Hourly.Time) != len(body.Hourly.Precipitation) || len(body.Hourly.Time) == 0 {
		o.observe(start, "bad_body")
		return rainEntry{}, errors.New("open-meteo returned no hourly precipitation")
	}
	e := rainEntry{fetched: o.now()}
	for i, ts := range body.Hourly.Time {
		t, err := time.Parse("2006-01-02T15:04", ts)
		if err != nil || body.Hourly.Precipitation[i] == nil {
			continue
		}
		e.hours = append(e.hours, t)
		e.mm = append(e.mm, *body.Hourly.Precipitation[i])
	}
	o.observe(start, "ok")
	return e, nil
}

func (o *OpenMeteo) observe(start time.Time, outcome string) {
	if o.metrics == nil {
		return
	}
	o.metrics.Upstream.Inc("open_meteo", outcome)
	o.metrics.UpstreamDuration.Observe(time.Since(start).Seconds(), "open_meteo")
}
