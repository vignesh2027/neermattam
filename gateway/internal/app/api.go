package app

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math"
	"mime"
	"neermattam/gateway/internal/cities"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"sync"
	"time"

	"neermattam/gateway/internal/config"
	"neermattam/gateway/internal/live"
	"neermattam/gateway/internal/overlay"
	"neermattam/gateway/internal/report"
	"neermattam/gateway/internal/upstream"
	"neermattam/gateway/internal/web"
)

var hintReferences = map[string]bool{
	"two_wheeler_wheel": true, "car_wheel": true, "auto_wheel": true, "kerb": true, "person": true,
}

// parseSubmission streams the multipart body, holding at most the largest
// allowed file in memory, and never trusts the client's Content-Type.
func (a *App) parseSubmission(w http.ResponseWriter, r *http.Request, channel string) (report.Submission, *report.Error) {
	sub := report.Submission{Channel: channel}
	maxFile := max(a.Cfg.MaxImageBytes, a.Cfg.MaxVideoBytes, a.Cfg.MaxAudioBytes)
	r.Body = http.MaxBytesReader(w, r.Body, maxFile+(2<<20))
	ct, _, _ := mime.ParseMediaType(r.Header.Get("Content-Type"))
	if ct != "multipart/form-data" {
		return sub, &report.Error{Status: http.StatusBadRequest, Code: "not_multipart", Key: "not_multipart", Message: "Send the report as multipart/form-data with a file and lat and lon fields."}
	}
	mr, err := r.MultipartReader()
	if err != nil {
		return sub, &report.Error{Status: http.StatusBadRequest, Code: "bad_multipart", Key: "bad_multipart", Message: "The upload could not be read."}
	}
	var latS, lonS, sampleID string
	for parts := 0; ; parts++ {
		if parts > 12 {
			return sub, &report.Error{Status: http.StatusBadRequest, Code: "too_many_parts", Key: "too_many_parts", Message: "The form has too many parts."}
		}
		part, err := mr.NextPart()
		if err == io.EOF {
			break
		}
		if err != nil {
			return sub, tooLargeOr(err, "The upload could not be read.")
		}
		name := part.FormName()
		if part.FileName() != "" || name == "image" || name == "video" || name == "audio" || name == "media" {
			data, err := io.ReadAll(io.LimitReader(part, maxFile+1))
			part.Close()
			if err != nil {
				return sub, tooLargeOr(err, "The file could not be read.")
			}
			if int64(len(data)) > maxFile {
				return sub, &report.Error{Status: http.StatusRequestEntityTooLarge, Code: "too_large", Key: "too_large_file", Args: []any{maxFile >> 20}, Message: fmt.Sprintf("Files can be up to %d MB.", maxFile>>20)}
			}
			if len(data) > 0 {
				sub.Files = append(sub.Files, report.File{Field: name, Filename: part.FileName(), Data: data})
			}
			continue
		}
		val, err := io.ReadAll(io.LimitReader(part, 1024))
		part.Close()
		if err != nil {
			return sub, tooLargeOr(err, "The form could not be read.")
		}
		v := strings.TrimSpace(string(val))
		switch name {
		case "lat":
			latS = v
		case "lon", "lng":
			lonS = v
		case "location":
			latS, lonS, _ = strings.Cut(v, ",")
		case "city":
			sub.City = strings.ToLower(v)
		case "sample":
			sampleID = v
		case "hint_reference":
			if v != "" {
				if !hintReferences[v] {
					return sub, &report.Error{Status: http.StatusBadRequest, Code: "bad_hint", Key: "bad_hint_ref", Message: "hint_reference must be two_wheeler_wheel, car_wheel, auto_wheel, kerb or person."}
				}
				sub.HintReference = v
			}
		case "hint_diameter_cm":
			if v != "" {
				d, err := strconv.ParseFloat(v, 64)
				if err != nil || d < 10 || d > 150 {
					return sub, &report.Error{Status: http.StatusBadRequest, Code: "bad_hint", Key: "bad_hint_diameter", Message: "The wheel diameter must be a number of centimetres between 10 and 150."}
				}
				sub.HintDiameterCm = &d
			}
		}
	}
	if latS != "" || lonS != "" {
		lat, err1 := strconv.ParseFloat(strings.TrimSpace(latS), 64)
		lon, err2 := strconv.ParseFloat(strings.TrimSpace(lonS), 64)
		if err1 != nil || err2 != nil || math.IsNaN(lat) || math.IsNaN(lon) {
			return sub, &report.Error{Status: http.StatusBadRequest, Code: "bad_location", Key: "bad_location", Message: "Latitude and longitude must be decimal numbers, such as 12.9791 and 80.2209."}
		}
		sub.Lat, sub.Lon, sub.HasLocation = lat, lon, true
	}
	if sampleID != "" {
		// One of the gateway's own sample photos (landing.go).
		if e := applySample(&sub, sampleID); e != nil {
			return sub, e
		}
	}
	return sub, nil
}

