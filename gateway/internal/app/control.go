package app

import (
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/hex"
	"errors"
	"io"
	"net/http"
	"net/url"
	"regexp"
	"strings"
	"sync"
	"time"

	"neermattam/gateway/internal/cities"
	"neermattam/gateway/internal/i18n"
	"neermattam/gateway/internal/ratelimit"
	"neermattam/gateway/internal/upstream"
	"neermattam/gateway/internal/web"
)

// ControlCookie holds the control room session: an HMAC of a fixed label
// under CONTROL_ROOM_KEY, never the key itself. Changing the key ends every
// session.
const ControlCookie = "nm_ctl"

var loginLimit = struct {
	once sync.Once
	l    *ratelimit.Limiter
}{}

func controlLoginLimiter() *ratelimit.Limiter {
	loginLimit.once.Do(func() { loginLimit.l = ratelimit.New(5, 5) })
	return loginLimit.l
}

func (a *App) controlToken() string {
	m := hmac.New(sha256.New, []byte(a.Cfg.ControlRoomKey))
	m.Write([]byte("neermattam control room session v1"))
	return hex.EncodeToString(m.Sum(nil))
}

// controlAuthorized reports whether the request carries a valid session.
func (a *App) controlAuthorized(r *http.Request) bool {
	if a.Cfg.ControlRoomKey == "" {
		return false
	}
	c, err := r.Cookie(ControlCookie)
	if err != nil {
		return false
	}
	return subtle.ConstantTimeCompare([]byte(c.Value), []byte(a.controlToken())) == 1
}

func (a *App) secureCookies() bool { return strings.HasPrefix(a.Cfg.PublicURL, "https://") }

// controlPage is GET /control: the room with a session, else the key page.
func (a *App) controlPage(w http.ResponseWriter, r *http.Request) {
	if !a.controlAuthorized(r) {
		a.renderLogin(w, r, http.StatusOK, "")
		return
	}
	p := a.page(w, r, "control", false)
	p.Control = a.controlView(r.Context(), p.Lang, p.City.ID, "/control", false, cellParam(r))
	w.Header().Set("Cache-Control", "no-store")
	a.render(w, http.StatusOK, "control", p)
}

func (a *App) renderLogin(w http.ResponseWriter, r *http.Request, status int, errKey string) {
	p := a.page(w, r, "control", false)
	p.Title = i18n.T(p.Lang, "title.control_login")
	p.Control = &web.ControlView{Base: "/control", Demo: a.Cfg.Demo, Error: errKey}
	if a.Cfg.ControlRoomKey == "" {
		p.Control.Error = "ctl.login_off"
	}
	a.render(w, status, "control_login", p)
}

// controlLogin is POST /control/login.
func (a *App) controlLogin(w http.ResponseWriter, r *http.Request) {
	if ok, wait := controlLoginLimiter().Allow(clientKey(r, a.Cfg.TrustProxy)); !ok {
		a.Metrics.RateLimited.Inc("control_login")
		w.Header().Set("Retry-After", strings.TrimSuffix(wait.Round(time.Second).String(), "s"))
		a.renderLogin(w, r, http.StatusTooManyRequests, "ctl.login_slow")
		return
	}
	r.Body = http.MaxBytesReader(w, r.Body, 4096)
	key := strings.TrimSpace(r.PostFormValue("key"))
	if a.Cfg.ControlRoomKey == "" || subtle.ConstantTimeCompare([]byte(key), []byte(a.Cfg.ControlRoomKey)) != 1 {
		a.Log.Info("control room key refused")
		a.renderLogin(w, r, http.StatusUnauthorized, "ctl.login_wrong")
		return
	}
	http.SetCookie(w, &http.Cookie{
		Name: ControlCookie, Value: a.controlToken(), Path: "/control", MaxAge: 12 * 3600,
		HttpOnly: true, Secure: a.secureCookies(), SameSite: http.SameSiteStrictMode,
	})
	http.Redirect(w, r, "/control", http.StatusSeeOther)
}

// controlLogout is POST /control/logout.
func (a *App) controlLogout(w http.ResponseWriter, r *http.Request) {
	http.SetCookie(w, &http.Cookie{Name: ControlCookie, Value: "", Path: "/control", MaxAge: -1, HttpOnly: true, Secure: a.secureCookies(), SameSite: http.SameSiteStrictMode})
	http.Redirect(w, r, "/control", http.StatusSeeOther)
}

// controlDemoPage is GET /control/demo: the public, read-only room over the
// simulated Chennai scenario.
func (a *App) controlDemoPage(w http.ResponseWriter, r *http.Request) {
	if !a.Cfg.Demo {
		// The public room exists only with the simulated scenario (DEMO=1).
		a.notFound(w, r)
		return
	}
	q := r.URL.Query()
	q.Set("city", cities.Default) // the demo shows the scenario's city only
	r.URL.RawQuery = q.Encode()
	p := a.page(nil, r, "control", false)
	p.Control = a.controlView(r.Context(), p.Lang, cities.Default, "/control/demo", true, cellParam(r))
	a.render(w, http.StatusOK, "control", p)
}

