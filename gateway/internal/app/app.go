// Package app wires the gateway together: routes, middleware, handlers for
// pages and APIs, and the background workers.
package app

import (
	"context"
	"log/slog"
	"net/http"
	"os"
	"path/filepath"
	"sync"
	"time"

	"neermattam/gateway/internal/alerts"
	"neermattam/gateway/internal/apikeys"
	"neermattam/gateway/internal/buildinfo"
	"neermattam/gateway/internal/certs"
	"neermattam/gateway/internal/cities"
	"neermattam/gateway/internal/config"
	"neermattam/gateway/internal/facts"
	"neermattam/gateway/internal/live"
	"neermattam/gateway/internal/metrics"
	"neermattam/gateway/internal/overlay"
	"neermattam/gateway/internal/ratelimit"
	"neermattam/gateway/internal/report"
	"neermattam/gateway/internal/upstream"
	"neermattam/gateway/internal/verify"
	"neermattam/gateway/internal/web"
	"neermattam/gateway/internal/whatsapp"
)

// App is the running gateway.
type App struct {
	Cfg        config.Config
	Log        *slog.Logger
	Metrics    *metrics.Set
	Engine     *upstream.Engine
	Perception *upstream.Perception
	Origins    *live.Origins
	Hub        *live.Hub
	Pipeline   *report.Pipeline
	Renderer   *web.Renderer
	WhatsApp   *whatsapp.Handler
	Overlays   *overlay.Store
	Facts      facts.Facts
	Cities     *cities.Registry
	AlertStore *alerts.Store
	Alerts     *alerts.Service // nil when ALERTS=0
	Keys       *apikeys.Store
	// CertKeys are the public keys certificates are checked against;
	// Certs keeps each report's certificate.
	CertKeys *certs.KeySet
	Certs    *certs.Store
	// Sites is the Critical sites watcher (sites.go).
	Sites sitesState
	// Build is the source commit this gateway was built from.
	Build buildinfo.Info

	ready readiness // /readyz hysteresis (ready.go)

	reportLimit *ratelimit.Limiter
	apiLimit    *ratelimit.Limiter
	cells       *cellsCache
	started     time.Time
	saveMu      sync.Mutex
	saveErr     string
	bg          sync.WaitGroup // the saver, joined by Wait
	keysFetched time.Time      // last fetch of perception's keys, under keysMu
	keysMu      sync.Mutex
}

// Wait blocks until the background saver has stopped (after ctx ends).
func (a *App) Wait() { a.bg.Wait() }

// Options let tests replace external dependencies.
type Options struct {
	Rainfall verify.Rainfall // nil uses Open-Meteo
	// Now replaces the clock the report pipeline uses, for tests.
	Now func() time.Time
}

