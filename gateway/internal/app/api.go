package app

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math"
	"mime"
	"net/http"
	"strconv"
	"strings"
	"sync"
	"time"

	"neermattam/gateway/internal/config"
	"neermattam/gateway/internal/live"
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
		return sub, &report.Error{Status: http.StatusBadRequest, Code: "not_multipart", Message: "Send the report as multipart/form-data with a file and lat and lon fields."}
	}
	mr, err := r.MultipartReader()
	if err != nil {
		return sub, &report.Error{Status: http.StatusBadRequest, Code: "bad_multipart", Message: "The upload could not be read."}
	}
	var latS, lonS string
	for parts := 0; ; parts++ {
		if parts > 12 {
			return sub, &report.Error{Status: http.StatusBadRequest, Code: "too_many_parts", Message: "The form has too many parts."}
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
				return sub, &report.Error{Status: http.StatusRequestEntityTooLarge, Code: "too_large", Message: fmt.Sprintf("Files can be up to %d MB.", maxFile>>20)}
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
		case "hint_reference":
			if v != "" {
				if !hintReferences[v] {
					return sub, &report.Error{Status: http.StatusBadRequest, Code: "bad_hint", Message: "hint_reference must be two_wheeler_wheel, car_wheel, auto_wheel, kerb or person."}
				}
				sub.HintReference = v
			}
		case "hint_diameter_cm":
			if v != "" {
				d, err := strconv.ParseFloat(v, 64)
				if err != nil || d < 10 || d > 150 {
					return sub, &report.Error{Status: http.StatusBadRequest, Code: "bad_hint", Message: "The wheel diameter must be a number of centimetres between 10 and 150."}
				}
				sub.HintDiameterCm = &d
			}
		}
	}
	if latS != "" || lonS != "" {
		lat, err1 := strconv.ParseFloat(strings.TrimSpace(latS), 64)
		lon, err2 := strconv.ParseFloat(strings.TrimSpace(lonS), 64)
		if err1 != nil || err2 != nil || math.IsNaN(lat) || math.IsNaN(lon) {
			return sub, &report.Error{Status: http.StatusBadRequest, Code: "bad_location", Message: "Latitude and longitude must be decimal numbers, such as 12.9791 and 80.2209."}
		}
		sub.Lat, sub.Lon, sub.HasLocation = lat, lon, true
	}
	return sub, nil
}

func tooLargeOr(err error, msg string) *report.Error {
	var mbe *http.MaxBytesError
	if errors.As(err, &mbe) {
		return &report.Error{Status: http.StatusRequestEntityTooLarge, Code: "too_large", Message: "The upload is larger than the limit."}
	}
	return &report.Error{Status: http.StatusBadRequest, Code: "bad_multipart", Message: msg}
}

func asReportError(err error) *report.Error {
	var re *report.Error
	if errors.As(err, &re) {
		return re
	}
	if errors.Is(err, context.Canceled) || errors.Is(err, context.DeadlineExceeded) {
		return &report.Error{Status: http.StatusServiceUnavailable, Code: "timeout", Message: "The report took too long to process. Please try again."}
	}
	return &report.Error{Status: http.StatusInternalServerError, Code: "internal", Message: "Something went wrong while processing the report."}
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

// cellsCache holds decorated cell collections for a short time, so a burst
// of page loads costs the engine one query.
type cellsCache struct {
	ttl   time.Duration
	mu    sync.Mutex
	m     map[string]cachedCells
	gz    []byte
	gzFor []byte
}

type cachedCells struct {
	body []byte
	at   time.Time
}

func newCellsCache(ttl time.Duration) *cellsCache {
	return &cellsCache{ttl: ttl, m: map[string]cachedCells{}}
}

func (c *cellsCache) get(key string) ([]byte, bool) {
	c.mu.Lock()
	defer c.mu.Unlock()
	e, ok := c.m[key]
	if !ok || time.Since(e.at) > c.ttl {
		return nil, false
	}
	return e.body, true
}

// gzipped returns a compressed copy of a cached body, made once.
func (c *cellsCache) gzipped(body []byte) []byte {
	c.mu.Lock()
	defer c.mu.Unlock()
	if len(body) > 0 && len(c.gzFor) == len(body) && &c.gzFor[0] == &body[0] {
		return c.gz
	}
	c.gz = web.Compress(body)
	c.gzFor = body
	return c.gz
}

func (c *cellsCache) put(key string, body []byte) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if len(c.m) >= 128 {
		for k, e := range c.m {
			if time.Since(e.at) > c.ttl {
				delete(c.m, k)
			}
		}
		if len(c.m) >= 128 {
			c.m = map[string]cachedCells{}
		}
	}
	c.m[key] = cachedCells{body: body, at: time.Now()}
}

// cellsFor returns decorated cells for a bbox, from the cache when fresh.
func (a *App) cellsFor(ctx context.Context, bbox config.BBox) ([]byte, error) {
	key := fmt.Sprintf("%.4f,%.4f,%.4f,%.4f", bbox.MinLon, bbox.MinLat, bbox.MaxLon, bbox.MaxLat)
	if body, ok := a.cells.get(key); ok {
		a.Metrics.CellsCache.Inc("hit")
		return body, nil
	}
	a.Metrics.CellsCache.Inc("miss")
	raw, err := a.Engine.Cells(ctx, key)
	if err != nil {
		return nil, err
	}
	body, err := live.DecorateCollection(raw, a.Origins)
	if err != nil {
		return nil, err
	}
	a.cells.put(key, body)
	return body, nil
}