func tooLargeOr(err error, msg string) *report.Error {
	var mbe *http.MaxBytesError
	if errors.As(err, &mbe) {
		return &report.Error{Status: http.StatusRequestEntityTooLarge, Code: "too_large", Key: "too_large_upload", Message: "The upload is larger than the limit."}
	}
	return &report.Error{Status: http.StatusBadRequest, Code: "bad_multipart", Key: "bad_multipart", Message: msg}
}

func asReportError(err error) *report.Error {
	var re *report.Error
	if errors.As(err, &re) {
		return re
	}
	if errors.Is(err, context.Canceled) || errors.Is(err, context.DeadlineExceeded) {
		return &report.Error{Status: http.StatusServiceUnavailable, Code: "timeout", Key: "timeout", Message: "The report took too long to process. Please try again."}
	}
	return &report.Error{Status: http.StatusInternalServerError, Code: "internal", Key: "internal", Message: "Something went wrong while processing the report."}
}

// apiReport is POST /api/report (CONTRACTS.md section 3).
func (a *App) apiReport(w http.ResponseWriter, r *http.Request) {
	if !a.limit(w, r, a.reportLimit, "report") {
		return
	}
	sub, perr := a.parseSubmission(w, r, "api")
	if perr != nil {
		a.Metrics.Reports.Inc("api", "invalid")
		writeJSON(w, perr.Status, perr)
		return
	}
	out, err := a.Pipeline.Run(r.Context(), sub, nil)
	if err != nil {
		re := asReportError(err)
		writeJSON(w, re.Status, re)
		return
	}
	writeJSON(w, http.StatusOK, out)
}

// apiOverlay serves a report's overlay JPEG for an hour after the report.
func (a *App) apiOverlay(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	data, ok := a.Overlays.Get(id)
	if !overlay.ValidID(id) || !ok {
		writeJSON(w, http.StatusNotFound, map[string]string{"error": "this overlay has expired or never existed; overlays are kept for an hour"})
		return
	}
	w.Header().Set("Content-Type", "image/jpeg")
	w.Header().Set("Cache-Control", "private, max-age=3600, immutable")
	w.Header().Set("Content-Length", strconv.Itoa(len(data)))
	_, _ = w.Write(data)
}

// cellsCache holds compacted cell collections for a short time, with their
// gzip form and an ETag, so a burst of page loads costs the engine one
// query and a browser that already has the data gets a 304.
type cellsCache struct {
	ttl      time.Duration
	mu       sync.Mutex
	m        map[string]*cellsEntry
	inflight map[string]*cellsCall
}

type cellsEntry struct {
	body []byte
	gz   []byte
	etag string
	at   time.Time

	city        string
	frame       web.Frame
	devOnce     sync.Once
	devBody     []byte
	devGz       []byte
	previewOnce sync.Once
	preview     web.PreviewView
}

// cellsCall is one engine fetch that concurrent misses for the same key
// share, so fifty viewers arriving as the entry expires cost one query and
// one compression, not fifty.
type cellsCall struct {
	done chan struct{}
	e    *cellsEntry
	err  error
}

func newCellsCache(ttl time.Duration) *cellsCache {
	return &cellsCache{ttl: ttl, m: map[string]*cellsEntry{}, inflight: map[string]*cellsCall{}}
}

