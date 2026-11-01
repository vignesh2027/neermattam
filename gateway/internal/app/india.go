package app

import (
	"context"
	"net/http"
	"net/url"
	"sync"
	"time"

	"neermattam/gateway/internal/live"
	"neermattam/gateway/internal/photos"
	"neermattam/gateway/internal/upstream"
)

// IndiaDoc is GET /api/india: every city's real, current state in one
// answer. Only real data: live reports, Open-Meteo rainfall, the measured
// archival photos and OpenStreetMap's critical sites. The simulated replay
// never appears here. A number that is not known is null, never zero.
type IndiaDoc struct {
	GeneratedAt    time.Time    `json:"generated_at"`
	Cities         []IndiaCity  `json:"cities"`
	MeasuredPhotos []IndiaPhoto `json:"measured_photos"`
}

// IndiaCity is one city's line.
type IndiaCity struct {
	ID   string  `json:"id"`
	Name string  `json:"name"`
	Lat  float64 `json:"lat"`
	Lon  float64 `json:"lon"`
	// Modelled rainfall from Open-Meteo, read every 10 minutes; null when it
	// could not be read (the page then says "rain data unavailable").
	RainMM1h   *float64   `json:"rain_mm_1h"`
	RainMM24h  *float64   `json:"rain_mm_24h"`
	RainAt     *time.Time `json:"rain_at"`
	RainSource string     `json:"rain_source"`
	// Real reports the gateway fused into the live map in the last 24 hours.
	RealReports24h int `json:"real_reports_24h"`
	// Live cells with evidence in force, and the deepest mean depth among
	// those the engine rates known; null when the engine did not answer
	// (deepest_cm also when no cell is known).
	CellsWithEvidence *int     `json:"cells_with_evidence"`
	DeepestCm         *float64 `json:"deepest_cm"`
	MeasuredPhotos    int      `json:"measured_photos"`
	// OpenStreetMap data centres, hospitals, telephone exchanges and cable
	// landing stations in the city; null when the engine did not answer.
	CriticalSites *int `json:"critical_sites"`
}

// IndiaPhoto is one measured archival photo.
type IndiaPhoto struct {
	ID        string   `json:"id"` // perception's measurement id
	City      string   `json:"city"`
	Place     string   `json:"place"`
	PhotoDate string   `json:"photo_date"`
	DepthCm   *float64 `json:"depth_cm"`
	SigmaCm   *float64 `json:"sigma_cm"`
	// Method is wheel_geometry, person_reference, model_estimate (the scene
	// model's labelled estimate) or not_measured.
	Method    string `json:"method"`
	ThumbURL  string `json:"thumb_url"`
	VerifyURL string `json:"verify_url"`
	Credit    string `json:"credit"`
	SourceURL string `json:"source_url"`
}

// indiaTTL bounds how often /api/india and the map's city strip ask the
// engine for every city's cells.
const indiaTTL = 15 * time.Second

type indiaCache struct {
	mu    sync.Mutex
	at    time.Time
	doc   *IndiaDoc
	sites map[string]int
	siteT map[string]time.Time
}

// India builds (or returns the cached) document.
func (a *App) India(ctx context.Context) *IndiaDoc {
	c := &a.india
	c.mu.Lock()
	if c.sites == nil {
		c.sites, c.siteT = map[string]int{}, map[string]time.Time{}
	}
	defer c.mu.Unlock()
	if c.doc != nil && time.Since(c.at) < indiaTTL {
		return c.doc
	}
	ctx, cancel := context.WithTimeout(context.WithoutCancel(ctx), 4*time.Second)
	defer cancel()
	doc := a.indiaBase(time.Now())
	all := a.Cities.All()
	var wg sync.WaitGroup
	var smu sync.Mutex
	for i, city := range all {
		if i >= len(doc.Cities) || doc.Cities[i].ID != city.ID {
			continue // the city list changed between the two reads
		}
		wg.Add(2)
		go func(i int, id string) {
			defer wg.Done()
			n, deepest, ok := a.liveStats(ctx, id)
			if !ok {
				return
			}
			doc.Cities[i].CellsWithEvidence = &n
			doc.Cities[i].DeepestCm = deepest
		}(i, city.ID)
		go func(i int, id string) {
			defer wg.Done()
			smu.Lock()
			n, fresh := c.sites[id]
			fresh = fresh && time.Since(c.siteT[id]) < 30*time.Minute
			smu.Unlock()
			if !fresh {
				list, err := a.Engine.Sites(ctx, url.Values{"city": {id}, "kind": {"data_centre"}, "limit": {"1"}})
				if err != nil {
					smu.Lock()
					_, held := c.sites[id]
					smu.Unlock()
					if !held {
						return
					}
				} else {
					n = 0
					for _, k := range list.Counts {
						n += k.OSM
					}
					smu.Lock()
					c.sites[id], c.siteT[id] = n, time.Now()
					smu.Unlock()
				}
			}
			doc.Cities[i].CriticalSites = &n
		}(i, city.ID)
	}
	wg.Wait()
	c.doc, c.at = doc, time.Now()
	return doc
}

