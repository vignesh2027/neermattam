// Package demo seeds the engine with a SIMULATED flood scenario so the map
// is alive before any real report arrives. The places are low points that
// flooded during Cyclone Michaung in December 2023 (Velachery, Pallikaranai,
// T. Nagar and the Mambalam subways); every depth here is invented for the
// demonstration. Simulated readings carry reading ids starting with "sim_",
// every cell they touch is labelled "simulated" by the gateway, and the UI
// says "Simulated scenario" wherever they appear.
package demo

import (
	"context"
	crand "crypto/rand"
	"encoding/binary"
	"fmt"
	"log/slog"
	"math"
	"math/rand/v2"
	"sync"
	"time"

	"neermattam/gateway/internal/live"
	"neermattam/gateway/internal/report"
	"neermattam/gateway/internal/upstream"
)

// Hotspot is one simulated pool of water.
type Hotspot struct {
	Name     string
	Lat, Lon float64
	PeakCm   float64
	RadiusM  float64
	// FlowMps is a simulated surface speed near the centre, or 0 when the
	// water is treated as standing (velocity not reported).
	FlowMps float64
}

// Scenario is the simulated Chennai flood.
var Scenario = []Hotspot{
	{"Ram Nagar, Velachery", 12.9768, 80.2152, 78, 650, 0},
	{"Velachery Main Road", 12.9822, 80.2216, 46, 420, 0.5},
	{"Vijayanagar, Velachery", 12.9725, 80.2235, 38, 380, 0},
	{"Pallikaranai, Narayanapuram", 12.9455, 80.2128, 88, 800, 0},
	{"Pallikaranai Main Road", 12.9355, 80.2045, 52, 500, 0.3},
	{"Madley Road subway, T. Nagar", 13.0378, 80.2287, 96, 230, 0.6},
	{"Usman Road, T. Nagar", 13.0405, 80.2335, 34, 420, 0},
	{"Arya Gowda Road, West Mambalam", 13.0352, 80.2222, 44, 420, 0},
	{"Saidapet, by the Adyar", 13.0228, 80.2252, 48, 350, 0.9},
	{"Madipakkam", 12.9640, 80.1990, 50, 550, 0},
	{"Ashok Nagar", 13.0350, 80.2110, 28, 450, 0},
	{"Perungudi", 12.9625, 80.2425, 34, 450, 0},
}

// dryPoints are simulated reports of a dry or barely wet street, so the
// scenario also shows known shallow cells, not only deep ones.
var dryPoints = [][2]float64{
	{13.0067, 80.2206}, {13.0850, 80.2101}, {13.0569, 80.2425}, {13.0067, 80.2570},
	{13.0732, 80.2609}, {13.0500, 80.2121}, {12.9850, 80.2440}, {13.0180, 80.2420},
}

// vaguePoints are simulated reports with wide uncertainty. They leave cells
// the engine rates unknown, so the scenario shows the hatched state too.
var vaguePoints = [][2]float64{
	{13.1240, 80.2120}, {13.1210, 80.2330}, {13.0382, 80.1565}, {12.9171, 80.1923}, {12.9516, 80.1462},
}

// Seeder posts the scenario and keeps it alive.
type Seeder struct {
	Engine  *upstream.Engine
	Origins *live.Origins
	Hub     *live.Hub
	Log     *slog.Logger
	Drip    time.Duration
	Reseed  time.Duration

	mu    sync.Mutex
	rng   *rand.Rand
	start time.Time

	// salt makes reading ids unique to this process. The scenario's shape
	// comes from a fixed seed so it looks the same every run, but the engine
	// drops a reading id it has seen, so without the salt a restarted
	// gateway would replay ids the engine already holds and the map would
	// stop moving.
	salt     uint64
	saltOnce sync.Once
}

func (s *Seeder) id() string {
	s.saltOnce.Do(func() {
		var b [8]byte
		if _, err := crand.Read(b[:]); err == nil {
			s.salt = binary.LittleEndian.Uint64(b[:])
		} else {
			s.salt = uint64(time.Now().UnixNano())
		}
	})
	return fmt.Sprintf("%s%x", live.SimulatedPrefix, s.rng.Uint64()^s.salt)
}

