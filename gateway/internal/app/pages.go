package app

import (
	"context"
	"encoding/json"
	"fmt"
	"html/template"
	"io"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"sync"
	"time"

	"neermattam/gateway/internal/cities"
	"neermattam/gateway/internal/i18n"
	"neermattam/gateway/internal/ratelimit"
	"neermattam/gateway/internal/report"
	"neermattam/gateway/internal/upstream"
	"neermattam/gateway/internal/web"
)

func (a *App) page(w http.ResponseWriter, r *http.Request, active string, needsMap bool) *web.Page {
	lang := langFrom(r.Context())
	city := a.cityFor(w, r)
	info, _ := i18n.Get(lang)
	p := &web.Page{
		Request:     r,
		PublicURL:   a.Cfg.PublicURL,
		Title:       i18n.T(lang, "title."+active),
		Active:      active,
		Nonce:       nonceFrom(r.Context()),
		NeedsMap:    needsMap,
		Footer:      active == "report" || active == "about" || active == "home",
		Demo:        a.Cfg.Demo,
		Lang:        lang,
		LangNative:  info.Native,
		LangFont:    info.Font,
		LangFontURL: fontURL(info.Font),
		Path:        r.URL.Path,
		Limits: web.Limits{
			ImageMB:      int(a.Cfg.MaxImageBytes >> 20),
			VideoMB:      int(a.Cfg.MaxVideoBytes >> 20),
			AudioMB:      int(a.Cfg.MaxAudioBytes >> 20),
			VideoSeconds: int(a.Cfg.MaxVideoSeconds),
		},
	}
	for _, l := range i18n.Languages {
		q := r.URL.Query()
		q.Set("lang", l.Code)
		p.LangLinks = append(p.LangLinks, web.LangLink{Code: l.Code, Name: l.Name, Native: l.Native, Href: r.URL.Path + "?" + q.Encode(), Current: l.Code == lang})
	}
	p.City = web.NewCityView(city.ID, city.Name, city.BBox, city.CentreLat, city.CentreLon, city.Cells, city.Routing, a.simulatedIn(city.ID))
	for _, c := range a.Cities.All() {
		q := r.URL.Query()
		for _, k := range []string{"at", "from", "to", "cell"} {
			q.Del(k) // points belong to the old city
		}
		q.Set("city", c.ID)
		p.CityLinks = append(p.CityLinks, web.CityLink{ID: c.ID, Href: r.URL.Path + "?" + q.Encode(), Current: c.ID == city.ID, Simulated: a.simulatedIn(c.ID)})
	}
	return p
}

func (a *App) render(w http.ResponseWriter, status int, name string, p *web.Page) {
	if err := a.Renderer.Render(w, p.Request, status, name, p); err != nil {
		a.Log.Error("render failed", "page", name, "error", err.Error())
	}
}

// mapPage is GET /map. Stats are filled server-side when the engine answers
// quickly, so the page reads correctly before any script runs.
func (a *App) mapPage(w http.ResponseWriter, r *http.Request) {
	p := a.page(w, r, "map", true)
	p.Stats = web.MapStats{Cells: "0", Deep: "0", Max: i18n.T(p.Lang, "js.none")}
	ctx, cancel := context.WithTimeout(r.Context(), 1500*time.Millisecond)
	defer cancel()
	if e, err := a.cellsForCity(ctx, p.City.ID); err == nil {
		// The first paint needs no second round trip: the cells ride in
		// the page. json.Marshal escapes <, > and &, so the JSON cannot
		// close its script element.
		p.CellsJSON = template.JS(e.body)
		p.CellsETag = e.etag
		if cells, err := upstream.DecodeCells(e.body); err == nil {
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
			p.Stats.Empty = len(cells) == 0
			p.Stats.Deep = strconv.Itoa(deep)
			if known > 0 {
				p.Stats.Max = fmt.Sprintf("%.0f cm", maxD)
			}
		}
	}
	p.Feed = a.Hub.Feed(p.City.ID)
	if len(p.Feed) > 5 {
		p.Feed = p.Feed[:5]
	}
	a.render(w, http.StatusOK, "map", p)
}

