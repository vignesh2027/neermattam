package app

import (
	"context"
	"errors"
	"sync"
	"time"

	"neermattam/gateway/internal/cities"
	"neermattam/gateway/internal/verify"
)

// RecentRainSource reads recent modelled rainfall at a point. Open-Meteo is
// the real one (verify.OpenMeteo); tests pass a fake.
type RecentRainSource interface {
	Recent(ctx context.Context, lat, lon float64, maxAge time.Duration) (verify.RecentRain, error)
}

// rainUnavailable is a source that is never available.
type rainUnavailable struct{}

func (rainUnavailable) Recent(context.Context, float64, float64, time.Duration) (verify.RecentRain, error) {
	return verify.RecentRain{}, errNoRain
}

var errNoRain = errors.New("no rainfall source configured")

const (
	// rainEvery is how often each city's rainfall is read again. The page
	// says when it was read; it is never called real time.
	rainEvery = 10 * time.Minute
	// rainStale is how old a reading may be and still be shown, with its
	// time, while Open-Meteo is failing. Older, the page says "rain data
	// unavailable", never zero.
	rainStale = 90 * time.Minute
)

// cityRain keeps the latest rainfall per city, refreshed in the background,
// so no page waits for Open-Meteo.
type cityRain struct {
	src    RecentRainSource
	cities *cities.Registry
	now    func() time.Time

	mu   sync.RWMutex
	last map[string]verify.RecentRain
	err  map[string]string
}

func newCityRain(src RecentRainSource, reg *cities.Registry) *cityRain {
	return &cityRain{src: src, cities: reg, now: time.Now, last: map[string]verify.RecentRain{}, err: map[string]string{}}
}

// Run reads every city at once and then every rainEvery until ctx ends.
func (c *cityRain) Run(ctx context.Context) {
	c.Refresh(ctx)
	t := time.NewTicker(rainEvery)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-t.C:
			c.Refresh(ctx)
		}
	}
}

// Refresh reads every city's rainfall in parallel. A failure keeps the
// previous reading, which Get stops showing once it is older than rainStale.
func (c *cityRain) Refresh(ctx context.Context) {
	var wg sync.WaitGroup
	for _, city := range c.cities.All() {
		wg.Add(1)
		go func(city cities.City) {
			defer wg.Done()
			rctx, cancel := context.WithTimeout(ctx, 15*time.Second)
			defer cancel()
			r, err := c.src.Recent(rctx, city.CentreLat, city.CentreLon, rainEvery)
			c.mu.Lock()
			defer c.mu.Unlock()
			if err != nil {
				c.err[city.ID] = err.Error()
				return
			}
			delete(c.err, city.ID)
			c.last[city.ID] = r
		}(city)
	}
	wg.Wait()
}

// Get returns a city's rainfall when a reading no older than rainStale is
// held.
func (c *cityRain) Get(city string) (verify.RecentRain, bool) {
	c.mu.RLock()
	defer c.mu.RUnlock()
	r, ok := c.last[city]
	if !ok || c.now().Sub(r.ReadAt) > rainStale {
		return verify.RecentRain{}, false
	}
	return r, true
}