// Preview is the landing page's schematic of these cells, built once per
// snapshot.
func (e *cellsEntry) Preview() web.PreviewView {
	e.previewOnce.Do(func() {
		cells, err := upstream.DecodeCells(e.body)
		if err != nil {
			cells = nil
		}
		e.preview = web.NewPreview(cells, e.frame)
	})
	return e.preview
}

func (c *cellsCache) get(key string) (*cellsEntry, bool) {
	c.mu.Lock()
	defer c.mu.Unlock()
	e, ok := c.m[key]
	if !ok || time.Since(e.at) > c.ttl {
		return nil, false
	}
	return e, true
}

func (c *cellsCache) put(key string, e *cellsEntry) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if len(c.m) >= 128 {
		for k, old := range c.m {
			if time.Since(old.at) > c.ttl {
				delete(c.m, k)
			}
		}
		if len(c.m) >= 128 {
			c.m = map[string]*cellsEntry{}
		}
	}
	c.m[key] = e
}

// cellsFor returns compacted, labelled cells for a bbox, from the cache when
// fresh.
func (a *App) cellsFor(ctx context.Context, bbox config.BBox) (*cellsEntry, error) {
	key := fmt.Sprintf("%.4f,%.4f,%.4f,%.4f", bbox.MinLon, bbox.MinLat, bbox.MaxLon, bbox.MaxLat)
	return a.cellsCached(ctx, "bbox="+key, url.Values{"bbox": {key}}, "")
}

// cellsCached serves one engine cells query from the 2-second cache.
// Requests that miss together share one fetch.
func (a *App) cellsCached(ctx context.Context, key string, q url.Values, city string) (*cellsEntry, error) {
	if e, ok := a.cells.get(key); ok {
		a.Metrics.CellsCache.Inc("hit")
		return e, nil
	}
	a.cells.mu.Lock()
	if c, ok := a.cells.inflight[key]; ok {
		a.cells.mu.Unlock()
		a.Metrics.CellsCache.Inc("shared")
		select {
		case <-c.done:
			return c.e, c.err
		case <-ctx.Done():
			return nil, ctx.Err()
		}
	}
	c := &cellsCall{done: make(chan struct{})}
	a.cells.inflight[key] = c
	a.cells.mu.Unlock()
	a.Metrics.CellsCache.Inc("miss")

	// The shared fetch must not die with the first caller's request.
	fctx, cancel := context.WithTimeout(context.WithoutCancel(ctx), 10*time.Second)
	defer cancel()
	c.e, c.err = a.fetchCells(fctx, q, city)
	if c.err == nil {
		a.cells.put(key, c.e)
	}
	a.cells.mu.Lock()
	delete(a.cells.inflight, key)
	a.cells.mu.Unlock()
	close(c.done)
	return c.e, c.err
}

func (a *App) fetchCells(ctx context.Context, q url.Values, city string) (*cellsEntry, error) {
	raw, err := a.Engine.CellsQuery(ctx, q)
	if err != nil {
		return nil, err
	}
	simulated := a.Origins.HasSimulated()
	if city != "" {
		simulated = a.simulatedIn(city)
	}
	body, err := live.DecorateCollectionFor(raw, a.Origins, city, simulated)
	if err != nil {
		return nil, err
	}
	sum := sha256.Sum256(body)
	e := &cellsEntry{body: body, gz: web.CompressBest(body), etag: `"` + hex.EncodeToString(sum[:12]) + `"`, at: time.Now(), city: city}
	if c, ok := a.Cities.Get(city); ok {
		e.frame = web.FrameFor(c.ID, c.BBox.MinLon, c.BBox.MinLat, c.BBox.MaxLon, c.BBox.MaxLat)
	}
	return e, nil
}

// apiCells is GET /api/cells?bbox=minLon,minLat,maxLon,maxLat. Without a
// bbox it returns the whole service area.
func (a *App) apiCells(w http.ResponseWriter, r *http.Request) {
	if !a.limit(w, r, a.apiLimit, "api") {
		return
	}
	e, code, msg := a.cellsRequest(r)
	if code != 0 {
		writeJSON(w, code, map[string]string{"error": msg, "code": "bad_request"})
		return
	}
	if e == nil {
		return
	}
	if e.err != nil {
		a.upstreamError(w, e.err)
		return
	}
	a.serveCells(w, r, e.entry)
}