func (a *App) reportPage(w http.ResponseWriter, r *http.Request) {
	p := a.page(w, r, "report", true)
	p.LiveTrace = &web.TraceView{Live: true}
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
	streaming := strings.Contains(r.Header.Get("Accept"), "text/event-stream")
	fail := func(re *report.Error) {
		if streaming {
			html, _ := a.Renderer.Fragment(langFrom(r.Context()), "result_card", web.ErrorView(langFrom(r.Context()), re))
			w.Header().Set("Content-Type", "text/event-stream")
			w.WriteHeader(re.Status)
			writeEvent(w, "error", map[string]any{"code": re.Code, "error": re.Message, "html": html})
			return
		}
		p := a.page(w, r, "report", true)
		p.LiveTrace = &web.TraceView{Live: true}
		p.Report.Error = web.ErrorText(p.Lang, re)
		a.render(w, re.Status, "report", p)
	}
	if ok, wait := a.reportLimit.Allow(clientKey(r, a.Cfg.TrustProxy)); !ok {
		a.Metrics.RateLimited.Inc("report")
		w.Header().Set("Retry-After", strconv.Itoa(int(wait.Seconds()+0.999)))
		fail(&report.Error{Status: http.StatusTooManyRequests, Code: "rate_limited", Key: "rate_limited", Args: []any{int(wait.Seconds() + 0.999)}, Message: fmt.Sprintf("You have sent several reports in a short time. Please wait %d seconds and try again.", int(wait.Seconds()+0.999))})
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
		p := a.page(w, r, "report", true)
		p.LiveTrace = &web.TraceView{Live: true}
		p.Report.Lat, p.Report.Lon = strconv.FormatFloat(sub.Lat, 'f', 6, 64), strconv.FormatFloat(sub.Lon, 'f', 6, 64)
		out, err := a.Pipeline.Run(r.Context(), sub, nil)
		if err != nil {
			re := asReportError(err)
			p.Result = web.ErrorView(p.Lang, re)
			a.render(w, re.Status, "report", p)
			return
		}
		p.Result = web.NewResultView(out, p.Lang)
		p.Trace = p.Result.Trace
		a.render(w, http.StatusOK, "report", p)
		return
	}

	// The browser posts the form and reads this response as server-sent
	// events (EventSource cannot upload a file): one "stage" event per
	// pipeline stage, one "step" event per agent step as perception makes
	// it, and a final "done" with the result card and the full trace, all
	// rendered here.
	lang := langFrom(r.Context())
	rc := http.NewResponseController(w)
	w.Header().Set("Content-Type", "text/event-stream")
	w.Header().Set("Cache-Control", "no-store")
	w.Header().Set("X-Accel-Buffering", "no")
	w.WriteHeader(http.StatusOK)
	var mu sync.Mutex
	send := func(event string, v map[string]any) {
		mu.Lock()
		defer mu.Unlock()
		writeEvent(w, event, v)
		_ = rc.Flush()
	}
	send("stage", map[string]any{"stage": report.StageReceived})
	steps := 0
	out, err := a.Pipeline.Run(r.Context(), sub, func(ev report.Event) {
		if ev.Stage == report.StageStep && ev.Step != nil {
			st := web.NewTraceStep(lang, steps, *ev.Step)
			st.DelayMs = 0
			steps++
			html, ferr := a.Renderer.Fragment(lang, "trace_step", st)
			if ferr != nil {
				a.Log.Error("trace step render failed", "error", ferr.Error())
				return
			}
			send("step", map[string]any{"html": html, "tone": st.Tone, "index": st.Index})
			return
		}
		send("stage", map[string]any{"stage": ev.Stage})
	})
	var view *web.ResultView
	final := map[string]any{}
	event := "done"
	if err != nil {
		re := asReportError(err)
		view = web.ErrorView(lang, re)
		event = "error"
		final["code"], final["error"] = re.Code, re.Message
	} else {
		view = web.NewResultView(out, lang)
		final["status"] = out.Status
	}
	html, rerr := a.Renderer.Fragment(lang, "result_card", view)
	if rerr != nil {
		a.Log.Error("result render failed", "error", rerr.Error())
	}
	final["html"] = html
	if view.Trace != nil {
		// Steps already shown live are not animated again.
		view.Trace.Reveal = steps == 0
		if th, terr := a.Renderer.Fragment(lang, "trace_panel", view.Trace); terr == nil {
			final["trace_html"] = th
		}
	}
	final["streamed_steps"] = steps
	send(event, final)
}

// writeEvent writes one server-sent event with a JSON payload.
func writeEvent(w io.Writer, event string, v any) {
	b, _ := json.Marshal(v)
	fmt.Fprintf(w, "event: %s\ndata: %s\n\n", event, b)
}

func clientKey(r *http.Request, trust bool) string {
	return ratelimit.ClientIP(r, trust)
}