// New builds the gateway. base is cancelled at shutdown and bounds every
// background job.
func New(base context.Context, cfg config.Config, log *slog.Logger, opts Options) (*App, error) {
	m := metrics.NewSet()
	r, err := web.NewRenderer()
	if err != nil {
		return nil, err
	}
	a := &App{
		Cfg:         cfg,
		Log:         log,
		Metrics:     m,
		Engine:      upstream.NewEngine(cfg.EngineURL, cfg.EngineTimeout, cfg.RouteTimeout, m),
		Perception:  upstream.NewPerception(cfg.PerceptionURL, cfg.PerceptionTimeout, m),
		Origins:     live.NewOrigins(),
		Renderer:    r,
		reportLimit: ratelimit.New(cfg.ReportsPerMinute, cfg.ReportBurst),
		apiLimit:    ratelimit.New(cfg.APIPerMinute, cfg.APIBurst),
		cells:       newCellsCache(cfg.CellsCacheTTL),
		started:     time.Now(),
	}
	a.Origins.Demo = cfg.Demo
	a.Cities = cities.NewRegistry(log)
	if c, ok := a.Cities.Get(cities.Default); ok {
		// Only the default city holds the simulated scenario, so only there
		// can a cell with no record read as simulated.
		a.Origins.DemoArea = c.Contains
	}
	if path := a.provenancePath(); path != "" {
		n, err := a.Origins.Load(path)
		if err != nil {
			// Keep the bad file for inspection and start with no record.
			aside := path + ".rejected-" + time.Now().UTC().Format("20060102T150405")
			_ = os.Rename(path, aside)
			log.Warn("provenance not loaded; starting with an empty record", "error", err.Error(), "moved_to", aside)
		} else {
			log.Info("provenance loaded", "cells", n, "real_reports", len(a.Origins.Records()), "path", path)
		}
	}
	a.Facts, _ = facts.Load(cfg.RepoRoot)
	a.Build = buildinfo.Read(cfg.BuildInfoPath)
	if a.Build.Commit != "" {
		log.Info("build", "commit", a.Build.Commit, "from", a.Build.Source, "uncommitted_changes", a.Build.Dirty)
	}
	a.Hub = live.NewHub(cfg.MaxSSEClients, a.Origins, log, m)
	// The ticker shows real reports from before a restart.
	recs := a.Origins.Records()
	for i := min(len(recs), 8) - 1; i >= 0; i-- {
		r := recs[i]
		city := ""
		if c, ok := a.Cities.Find(r.Lat, r.Lon); ok {
			city = c.ID
		}
		a.Hub.RestoreFeed(live.FeedItem{City: city, Source: r.Source, DepthCm: r.DepthCm, DepthSigmaCm: r.DepthSigmaCm, Place: r.Place, Lat: r.Lat, Lon: r.Lon, At: r.ReceivedAt})
	}
	a.Overlays = overlay.NewStore(time.Hour, 300, 96<<20)
	if err := a.setupCertificates(); err != nil {
		return nil, err
	}
	rain := opts.Rainfall
	if rain == nil {
		rain = verify.NewOpenMeteo(cfg.OpenMeteoURL, cfg.WeatherTimeout, m)
	}
	a.Pipeline = &report.Pipeline{
		Cfg:        cfg,
		Cities:     a.Cities,
		Perception: a.Perception,
		Engine:     a.Engine,
		Verifier: &verify.Verifier{
			T:       verify.DefaultThresholds(),
			Cells:   report.EngineNeighbourhood{Engine: a.Engine, Origins: a.Origins},
			Rain:    rain,
			Log:     log,
			Metrics: m,
		},
		Dedupe:       report.NewDedupe(cfg.DuplicateWindow, cfg.NearDuplicateMax),
		Origins:      a.Origins,
		Overlays:     a.Overlays,
		Certificates: a.Certs,
		ReasonTexts:  web.ReasonTexts,
		Hub:          a.Hub,
		Log:          log,
		Metrics:      m,
	}
	if opts.Now != nil {
		a.Pipeline.SetClock(opts.Now)
	}
	a.Pipeline.Init()
	if err := a.setupAlerts(); err != nil {
		return nil, err
	}
	a.setupSites() // Critical sites (sites.go)
	if cfg.WhatsApp.Enabled() {
		limits := map[string]int64{"image": cfg.MaxImageBytes, "video": cfg.MaxVideoBytes, "audio": cfg.MaxAudioBytes}
		a.WhatsApp = whatsapp.New(base, cfg.WhatsApp, cfg.PublicURL, limits, a.Pipeline, log, m)
		if a.Alerts != nil {
			a.WhatsApp.Watch = a.whatsAppWatch()
			a.Alerts.WhatsApp = a.WhatsApp.Send
		}
	}
	keysDir := cfg.DataDir
	if keysDir == "" {
		keysDir = os.TempDir() // no persistence: keys can still be made with -dir
	}
	keys, err := apikeys.Open(keysDir)
	if err != nil {
		log.Warn("developer API keys not loaded", "error", err.Error())
		keys, _ = apikeys.Open(filepath.Join(os.TempDir(), "neermattam-no-keys"))
	}
	a.Keys = keys
	m.Registry.NewGaugeFunc("neermattam_sse_clients", "Browsers connected to the live stream.", func() float64 { return float64(a.Hub.Clients()) })
	m.Registry.NewGaugeFunc("neermattam_engine_stream_up", "1 when the engine stream is connected.", func() float64 {
		if a.Hub.UpstreamConnected() {
			return 1
		}
		return 0
	})
	m.Registry.NewGaugeFunc("neermattam_rate_limit_keys", "Client keys tracked by the rate limiters.", func() float64 {
		return float64(a.reportLimit.Len() + a.apiLimit.Len())
	})
	m.Registry.NewGaugeFunc("neermattam_uptime_seconds", "Seconds since the gateway started.", func() float64 { return time.Since(a.started).Seconds() })
	return a, nil
}