// indiaBase is the part of the document held in memory: rainfall, real
// reports and the photos. The engine's parts are left null.
func (a *App) indiaBase(now time.Time) *IndiaDoc {
	all := a.Cities.All()
	doc := &IndiaDoc{GeneratedAt: now.UTC(), Cities: make([]IndiaCity, len(all)), MeasuredPhotos: []IndiaPhoto{}}
	reports := map[string]int{}
	for _, r := range a.Origins.Records() {
		if live.IsSimulatedID(r.ReadingID) || now.Sub(r.ReceivedAt) > 24*time.Hour {
			continue
		}
		if city, ok := a.Cities.Find(r.Lat, r.Lon); ok {
			reports[city.ID]++
		}
	}
	for i, city := range all {
		ic := IndiaCity{
			ID: city.ID, Name: city.Name, Lat: round4(city.CentreLat), Lon: round4(city.CentreLon),
			RainSource: "Open-Meteo", RealReports24h: reports[city.ID], MeasuredPhotos: a.Photos.Count(city.ID),
		}
		if r, ok := a.Rain.Get(city.ID); ok {
			h1, h24, at := r.Last1hMM, r.Last24hMM, r.ReadAt.UTC()
			ic.RainMM1h, ic.RainMM24h, ic.RainAt = &h1, &h24, &at
		}
		doc.Cities[i] = ic
	}
	for _, p := range a.Photos.List {
		doc.MeasuredPhotos = append(doc.MeasuredPhotos, a.indiaPhoto(p))
	}
	return doc
}

// indiaCached never keeps a page waiting on the engine: it returns the
// document /api/india built in the last two minutes, else the in-memory
// part alone (rainfall, real reports and photos), and asks for a fresh one
// in the background.
func (a *App) indiaCached() *IndiaDoc {
	c := &a.india
	if c.mu.TryLock() {
		doc, age := c.doc, time.Since(c.at)
		c.mu.Unlock()
		if doc != nil && age < 2*time.Minute {
			return doc
		}
	}
	return a.indiaBase(time.Now())
}

func (a *App) indiaPhoto(p photos.Photo) IndiaPhoto {
	m := p.Measurement
	out := IndiaPhoto{
		ID: p.MeasurementID(), City: p.City, Place: p.Place, PhotoDate: p.PhotoDate,
		Method: p.Method(), ThumbURL: a.Renderer.Assets().URL("photos/" + p.ID + ".jpg"),
		VerifyURL: "/verify/" + p.MeasurementID(), Credit: p.Credit(), SourceURL: p.SourceURL,
	}
	if out.Method == "" {
		out.Method = "not_measured"
	} else {
		out.DepthCm, out.SigmaCm = m.DepthCm, m.DepthSigmaCm
	}
	return out
}

// liveStats counts a city's live cells with evidence in force and the
// deepest known mean depth. Cells labelled simulated (left from an older
// deployment that mixed the scenario into the live map) are not counted.
func (a *App) liveStats(ctx context.Context, city string) (int, *float64, bool) {
	e, err := a.cellsForCity(ctx, city)
	if err != nil {
		return 0, nil, false
	}
	cells, err := upstream.DecodeCells(e.body)
	if err != nil {
		return 0, nil, false
	}
	n, known := 0, false
	deepest := 0.0
	for _, c := range cells {
		if c.Props.Expired || c.Props.Origin == live.OriginSimulated {
			continue
		}
		n++
		if c.Props.Unknown {
			continue
		}
		if !known || c.Props.DepthCm > deepest {
			deepest, known = c.Props.DepthCm, true
		}
	}
	if !known {
		return n, nil, true
	}
	d := round1(deepest)
	return n, &d, true
}

// apiIndia is GET /api/india.
func (a *App) apiIndia(w http.ResponseWriter, r *http.Request) {
	if !a.limit(w, r, a.apiLimit, "api") {
		return
	}
	w.Header().Set("Cache-Control", "public, max-age=15")
	w.Header().Set("Access-Control-Allow-Origin", "*")
	writeJSON(w, http.StatusOK, a.India(r.Context()))
}

func round1(x float64) float64 { return float64(int64(x*10+0.5*sign(x))) / 10 }
func round4(x float64) float64 { return float64(int64(x*1e4+0.5*sign(x))) / 1e4 }

func sign(x float64) float64 {
	if x < 0 {
		return -1
	}
	return 1
}
