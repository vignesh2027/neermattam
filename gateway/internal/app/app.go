// Package app wires the gateway together: routes, middleware, handlers for
// pages and APIs, and the background workers.
package app

import (
	"context"
	"log/slog"
	"net/http"
	"time"

	"neermattam/gateway/internal/config"
	"neermattam/gateway/internal/live"
	"neermattam/gateway/internal/metrics"
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

	reportLimit *ratelimit.Limiter
	apiLimit    *ratelimit.Limiter
	cells       *cellsCache
	started     time.Time
}

// Options let tests replace external dependencies.
type Options struct {
	Rainfall verify.Rainfall // nil uses Open-Meteo
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
	a.Hub = live.NewHub(cfg.MaxSSEClients, a.Origins, log, m)
	rain := opts.Rainfall
	if rain == nil {
		rain = verify.NewOpenMeteo(cfg.OpenMeteoURL, cfg.WeatherTimeout, m)
	}
	a.Pipeline = &report.Pipeline{
		Cfg:        cfg,
		Perception: a.Perception,
		Engine:     a.Engine,
		Verifier: &verify.Verifier{
			T:       verify.DefaultThresholds(),
			Cells:   report.EngineNeighbourhood{Engine: a.Engine, Origins: a.Origins},
			Rain:    rain,
			Log:     log,
			Metrics: m,
		},
		Dedupe:  report.NewDedupe(cfg.DuplicateWindow, cfg.NearDuplicateMax),
		Origins: a.Origins,
		Hub:     a.Hub,
		Log:     log,
		Metrics: m,
	}
	a.Pipeline.Init()
	if cfg.WhatsApp.Enabled() {
		limits := map[string]int64{"image": cfg.MaxImageBytes, "video": cfg.MaxVideoBytes, "audio": cfg.MaxAudioBytes}
		a.WhatsApp = whatsapp.New(base, cfg.WhatsApp, cfg.PublicURL, limits, a.Pipeline, log, m)
	}
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

	mux.Handle("GET /{$}", page(a.mapPage))
	mux.Handle("GET /report", page(a.reportPage))
	mux.Handle("POST /report", http.HandlerFunc(a.reportForm))
	mux.Handle("GET /route", page(a.routePage))
	mux.Handle("GET /route/plan", http.HandlerFunc(a.routePlan))
	mux.Handle("GET /about", page(a.aboutPage))

	mux.HandleFunc("POST /api/report", a.apiReport)
	mux.HandleFunc("GET /api/cells", a.apiCells)
	mux.HandleFunc("GET /api/route", a.apiRoute)
	mux.HandleFunc("GET /api/stream", a.apiStream)

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
	mux.Handle("GET /static/", web.Static())
	mux.HandleFunc("GET /favicon.ico", func(w http.ResponseWriter, r *http.Request) {
		http.Redirect(w, r, "/static/favicon.svg", http.StatusMovedPermanently)
	})
	mux.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
		if wantsHTML(r) {
			http.Error(w, "Page not found. Try the live map at /.", http.StatusNotFound)
			return
		}
		writeJSON(w, http.StatusNotFound, map[string]string{"error": "no such endpoint; see docs/CONTRACTS.md section 3"})
	})

	// secure runs before observe so the request observe sees is the one the
	// mux annotates with its matched pattern.
	return a.recoverer(a.secure(a.observe(mux)))
}

// Run starts the background workers: the engine stream fan-out.
func (a *App) Run(ctx context.Context) {
	go a.Hub.Run(ctx, a.Engine)
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
}