// Handler returns the full HTTP handler with middleware.
func (a *App) Handler() http.Handler {
	mux := http.NewServeMux()
	page := func(h http.HandlerFunc) http.Handler { return h }

	mux.Handle("GET /{$}", page(a.homePage))
	mux.Handle("GET /map", page(a.mapPage))
	mux.Handle("GET /report", page(a.reportPage))
	mux.Handle("POST /report", http.HandlerFunc(a.reportForm))
	mux.Handle("GET /route", page(a.routePage))
	mux.Handle("GET /route/plan", http.HandlerFunc(a.routePlan))
	mux.Handle("GET /about", page(a.aboutPage))
	mux.Handle("GET /alerts", page(a.alertsPage))
	mux.Handle("GET /developers", page(a.developersPage))
	mux.HandleFunc("GET /sw.js", a.serviceWorker)

	mux.Handle("GET /control", page(a.controlPage))
	mux.HandleFunc("POST /control/login", a.controlLogin)
	mux.HandleFunc("POST /control/logout", a.controlLogout)
	// The key page lives at /control; a bookmarked or typed /control/login
	// goes there instead of a 404 or a 405.
	mux.HandleFunc("GET /control/login", toControl)
	mux.HandleFunc("GET /control/logout", toControl)
	mux.HandleFunc("GET /control/panel", a.controlPanel)
	mux.HandleFunc("GET /control/export", a.controlExport)
	mux.Handle("GET /control/demo", page(a.controlDemoPage))
	mux.HandleFunc("GET /control/demo/panel", a.controlDemoPanel)
	mux.HandleFunc("GET /control/demo/export", a.controlDemoExport)

	mux.HandleFunc("GET /api/cities", a.apiCities)
	mux.HandleFunc("GET /api/alerts/key", a.apiAlertKey)
	mux.HandleFunc("POST /api/alerts", a.apiAlertCreate)
	mux.HandleFunc("GET /api/alerts/{id}", a.apiAlertGet)
	mux.HandleFunc("DELETE /api/alerts/{id}", a.apiAlertDelete)

	mux.HandleFunc("GET /api/v1/route", a.devRoute)
	mux.HandleFunc("GET /api/v1/cells", a.devCells)
	mux.HandleFunc("GET /api/v1/usage", a.devUsage)

	a.registerSites(mux) // Critical sites (sites.go)

	mux.HandleFunc("POST /api/report", a.apiReport)
	mux.HandleFunc("GET /api/cells", a.apiCells)
	mux.HandleFunc("GET /api/route", a.apiRoute)
	mux.HandleFunc("GET /api/stream", a.apiStream)
	mux.HandleFunc("GET /api/overlay/{id}", a.apiOverlay)
	mux.HandleFunc("GET /api/certificates/{id}", a.apiCertificate)
	mux.Handle("GET /verify/{id}", page(a.verifyPage))
	mux.HandleFunc("GET /.well-known/neermattam-keys.json", a.wellKnownKeys)

	if a.WhatsApp != nil {
		mux.HandleFunc("GET /webhooks/whatsapp", a.WhatsApp.Verify)
		mux.HandleFunc("POST /webhooks/whatsapp", a.WhatsApp.Receive)
	} else {
		disabled := func(w http.ResponseWriter, _ *http.Request) {
			writeJSON(w, http.StatusNotFound, map[string]string{"error": "the WhatsApp webhook is not configured on this gateway"})
		}
		mux.HandleFunc("GET /webhooks/whatsapp", disabled)
		mux.HandleFunc("POST /webhooks/whatsapp", disabled)
	}

	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, _ *http.Request) {
		writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
	})
	mux.HandleFunc("GET /readyz", a.readyz)
	mux.Handle("GET /metrics", a.Metrics.Registry.Handler())
	mux.Handle("GET /static/", a.Renderer.Assets().Handler())
	mux.HandleFunc("GET /favicon.ico", func(w http.ResponseWriter, r *http.Request) {
		http.Redirect(w, r, "/static/brand/favicon.ico", http.StatusMovedPermanently)
	})
	mux.HandleFunc("/", a.notFound)

	// secure runs before observe so the request observe sees is the one the
	// mux annotates with its matched pattern.
	return a.recoverer(a.secure(a.withLang(a.observe(mux))))
}