// cellsAnswer is a cells lookup's result.
type cellsAnswer struct {
	entry *cellsEntry
	err   error
}

// cellsRequest reads ?city= or ?bbox= (default: the default city).
func (a *App) cellsRequest(r *http.Request) (*cellsAnswer, int, string) {
	q := r.URL.Query()
	if raw := q.Get("bbox"); raw != "" {
		b, err := config.ParseBBox(raw)
		if err != nil {
			return nil, http.StatusBadRequest, err.Error()
		}
		if (b.MaxLon-b.MinLon)*(b.MaxLat-b.MinLat) > 1 {
			return nil, http.StatusBadRequest, "bbox is too large: at most 1 square degree per request"
		}
		e, err := a.cellsFor(r.Context(), b)
		return &cellsAnswer{e, err}, 0, ""
	}
	city := strings.ToLower(strings.TrimSpace(q.Get("city")))
	if city == "" {
		city = cities.Default
	}
	if _, ok := a.Cities.Get(city); !ok {
		return nil, http.StatusBadRequest, fmt.Sprintf("city must be one of %s", strings.Join(a.Cities.SortedIDs(), ", "))
	}
	e, err := a.cellsForCity(r.Context(), city)
	return &cellsAnswer{e, err}, 0, ""
}

// serveCells writes a cached collection with its ETag and gzip copy.
func (a *App) serveCells(w http.ResponseWriter, r *http.Request, e *cellsEntry) {
	h := w.Header()
	h.Set("ETag", e.etag)
	h.Set("Cache-Control", "public, max-age=2")
	h.Set("Vary", "Accept-Encoding")
	if etagMatch(r.Header.Get("If-None-Match"), e.etag) {
		w.WriteHeader(http.StatusNotModified)
		return
	}
	h.Set("Content-Type", "application/geo+json")
	web.WriteBody(w, r, e.body, e.gz)
}

func etagMatch(header, etag string) bool {
	for _, t := range strings.Split(header, ",") {
		t = strings.TrimSpace(strings.TrimPrefix(strings.TrimSpace(t), "W/"))
		if t == etag || t == "*" {
			return true
		}
	}
	return false
}

func (a *App) upstreamError(w http.ResponseWriter, err error) {
	var ue *upstream.Error
	if errors.As(err, &ue) {
		status := ue.Status
		if status >= 500 && status != http.StatusServiceUnavailable {
			status = http.StatusBadGateway
		}
		writeJSON(w, status, map[string]string{"error": ue.Message, "code": "engine_error"})
		return
	}
	a.Log.Warn("engine unavailable", "error", err.Error())
	writeJSON(w, http.StatusServiceUnavailable, map[string]string{"error": "the map engine is not reachable right now; try again shortly", "code": "engine_unavailable"})
}

// NormaliseVehicle maps UI and API names onto the engine's vehicle ids.
func NormaliseVehicle(v string) (string, bool) {
	switch strings.ToLower(strings.TrimSpace(strings.ReplaceAll(v, "-", "_"))) {
	case "walking", "walk", "pedestrian", "foot":
		return "pedestrian", true
	case "two_wheeler", "twowheeler", "scooter", "bike", "motorcycle":
		return "two_wheeler", true
	case "auto", "auto_rickshaw", "rickshaw":
		return "auto", true
	case "car":
		return "car", true
	case "bus":
		return "bus", true
	}
	return "", false
}

// checkPoint reads lat,lon and, when city is set, requires it to lie in
// that city.
func (a *App) checkPoint(raw, name string) (string, float64, float64, *routeError) {
	lat, lon, ok := parseLatLon(raw)
	if !ok {
		return "", 0, 0, rerr(400, "bad_point", "route_bad_point", "%s must be lat,lon, such as 12.9791,80.2209.", name)
	}
	return fmt.Sprintf("%.6f,%.6f", lat, lon), lat, lon, nil
}

type routeQuery struct {
	From, To, Vehicle, City string
}