// reading builds one simulated reading at a point near hotspot h.
func (s *Seeder) reading(h Hotspot, lat, lon float64, scale float64, observed time.Time) (upstream.Reading, bool) {
	d := live.DistanceM(lat, lon, h.Lat, h.Lon)
	base := h.PeakCm * scale * math.Exp(-1.6*(d/h.RadiusM)*(d/h.RadiusM))
	depth := base + s.rng.NormFloat64()*3
	if depth < 3 {
		return upstream.Reading{}, false
	}
	depth = math.Round(depth*10) / 10
	sigma := math.Round((3+0.07*depth+s.rng.Float64()*1.5)*10) / 10
	r := upstream.Reading{
		ReadingID:    s.id(),
		Lat:          lat,
		Lon:          lon,
		DepthCm:      depth,
		DepthSigmaCm: sigma,
		ObservedAt:   observed.UTC().Truncate(time.Second),
		Trust:        0.9,
	}
	if h.FlowMps > 0 && d < h.RadiusM*0.8 {
		v := math.Round(math.Max(0.05, h.FlowMps*(1-d/h.RadiusM)+s.rng.NormFloat64()*0.1)*100) / 100
		vs := 0.25
		r.VelocityMps, r.VelocitySigmaMps = &v, &vs
	}
	return r, true
}

// offset moves a point by metres east and north.
func offset(lat, lon, east, north float64) (float64, float64) {
	return lat + north/111_320, lon + east/(111_320*math.Cos(lat*math.Pi/180))
}

// Plan returns the full scenario's readings. Points sit on a jittered grid
// about 140 m apart, close to the spacing of H3 resolution 10 cells.
func (s *Seeder) Plan(now time.Time) []upstream.Reading {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.rng == nil {
		s.rng = rand.New(rand.NewPCG(2023, 12))
	}
	const step = 140.0
	var out []upstream.Reading
	for _, h := range Scenario {
		n := int(h.RadiusM*1.3/step) + 1
		for i := -n; i <= n; i++ {
			for j := -n; j <= n; j++ {
				east := float64(i)*step + float64(j%2)*step/2 + s.rng.NormFloat64()*25
				north := float64(j)*step*0.87 + s.rng.NormFloat64()*25
				if math.Hypot(east, north) > h.RadiusM*1.3 {
					continue
				}
				lat, lon := offset(h.Lat, h.Lon, east, north)
				observed := now.Add(-time.Duration(5+s.rng.IntN(70)) * time.Minute)
				if r, ok := s.reading(h, lat, lon, 1, observed); ok {
					out = append(out, r)
				}
			}
		}
	}
	for _, p := range dryPoints {
		for k := 0; k < 3; k++ {
			lat, lon := offset(p[0], p[1], s.rng.NormFloat64()*120, s.rng.NormFloat64()*120)
			depth := math.Round(s.rng.Float64()*30) / 10
			out = append(out, upstream.Reading{
				ReadingID: s.id(), Lat: lat, Lon: lon,
				DepthCm: depth, DepthSigmaCm: 2.5, ObservedAt: now.Add(-time.Duration(10+s.rng.IntN(50)) * time.Minute).UTC().Truncate(time.Second),
				Trust: 0.9,
			})
		}
	}
	for _, p := range vaguePoints {
		out = append(out, upstream.Reading{
			ReadingID: s.id(), Lat: p[0], Lon: p[1],
			DepthCm: math.Round(10 + s.rng.Float64()*20), DepthSigmaCm: 14,
			ObservedAt: now.Add(-time.Duration(20+s.rng.IntN(60)) * time.Minute).UTC().Truncate(time.Second),
			Trust:      0.9,
		})
	}
	return out
}