// apiCells is GET /api/cells?bbox=minLon,minLat,maxLon,maxLat. Without a
// bbox it returns the whole service area.
func (a *App) apiCells(w http.ResponseWriter, r *http.Request) {
	if !a.limit(w, r, a.apiLimit, "api") {
		return
	}
	bbox := a.Cfg.ServiceArea
	if q := r.URL.Query().Get("bbox"); q != "" {
		b, err := config.ParseBBox(q)
		if err != nil {
			writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
			return
		}
		if (b.MaxLon-b.MinLon)*(b.MaxLat-b.MinLat) > 1 {
			writeJSON(w, http.StatusBadRequest, map[string]string{"error": "bbox is too large: at most 1 square degree per request"})
			return
		}
		bbox = b
	}
	body, err := a.cellsFor(r.Context(), bbox)
	if err != nil {
		a.upstreamError(w, err)
		return
	}
	w.Header().Set("Content-Type", "application/geo+json")
	w.Header().Set("Cache-Control", "public, max-age=2")
	web.WriteBody(w, r, body, a.cells.gzipped(body))
}

func (a *App) upstreamError(w http.ResponseWriter, err error) {
	var ue *upstream.Error
	if errors.As(err, &ue) {
		status := ue.Status
		if status >= 500 && status != http.StatusServiceUnavailable {
			status = http.StatusBadGateway
		}
		writeJSON(w, status, map[string]string{"error": ue.Message})
		return
	}
	a.Log.Warn("engine unavailable", "error", err.Error())
	writeJSON(w, http.StatusServiceUnavailable, map[string]string{"error": "the map engine is not reachable right now; try again shortly"})
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

func (a *App) checkPoint(raw, name string) (string, error) {
	parts := strings.Split(raw, ",")
	if len(parts) != 2 {
		return "", fmt.Errorf("%s must be lat,lon, such as 12.9791,80.2209", name)
	}
	lat, err1 := strconv.ParseFloat(strings.TrimSpace(parts[0]), 64)
	lon, err2 := strconv.ParseFloat(strings.TrimSpace(parts[1]), 64)
	if err1 != nil || err2 != nil {
		return "", fmt.Errorf("%s must be two decimal numbers: lat,lon", name)
	}
	if !a.Cfg.ServiceArea.Contains(lat, lon) {
		return "", fmt.Errorf("%s (%.4f, %.4f) is outside the Chennai service area; the order is lat,lon", name, lat, lon)
	}
	return fmt.Sprintf("%.6f,%.6f", lat, lon), nil
}

type routeQuery struct {
	From, To, Vehicle string
}

func (a *App) parseRoute(r *http.Request) (routeQuery, error) {
	q := r.URL.Query()
	from, err := a.checkPoint(q.Get("from"), "from")
	if err != nil {
		return routeQuery{}, err
	}
	to, err := a.checkPoint(q.Get("to"), "to")
	if err != nil {
		return routeQuery{}, err
	}
	v, ok := NormaliseVehicle(q.Get("vehicle"))
	if !ok {
		return routeQuery{}, errors.New("vehicle must be walking, two_wheeler, auto, car or bus")
	}
	return routeQuery{from, to, v}, nil
}

// routeJSON asks the engine and labels the result when the map holds
// simulated water.
func (a *App) routeJSON(ctx context.Context, q routeQuery) ([]byte, error) {
	raw, err := a.Engine.Route(ctx, q.From, q.To, q.Vehicle)
	if err != nil {
		return nil, err
	}
	if !a.Origins.HasSimulated() {
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
	q, err := a.parseRoute(r)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
		return
	}
	body, err := a.routeJSON(r.Context(), q)
	if err != nil {
		a.upstreamError(w, err)
		return
	}
	w.Header().Set("Content-Type", "application/geo+json")
	w.Header().Set("Cache-Control", "no-store")
	web.WriteBody(w, r, body, nil)
}

// apiStream is GET /api/stream: server-sent events of changed cells, the
// report ticker, and resync hints, for a bounded number of browsers.
func (a *App) apiStream(w http.ResponseWriter, r *http.Request) {
	if !a.limit(w, r, a.apiLimit, "stream") {
		return
	}
	sub, err := a.Hub.Subscribe()
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
	hello, _ := json.Marshal(map[string]any{"simulated_scenario": a.Origins.HasSimulated(), "engine_stream": a.Hub.UpstreamConnected()})
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
func (a *App) readyz(w http.ResponseWriter, r *http.Request) {
	ctx, cancel := context.WithTimeout(r.Context(), 3*time.Second)
	defer cancel()
	var wg sync.WaitGroup
	var engErr, perErr error
	wg.Add(2)
	go func() { defer wg.Done(); engErr = a.Engine.Ready(ctx) }()
	go func() { defer wg.Done(); perErr = a.Perception.Ready(ctx) }()
	wg.Wait()
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
	code := http.StatusOK
	body["status"] = "ready"
	if engErr != nil || perErr != nil {
		code = http.StatusServiceUnavailable
		body["status"] = "not_ready"
	}
	writeJSON(w, code, body)
}