// parseRoute reads from, to and vehicle. Both ends must be in one covered
// city: the chosen one when city is given, else the one holding from.
func (a *App) parseRoute(r *http.Request, city string) (routeQuery, *routeError) {
	q := r.URL.Query()
	from, flat, flon, err := a.checkPoint(q.Get("from"), "from")
	if err != nil {
		return routeQuery{}, err
	}
	to, tlat, tlon, err := a.checkPoint(q.Get("to"), "to")
	if err != nil {
		return routeQuery{}, err
	}
	v, ok := NormaliseVehicle(q.Get("vehicle"))
	if !ok {
		return routeQuery{}, rerr(400, "bad_vehicle", "route_bad_vehicle", "vehicle must be walking, two_wheeler, auto, car or bus.")
	}
	fc, fok := a.Cities.Find(flat, flon)
	tc, tok := a.Cities.Find(tlat, tlon)
	if city == "" {
		if !fok {
			return routeQuery{}, rerr(400, "outside_area", "route_outside_all", "The start (%.4f, %.4f) is outside every city Neermattam covers. The order is lat,lon.", flat, flon)
		}
		city = fc.ID
	}
	c, _ := a.Cities.Get(city)
	switch {
	case !c.Contains(flat, flon) || !c.Contains(tlat, tlon):
		if fok && tok && fc.ID != tc.ID {
			return routeQuery{}, rerr(400, "different_cities", "route_different_cities", "The start and the end are in different cities. A route has to start and end in the same city.")
		}
		return routeQuery{}, rerr(400, "outside_area", "route_outside_city", "The start or the end is outside %s. Pick both points in %s, or switch city.", cityArg(city), cityArg(city))
	}
	return routeQuery{from, to, v, city}, nil
}

// routeJSON asks the engine and labels the result when the city holds
// simulated water.
func (a *App) routeJSON(ctx context.Context, q routeQuery) ([]byte, error) {
	raw, err := a.Engine.Route(ctx, q.From, q.To, q.Vehicle)
	if err != nil {
		return nil, err
	}
	if !a.simulatedIn(q.City) {
		return raw, nil
	}
	var f map[string]any
	if err := json.Unmarshal(raw, &f); err != nil {
		return nil, err
	}
	for _, x := range []any{f, f["baseline"]} {
		if m, ok := x.(map[string]any); ok {
			if p, ok := m["properties"].(map[string]any); ok {
				p["simulated_scenario"] = true
			}
		}
	}
	return json.Marshal(f)
}

// apiRoute is GET /api/route?from=lat,lon&to=lat,lon&vehicle=...
func (a *App) apiRoute(w http.ResponseWriter, r *http.Request) {
	if !a.limit(w, r, a.apiLimit, "api") {
		return
	}
	body, rerr := a.routeFor(r)
	if rerr != nil {
		writeJSON(w, rerr.Status, map[string]string{"error": rerr.Msg, "code": rerr.Code})
		return
	}
	w.Header().Set("Content-Type", "application/geo+json")
	w.Header().Set("Cache-Control", "no-store")
	web.WriteBody(w, r, body, nil)
}

// routeFor plans the route a request asks for, with errors in plain words.
func (a *App) routeFor(r *http.Request) ([]byte, *routeError) {
	q, perr := a.parseRoute(r, strings.ToLower(r.URL.Query().Get("city")))
	if perr != nil {
		return nil, perr
	}
	body, err := a.routeJSON(r.Context(), q)
	if err != nil {
		return nil, a.engineRouteError(err, q.City)
	}
	return body, nil
}