// Seed posts the whole scenario with a few workers, then labels every cell
// it produced as simulated.
func (s *Seeder) Seed(ctx context.Context, area string) (int, error) {
	readings := s.Plan(time.Now())
	jobs := make(chan upstream.Reading)
	var wg sync.WaitGroup
	var mu sync.Mutex
	var posted int
	var firstErr error
	for w := 0; w < 6; w++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for r := range jobs {
				s.Origins.Expect(r.Lat, r.Lon, true)
				res, err := s.Engine.PostReading(ctx, r)
				mu.Lock()
				if err != nil {
					if firstErr == nil {
						firstErr = err
					}
				} else {
					posted++
					s.Origins.Mark(res.Cell, true)
				}
				mu.Unlock()
			}
		}()
	}
	for _, r := range readings {
		select {
		case jobs <- r:
		case <-ctx.Done():
		}
	}
	close(jobs)
	wg.Wait()
	// Label the cells these readings touched (each reading's own cell and
	// its ring of six), found by distance from one query of the whole map.
	// Cells real reports touched keep their real label too, so they read
	// "mixed", never "simulated".
	if raw, err := s.Engine.Cells(ctx, area); err == nil {
		if cells, err := upstream.DecodeCells(raw); err == nil {
			centres := make([]live.CellCentre, len(cells))
			for i, c := range cells {
				centres[i] = live.CellCentre{ID: c.ID, Lat: c.Lat, Lon: c.Lon}
			}
			points := make([][2]float64, len(readings))
			for i, r := range readings {
				points[i] = [2]float64{r.Lat, r.Lon}
			}
			s.Origins.MarkAround(centres, points, true)
		}
	}
	if posted == 0 && firstErr != nil {
		return 0, firstErr
	}
	return posted, nil
}

// Run seeds once the engine accepts readings, then drips a simulated reading
// every Drip interval and reseeds every Reseed interval so the scenario does
// not decay away during a long demo.
func (s *Seeder) Run(ctx context.Context, area string) {
	s.start = time.Now()
	for {
		n, err := s.Seed(ctx, area)
		if err == nil {
			s.Log.Info("simulated scenario seeded", "readings", n)
			break
		}
		s.Log.Warn("simulated scenario not seeded yet; retrying", "error", err.Error())
		select {
		case <-ctx.Done():
			return
		case <-time.After(3 * time.Second):
		}
	}
	drip := time.NewTicker(s.Drip)
	defer drip.Stop()
	reseed := time.NewTicker(s.Reseed)
	defer reseed.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-reseed.C:
			if n, err := s.Seed(ctx, area); err != nil {
				s.Log.Warn("simulated reseed failed", "error", err.Error())
			} else {
				s.Log.Info("simulated scenario refreshed", "readings", n)
			}
		case <-drip.C:
			s.dripOne(ctx)
		}
	}
}

// dripOne posts one simulated reading. The water rises and falls slowly
// (a 40 minute cycle) so the live map visibly changes during a demo.
func (s *Seeder) dripOne(ctx context.Context) {
	s.mu.Lock()
	if s.rng == nil {
		s.rng = rand.New(rand.NewPCG(2023, 12))
	}
	h := Scenario[s.rng.IntN(len(Scenario))]
	phase := time.Since(s.start).Minutes() / 40 * 2 * math.Pi
	scale := 1 + 0.18*math.Sin(phase)
	ang := s.rng.Float64() * 2 * math.Pi
	dist := math.Sqrt(s.rng.Float64()) * h.RadiusM * 0.8
	lat, lon := offset(h.Lat, h.Lon, dist*math.Cos(ang), dist*math.Sin(ang))
	r, ok := s.reading(h, lat, lon, scale, time.Now())
	s.mu.Unlock()
	if !ok {
		return
	}
	s.Origins.Expect(lat, lon, true)
	res, err := s.Engine.PostReading(ctx, r)
	if err != nil {
		s.Log.Warn("simulated reading failed", "error", err.Error())
		return
	}
	s.Origins.Mark(res.Cell, true)
	report.MarkNearby(ctx, s.Engine, s.Origins, lat, lon, true)
	s.Hub.PublishFeed(live.FeedItem{
		Simulated: true, Source: "simulated", DepthCm: r.DepthCm, DepthSigmaCm: r.DepthSigmaCm,
		Place: h.Name, Lat: lat, Lon: lon, At: r.ObservedAt,
	})
}
