package app

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"strconv"
	"strings"
	"time"

	"neermattam/gateway/internal/places"
	"neermattam/gateway/internal/ratelimit"
	"neermattam/gateway/internal/report"
	"neermattam/gateway/internal/upstream"
	"neermattam/gateway/internal/web"
)

func (a *App) page(r *http.Request, title, active string, needsMap bool) *web.Page {
	return &web.Page{
		Title:    title,
		Active:   active,
		Nonce:    nonceFrom(r.Context()),
		NeedsMap: needsMap,
		Footer:   active == "report" || active == "about",
		Demo:     a.Cfg.Demo,
		Limits: web.Limits{
			ImageMB:      int(a.Cfg.MaxImageBytes >> 20),
			VideoMB:      int(a.Cfg.MaxVideoBytes >> 20),
			VideoSeconds: int(a.Cfg.MaxVideoSeconds),
		},
	}
}

func (a *App) render(w http.ResponseWriter, status int, name string, p *web.Page) {
	if err := a.Renderer.Render(w, status, name, p); err != nil {
		a.Log.Error("render failed", "page", name, "error", err.Error())
	}
}

// mapPage is GET /. Stats are filled server-side when the engine answers
// quickly, so the page reads correctly before any script runs.
func (a *App) mapPage(w http.ResponseWriter, r *http.Request) {
	p := a.page(r, "Neermattam, live flood depth for Chennai", "map", true)
	p.Stats = web.MapStats{Cells: "0", Deep: "0", Max: "None"}
	ctx, cancel := context.WithTimeout(r.Context(), 1500*time.Millisecond)
	defer cancel()
	if body, err := a.cellsFor(ctx, a.Cfg.ServiceArea); err == nil {
		if cells, err := upstream.DecodeCells(body); err == nil {
			deep, maxD, known := 0, 0.0, 0
			for _, c := range cells {
				if c.Props.Unknown {
					continue
				}
				known++
				if c.Props.DepthCm >= 30 {
					deep++
				}
				maxD = max(maxD, c.Props.DepthCm)
			}
			p.Stats.Cells = strconv.Itoa(len(cells))
			p.Stats.Deep = strconv.Itoa(deep)
			if known > 0 {
				p.Stats.Max = fmt.Sprintf("%.0f cm", maxD)
			}
		}
	}
	p.Feed = a.Hub.Feed()
	if len(p.Feed) > 5 {
		p.Feed = p.Feed[:5]
	}
	a.render(w, http.StatusOK, "map", p)
}

func (a *App) reportPage(w http.ResponseWriter, r *http.Request) {
	p := a.page(r, "Report water, Neermattam", "report", true)
	if at := r.URL.Query().Get("at"); at != "" {
		if lat, lon, ok := strings.Cut(at, ","); ok {
			if _, err := strconv.ParseFloat(lat, 64); err == nil {
				if _, err := strconv.ParseFloat(lon, 64); err == nil {
					p.Report.Lat, p.Report.Lon = lat, lon
				}
			}
		}
	}
	a.render(w, http.StatusOK, "report", p)
}

// reportForm is POST /report. With Accept: application/x-ndjson it streams
// one JSON line per real pipeline stage and ends with the rendered result
// card. Without it (no script) it renders the whole page.
func (a *App) reportForm(w http.ResponseWriter, r *http.Request) {
	streaming := strings.Contains(r.Header.Get("Accept"), "application/x-ndjson")
	fail := func(re *report.Error) {
		if streaming {
			html, _ := a.Renderer.Fragment("result_card", web.ErrorView(re.Message))
			w.Header().Set("Content-Type", "application/x-ndjson")
			w.WriteHeader(re.Status)
			_ = json.NewEncoder(w).Encode(map[string]any{"stage": "error", "code": re.Code, "error": re.Message, "html": html})
			return
		}
		p := a.page(r, "Report water, Neermattam", "report", true)
		p.Report.Error = re.Message
		a.render(w, re.Status, "report", p)
	}
	if ok, wait := a.reportLimit.Allow(clientKey(r, a.Cfg.TrustProxy)); !ok {
		a.Metrics.RateLimited.Inc("report")
		w.Header().Set("Retry-After", strconv.Itoa(int(wait.Seconds()+0.999)))
		fail(&report.Error{Status: http.StatusTooManyRequests, Code: "rate_limited", Message: fmt.Sprintf("You have sent several reports in a short time. Please wait %d seconds and try again.", int(wait.Seconds()+0.999))})
		return
	}
	sub, perr := a.parseSubmission(w, r, "web")
	if perr == nil {
		if err := a.Pipeline.Validate(sub); err != nil {
			perr = asReportError(err)
		}
	}
	if perr != nil {
		a.Metrics.Reports.Inc("web", "invalid")
		fail(perr)
		return
	}
	if !streaming {
		p := a.page(r, "Report water, Neermattam", "report", true)
		p.Report.Lat, p.Report.Lon = strconv.FormatFloat(sub.Lat, 'f', 6, 64), strconv.FormatFloat(sub.Lon, 'f', 6, 64)
		out, err := a.Pipeline.Run(r.Context(), sub, nil)
		if err != nil {
			re := asReportError(err)
			p.Result = web.ErrorView(re.Message)
			a.render(w, re.Status, "report", p)
			return
		}
		p.Result = web.NewResultView(out)
		a.render(w, http.StatusOK, "report", p)
		return
	}

	rc := http.NewResponseController(w)
	w.Header().Set("Content-Type", "application/x-ndjson")
	w.Header().Set("Cache-Control", "no-store")
	w.Header().Set("X-Accel-Buffering", "no")
	w.WriteHeader(http.StatusOK)
	enc := json.NewEncoder(w)
	send := func(v map[string]any) {
		_ = enc.Encode(v)
		_ = rc.Flush()
	}
	send(map[string]any{"stage": report.StageReceived})
	out, err := a.Pipeline.Run(r.Context(), sub, func(stage string) {
		send(map[string]any{"stage": stage})
	})
	var view *web.ResultView
	final := map[string]any{"stage": "done"}
	if err != nil {
		re := asReportError(err)
		view = web.ErrorView(re.Message)
		final = map[string]any{"stage": "error", "code": re.Code, "error": re.Message}
	} else {
		view = web.NewResultView(out)
		final["status"] = out.Status
	}
	html, rerr := a.Renderer.Fragment("result_card", view)
	if rerr != nil {
		a.Log.Error("result render failed", "error", rerr.Error())
	}
	final["html"] = html
	send(final)
}