// apiStream is GET /api/stream: server-sent events of changed cells, the
// report ticker, and resync hints, for a bounded number of browsers.
func (a *App) apiStream(w http.ResponseWriter, r *http.Request) {
	if !a.limit(w, r, a.apiLimit, "stream") {
		return
	}
	city := strings.ToLower(strings.TrimSpace(r.URL.Query().Get("city")))
	if city != "" {
		if _, ok := a.Cities.Get(city); !ok {
			writeJSON(w, http.StatusBadRequest, map[string]string{"error": "unknown city", "code": "bad_city"})
			return
		}
	}
	sub, err := a.Hub.SubscribeCity(city)
	if err != nil {
		a.Metrics.SSEDropped.Inc("refused_full")
		w.Header().Set("Retry-After", "10")
		writeJSON(w, http.StatusServiceUnavailable, map[string]string{"error": "the live map is at capacity; it will retry shortly"})
		return
	}
	defer sub.Close()
	rc := http.NewResponseController(w)
	h := w.Header()
	h.Set("Content-Type", "text/event-stream")
	h.Set("Cache-Control", "no-cache")
	h.Set("Connection", "keep-alive")
	h.Set("X-Accel-Buffering", "no")
	w.WriteHeader(http.StatusOK)
	hello, _ := json.Marshal(map[string]any{"city": city, "simulated_scenario": a.simulatedIn(city) || (city == "" && a.Origins.HasSimulated()), "engine_stream": a.Hub.UpstreamConnected()})
	if _, err := fmt.Fprintf(w, "retry: 3000\n\nevent: hello\ndata: %s\n\n", hello); err != nil {
		return
	}
	if rc.Flush() != nil {
		return
	}
	ping := time.NewTicker(20 * time.Second)
	defer ping.Stop()
	for {
		select {
		case <-r.Context().Done():
			return
		case m, ok := <-sub.C():
			if !ok {
				return
			}
			if _, err := fmt.Fprintf(w, "event: %s\ndata: %s\n\n", m.Event, m.Data); err != nil {
				return
			}
			if rc.Flush() != nil {
				return
			}
		case <-ping.C:
			if _, err := io.WriteString(w, ": ping\n\n"); err != nil {
				return
			}
			if rc.Flush() != nil {
				return
			}
		}
	}
}

// readyz reports 200 only when both the engine and perception are ready.
// A probe that times out or cannot connect does not flip readiness on its
// own: within READY_GRACE of that service's last successful probe the last
// answer stands, and the body says so (ready.go).
func (a *App) readyz(w http.ResponseWriter, r *http.Request) {
	ctx, cancel := context.WithTimeout(r.Context(), a.Cfg.ReadyProbeTimeout)
	defer cancel()
	var wg sync.WaitGroup
	var engErr, perErr error
	var eng upstream.Readiness
	wg.Add(2)
	go func() { defer wg.Done(); eng, engErr = a.Engine.Readiness(ctx) }()
	go func() { defer wg.Done(); perErr = a.Perception.Ready(ctx) }()
	wg.Wait()
	now := time.Now()
	engNote, perNote := "", ""
	if engErr == nil {
		a.ready.ok("engine", now, eng)
	} else if last, age, ok := a.ready.lean("engine", engErr, now, a.Cfg.ReadyGrace); ok {
		eng, engNote, engErr = last, staleNote(engErr, age), nil
	}
	if perErr == nil {
		a.ready.ok("perception", now, upstream.Readiness{})
	} else if _, age, ok := a.ready.lean("perception", perErr, now, a.Cfg.ReadyGrace); ok {
		perNote, perErr = staleNote(perErr, age), nil
	}
	status := func(err error) string {
		if err == nil {
			return "ready"
		}
		return err.Error()
	}
	whatsapp := "disabled"
	if a.WhatsApp != nil {
		whatsapp = "enabled"
	}
	body := map[string]any{
		"engine":             status(engErr),
		"perception":         status(perErr),
		"engine_stream":      a.Hub.UpstreamConnected(),
		"whatsapp":           whatsapp,
		"simulated_scenario": a.Cfg.Demo,
	}
	if engNote != "" {
		body["engine_note"] = engNote
	}
	if perNote != "" {
		body["perception_note"] = perNote
	}
	code := http.StatusOK
	body["status"] = "ready"
	if engErr == nil && eng.Status == "degraded" {
		// Some city's routing is loading or failed; everything else works.
		body["status"] = "degraded"
		body["engine"] = "degraded"
		body["reason"] = eng.Reason
	}
	if engErr != nil || perErr != nil {
		code = http.StatusServiceUnavailable
		body["status"] = "not_ready"
	}
	writeJSON(w, code, body)
}