func (a *App) provenancePath() string {
	if a.Cfg.DataDir == "" {
		return ""
	}
	return filepath.Join(a.Cfg.DataDir, live.FileName)
}

// SaveProvenance writes the provenance record if it changed. A failure is
// logged once until it changes, not every few seconds.
func (a *App) SaveProvenance() {
	path := a.provenancePath()
	if path == "" {
		return
	}
	err := a.Origins.Save(path)
	msg := ""
	if err != nil {
		msg = err.Error()
	}
	a.saveMu.Lock()
	defer a.saveMu.Unlock()
	if msg != a.saveErr {
		if msg != "" {
			a.Log.Warn("provenance not saved; set GATEWAY_DATA_DIR to a writable directory", "error", msg)
		} else {
			a.Log.Info("provenance saving again", "path", path)
		}
		a.saveErr = msg
	}
}

// Run starts the background workers: the engine stream fan-out and the
// provenance saver.
func (a *App) Run(ctx context.Context) {
	go a.Hub.Run(ctx, a.Engine)
	go a.citiesRefresh(ctx)
	go a.certKeysRefresh(ctx)
	if a.Alerts != nil {
		go a.Alerts.Run(ctx)
	}
	a.runSites(ctx) // Critical sites (sites.go)
	a.bg.Add(1)
	go func() {
		defer a.bg.Done()
		t := time.NewTicker(3 * time.Second)
		defer t.Stop()
		n := 0
		for {
			select {
			case <-ctx.Done():
				return
			case <-t.C:
				a.SaveProvenance()
				a.saveAlerts()
				a.saveSiteAlerts()
				if n++; n%10 == 0 {
					a.saveKeyUsage()
				}
			}
		}
	}()
	go func() {
		t := time.NewTicker(5 * time.Minute)
		defer t.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-t.C:
				a.reportLimit.Sweep()
				a.apiLimit.Sweep()
			}
		}
	}()
}

// Shutdown closes live streams so the HTTP server can drain, and waits for
// WhatsApp jobs.
func (a *App) Shutdown() {
	a.Hub.Close()
	if a.WhatsApp != nil {
		a.WhatsApp.Wait()
	}
	a.SaveProvenance()
	a.saveAlerts()
	a.saveSiteAlerts()
	a.saveKeyUsage()
}

func (a *App) saveKeyUsage() {
	if a.Keys != nil && a.Cfg.DataDir != "" {
		if err := a.Keys.Save(); err != nil {
			a.Log.Warn("API key usage not saved", "error", err.Error())
		}
	}
}