func (a *App) routeForm(r *http.Request, city cities.City) web.RouteForm {
	f := web.RouteForm{Vehicle: "two_wheeler", Presets: city.Presets, Vehicles: web.Vehicles}
	if len(city.Presets) >= 2 {
		f.From, f.To = city.Presets[0].Coord(), city.Presets[1].Coord()
	} else {
		f.From = fmt.Sprintf("%.4f,%.4f", city.CentreLat, city.CentreLon)
		f.To = f.From
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

// planRoute runs a route in the page's city and builds the summary view.
// Errors come back in the page's language.
func (a *App) planRoute(ctx context.Context, r *http.Request, city string) (*web.RouteView, int, string) {
	lang := langFrom(r.Context())
	q, perr := a.parseRoute(r, city)
	if perr != nil {
		return nil, perr.Status, perr.Text(lang)
	}
	raw, err := a.routeJSON(ctx, q)
	if err != nil {
		re := a.engineRouteError(err, q.City)
		return nil, re.Status, re.Text(lang)
	}
	v, err := web.NewRouteView(raw, q.Vehicle, a.simulatedIn(q.City), lang)
	if err != nil {
		return nil, http.StatusBadGateway, i18n.T(lang, "err.route_down")
	}
	if a.Alerts != nil {
		v.WatchHref = "/alerts?" + url.Values{"city": {q.City}, "from": {q.From}, "to": {q.To}, "vehicle": {q.Vehicle}}.Encode()
	}
	return v, http.StatusOK, ""
}

// routePage is GET /route; with from, to and vehicle it also plans.
func (a *App) routePage(w http.ResponseWriter, r *http.Request) {
	p := a.page(w, r, "route", true)
	city, _ := a.Cities.Get(p.City.ID)
	p.Route = a.routeForm(r, city)
	status := http.StatusOK
	q := r.URL.Query()
	if q.Get("from") != "" && q.Get("to") != "" {
		if !a.limit(w, r, a.apiLimit, "route") {
			return
		}
		v, code, msg := a.planRoute(r.Context(), r, city.ID)
		if msg != "" {
			p.Route.Error = msg
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
	city := a.cityFor(nil, r)
	v, code, msg := a.planRoute(r.Context(), r, city.ID)
	if msg != "" {
		writeJSON(w, code, map[string]string{"error": msg})
		return
	}
	html, err := a.Renderer.Fragment(langFrom(r.Context()), "route_summary", v)
	if err != nil {
		a.Log.Error("route summary render failed", "error", err.Error())
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "The summary could not be rendered."})
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"summary_html": html, "route": json.RawMessage(v.GeoJSON)})
}

func (a *App) aboutPage(w http.ResponseWriter, r *http.Request) {
	p := a.page(w, r, "about", false)
	p.Home = &web.HomeView{Facts: &a.Facts}
	a.render(w, http.StatusOK, "about", p)
}

// homePage is GET /: the landing page. Old links to /?at=lat,lon, such as
// those in WhatsApp replies, go to the map.
func (a *App) homePage(w http.ResponseWriter, r *http.Request) {
	if r.URL.Query().Get("at") != "" {
		http.Redirect(w, r, "/map?"+r.URL.RawQuery, http.StatusFound)
		return
	}
	p := a.page(w, r, "home", false)
	h := &web.HomeView{Facts: &a.Facts, Trace: web.NewHomeTrace(p.Lang, a.Facts.Trace)}
	ctx, cancel := context.WithTimeout(r.Context(), 1200*time.Millisecond)
	defer cancel()
	if e, err := a.cellsForCity(ctx, p.City.ID); err == nil {
		h.Preview = e.Preview()
	} else {
		c, _ := a.Cities.Get(p.City.ID)
		h.Preview = web.NewPreview(nil, web.FrameFor(c.ID, c.BBox.MinLon, c.BBox.MinLat, c.BBox.MaxLon, c.BBox.MaxLat))
	}
	p.Home = h
	a.render(w, http.StatusOK, "home", p)
}

func capitalise(s string) string {
	if s == "" {
		return s
	}
	return strings.ToUpper(s[:1]) + s[1:]
}

// fontURL is the Google Fonts stylesheet for one family. The family comes
// from the fixed language table, never from the request, so the URL is
// trusted; html/template would otherwise escape the + separators to %2b,
// which Google Fonts rejects with a 400.
func fontURL(family string) template.URL {
	if family == "" {
		return ""
	}
	return template.URL("https://fonts.googleapis.com/css2?family=" + strings.ReplaceAll(family, " ", "+") + ":wght@400;500;600;700&display=optional")
}