func clientKey(r *http.Request, trust bool) string {
	return ratelimit.ClientIP(r, trust)
}

func (a *App) routeForm(r *http.Request) web.RouteForm {
	velachery, _ := places.PresetByID("velachery")
	tnagar, _ := places.PresetByID("tnagar")
	f := web.RouteForm{
		From: velachery.Coord(), To: tnagar.Coord(), Vehicle: "two_wheeler",
		Presets: places.Presets, Vehicles: web.Vehicles,
	}
	q := r.URL.Query()
	if v := q.Get("from"); v != "" {
		f.From = v
	}
	if v := q.Get("to"); v != "" {
		f.To = v
	}
	if v, ok := NormaliseVehicle(q.Get("vehicle")); ok {
		f.Vehicle = v
	}
	return f
}

// planRoute runs a route and builds the summary view.
func (a *App) planRoute(ctx context.Context, r *http.Request) (*web.RouteView, int, error) {
	q, err := a.parseRoute(r)
	if err != nil {
		return nil, http.StatusBadRequest, err
	}
	raw, err := a.routeJSON(ctx, q)
	if err != nil {
		var ue *upstream.Error
		if errors.As(err, &ue) {
			return nil, ue.Status, fmt.Errorf("%s", capitalise(ue.Message))
		}
		return nil, http.StatusServiceUnavailable, fmt.Errorf("the route service is not reachable right now; try again shortly")
	}
	v, err := web.NewRouteView(raw, q.Vehicle, a.Origins.HasSimulated())
	if err != nil {
		return nil, http.StatusBadGateway, fmt.Errorf("the route could not be read")
	}
	return v, http.StatusOK, nil
}

// routePage is GET /route; with from, to and vehicle it also plans.
func (a *App) routePage(w http.ResponseWriter, r *http.Request) {
	p := a.page(r, "Water-aware routes, Neermattam", "route", true)
	p.Route = a.routeForm(r)
	status := http.StatusOK
	q := r.URL.Query()
	if q.Get("from") != "" && q.Get("to") != "" {
		if !a.limit(w, r, a.apiLimit, "route") {
			return
		}
		v, code, err := a.planRoute(r.Context(), r)
		if err != nil {
			p.Route.Error = capitalise(err.Error()) + "."
			status = code
		} else {
			p.Route.View = v
		}
	}
	a.render(w, status, "route", p)
}

// routePlan is GET /route/plan, used by the page script: the summary card
// rendered server-side plus the route GeoJSON.
func (a *App) routePlan(w http.ResponseWriter, r *http.Request) {
	if !a.limit(w, r, a.apiLimit, "route") {
		return
	}
	v, code, err := a.planRoute(r.Context(), r)
	if err != nil {
		writeJSON(w, code, map[string]string{"error": capitalise(err.Error()) + "."})
		return
	}
	html, err := a.Renderer.Fragment("route_summary", v)
	if err != nil {
		a.Log.Error("route summary render failed", "error", err.Error())
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "The summary could not be rendered."})
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"summary_html": html, "route": json.RawMessage(v.GeoJSON)})
}

func (a *App) aboutPage(w http.ResponseWriter, r *http.Request) {
	a.render(w, http.StatusOK, "about", a.page(r, "About Neermattam", "about", false))
}

func capitalise(s string) string {
	if s == "" {
		return s
	}
	return strings.ToUpper(s[:1]) + s[1:]
}