var cellRe = regexp.MustCompile(`^[0-9a-f]{15}$`)

func cellParam(r *http.Request) string {
	c := strings.ToLower(r.URL.Query().Get("cell"))
	if cellRe.MatchString(c) {
		return c
	}
	return ""
}

// controlView asks the engine for the summary, the hotspots and, with a
// cell, its history, in parallel.
func (a *App) controlView(ctx context.Context, lang, city, base string, demo bool, cell string) *web.ControlView {
	ctx, cancel := context.WithTimeout(ctx, 4*time.Second)
	defer cancel()
	var (
		wg  sync.WaitGroup
		sum *upstream.Summary
		hs  *upstream.Hotspots
		rec *upstream.Recession
		e1  error
		e2  error
		e3  error
	)
	wg.Add(2)
	go func() { defer wg.Done(); sum, e1 = a.Engine.Summary(ctx, city) }()
	go func() { defer wg.Done(); hs, e2 = a.Engine.Hotspots(ctx, city, 15) }()
	if cell != "" {
		wg.Add(1)
		go func() { defer wg.Done(); rec, e3 = a.Engine.Recession(ctx, cell) }()
	}
	wg.Wait()
	v := web.NewControlView(lang, city, base, demo, sum, hs, cell, a.Origins.Label)
	if e1 != nil || e2 != nil {
		v.Error = "ctl.engine_down"
	}
	for _, c := range a.Cities.All() {
		if demo && c.ID != city {
			continue
		}
		v.Routing = append(v.Routing, web.RoutingView{City: c.ID, Status: c.Routing, Current: c.ID == city})
	}
	if cell != "" {
		closeHref := base
		if !demo {
			closeHref += "?city=" + url.QueryEscape(city)
		}
		if e3 != nil && !upstream.IsStatus(e3, http.StatusNotFound) {
			rec = nil
		}
		v.Cell = web.NewCellDetail(lang, city, cell, closeHref, rec)
	}
	return v
}

// controlPanel is GET /control/panel: the live parts re-rendered.
func (a *App) controlPanel(w http.ResponseWriter, r *http.Request) {
	if !a.controlAuthorized(r) {
		writeJSON(w, http.StatusUnauthorized, map[string]string{"error": "the control room needs its key", "code": "no_session"})
		return
	}
	city := a.cityFor(nil, r)
	a.writePanel(w, r, a.controlView(r.Context(), langFrom(r.Context()), city.ID, "/control", false, cellParam(r)))
}

func (a *App) controlDemoPanel(w http.ResponseWriter, r *http.Request) {
	if !a.Cfg.Demo {
		writeJSON(w, http.StatusNotFound, map[string]string{"error": "no demo on this gateway", "code": "no_demo"})
		return
	}
	if !a.limit(w, r, a.apiLimit, "api") {
		return
	}
	a.writePanel(w, r, a.controlView(r.Context(), langFrom(r.Context()), cities.Default, "/control/demo", true, cellParam(r)))
}

func (a *App) writePanel(w http.ResponseWriter, r *http.Request, v *web.ControlView) {
	html, err := a.Renderer.Fragment(langFrom(r.Context()), "control_panel", v)
	if err != nil {
		a.Log.Error("control panel render failed", "error", err.Error())
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "the panel could not be rendered", "code": "internal"})
		return
	}
	w.Header().Set("Cache-Control", "no-store")
	writeJSON(w, http.StatusOK, map[string]string{"html": html})
}

// controlExport is GET /control/export: the engine's export, passed through.
func (a *App) controlExport(w http.ResponseWriter, r *http.Request) {
	if !a.controlAuthorized(r) {
		http.Error(w, "The control room needs its key.", http.StatusUnauthorized)
		return
	}
	a.export(w, r, a.cityFor(nil, r).ID)
}

func (a *App) controlDemoExport(w http.ResponseWriter, r *http.Request) {
	if !a.Cfg.Demo {
		http.NotFound(w, r)
		return
	}
	if !a.limit(w, r, a.apiLimit, "api") {
		return
	}
	a.export(w, r, cities.Default)
}

func (a *App) export(w http.ResponseWriter, r *http.Request, city string) {
	format := r.URL.Query().Get("format")
	if format != "csv" {
		format = "geojson"
	}
	ctx, cancel := context.WithTimeout(r.Context(), 30*time.Second)
	defer cancel()
	resp, err := a.Engine.Export(ctx, city, format)
	if err != nil {
		var ue *upstream.Error
		if errors.As(err, &ue) {
			http.Error(w, "The export failed: "+ue.Message, http.StatusBadGateway)
			return
		}
		http.Error(w, "The map engine is not reachable right now. Try again shortly.", http.StatusServiceUnavailable)
		return
	}
	defer resp.Body.Close()
	h := w.Header()
	for _, k := range []string{"Content-Type", "Content-Disposition", "X-Neermattam-Truncated", "X-Neermattam-Total-Cells"} {
		if v := resp.Header.Get(k); v != "" {
			h.Set(k, v)
		}
	}
	h.Set("Cache-Control", "no-store")
	h.Set("X-Content-Type-Options", "nosniff")
	_, _ = io.Copy(w, resp.Body)
}
