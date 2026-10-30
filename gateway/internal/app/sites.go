package app

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"time"

	"neermattam/gateway/internal/i18n"
	"neermattam/gateway/internal/sites"
	"neermattam/gateway/internal/upstream"
	"neermattam/gateway/internal/web"
)

// Critical sites: the page, its public JSON, site alerts and the developer
// API (CONTRACTS.md section 3). Everything here talks to the engine's
// /v1/sites, /v1/sites/{id} and /v1/siting.

// sitesState holds the site watcher; nil fields mean site alerts are off.
type sitesState struct {
	store *sites.Store
	watch *sites.Service
}

// setupSites builds the site watch store and watcher. Site alerts use the
// street alerts' Web Push key, so they are on exactly when push alerts are.
func (a *App) setupSites() {
	cfg := a.Cfg
	a.Sites.store = sites.NewStore(cfg.AlertsMax, cfg.AlertsPerClient, cfg.AlertTTL)
	if a.Alerts == nil || a.Alerts.Pusher == nil {
		return
	}
	if path := a.siteAlertsPath(); path != "" {
		if n, err := a.Sites.store.Load(path); err != nil {
			aside := path + ".rejected-" + time.Now().UTC().Format("20060102T150405")
			_ = os.Rename(path, aside)
			a.Log.Warn("site alerts not loaded; starting with none", "error", err.Error(), "moved_to", aside)
		} else if n > 0 {
			a.Log.Info("site alerts loaded", "watches", n)
		}
	}
	sent := a.Metrics.Registry.NewCounterVec("neermattam_site_alerts_sent_total", "Site alert messages by kind and outcome.", "kind", "outcome")
	a.Metrics.Registry.NewGaugeFunc("neermattam_site_watches", "Site watches held.", func() float64 { return float64(a.Sites.store.Len()) })
	a.Sites.watch = &sites.Service{
		Store: a.Sites.store, Send: sites.PushSender(a.Alerts.Pusher), PublicURL: cfg.PublicURL, Log: a.Log,
		Sweep: max(cfg.AlertSweep, time.Minute), MinGap: cfg.AlertMinGap,
		Fetch: func(ctx context.Context, site string, threshold float64) (*upstream.SiteDetail, error) {
			return a.Engine.Site(ctx, site, url.Values{"threshold_cm": {strconv.FormatFloat(threshold, 'f', -1, 64)}})
		},
		OnSent: func(kind, outcome string) { sent.Inc(kind, outcome) },
	}
}

// registerSites adds the Critical sites routes.
func (a *App) registerSites(mux *http.ServeMux) {
	mux.HandleFunc("GET /sites", a.sitesPage)
	mux.HandleFunc("GET /api/sites/{id}", a.apiSite)
	mux.HandleFunc("POST /api/sites/alerts", a.apiSiteAlertCreate)
	mux.HandleFunc("GET /api/sites/alerts/{id}", a.apiSiteAlertGet)
	mux.HandleFunc("DELETE /api/sites/alerts/{id}", a.apiSiteAlertDelete)
	mux.HandleFunc("GET /api/v1/sites", a.devSites)
	mux.HandleFunc("POST /api/v1/sites", a.devSiteAdd)
	mux.HandleFunc("GET /api/v1/sites/{id}", a.devSite)
	mux.HandleFunc("DELETE /api/v1/sites/{id}", a.devSiteDelete)
	mux.HandleFunc("GET /api/v1/siting", a.devSiting)
}

// runSites starts the site watcher.
func (a *App) runSites(ctx context.Context) {
	if a.Sites.watch != nil {
		go a.Sites.watch.Run(ctx)
	}
}

func (a *App) siteAlertsPath() string {
	if a.Cfg.DataDir == "" {
		return ""
	}
	return filepath.Join(a.Cfg.DataDir, sites.FileName)
}

func (a *App) saveSiteAlerts() {
	if path := a.siteAlertsPath(); path != "" && a.Sites.store != nil && a.Sites.watch != nil {
		if err := a.Sites.store.Save(path); err != nil {
			a.Log.Warn("site alerts not saved", "error", err.Error())
		}
	}
}

func validKind(k string) bool {
	for _, x := range web.SiteKinds {
		if x == k {
			return true
		}
	}
	return false
}

// sitesPage is GET /sites.
func (a *App) sitesPage(w http.ResponseWriter, r *http.Request) {
	p := a.page(w, r, "sites", true)
	p.Footer = true
	q := r.URL.Query()
	kind := q.Get("kind")
	if !validKind(kind) {
		kind = "data_centre"
	}
	site := q.Get("site")
	if !strings.HasPrefix(site, "osm:") {
		site = "" // the public page never shows operator sites
	}
	city := p.City.ID
	ctx := r.Context()
	var (
		wg                sync.WaitGroup
		list              *upstream.SiteList
		detail            *upstream.SiteDetail
		siting, simSiting *upstream.Siting
		listErr           error
	)
	wg.Add(3)
	go func() {
		defer wg.Done()
		list, listErr = a.Engine.Sites(ctx, url.Values{"city": {city}, "kind": {kind}, "limit": {"60"}})
	}()
	go func() {
		defer wg.Done()
		if site != "" {
			if d, err := a.Engine.Site(ctx, site, nil); err == nil && d.City == city {
				detail = d
			}
		}
	}()
	go func() {
		defer wg.Done()
		siting, _ = a.Engine.SitingAreas(ctx, url.Values{"city": {city}, "limit": {"8"}})
		if a.simulatedIn(city) {
			simSiting, _ = a.Engine.SitingAreas(ctx, url.Values{"city": {city}, "limit": {"8"}, "bucket": {"unattributed"}})
		}
	}()
	wg.Wait()
	o := web.SitesOptions{Lang: p.Lang, City: city, Kind: kind}
	if detail != nil {
		o.Site = detail.ID
	}
	if listErr != nil {
		o.Error = "sites.err_engine"
		a.Log.Warn("sites page: engine not read", "error", listErr.Error())
	}
	if a.Sites.watch != nil {
		o.Push, o.VAPIDKey = true, a.Alerts.Pusher.VAPID.Public
	}
	p.Sites = web.NewSitesView(o, list, detail, siting, simSiting)
	w.Header().Set("Cache-Control", "no-store")
	a.render(w, http.StatusOK, "sites", p)
}

// siteError answers an engine failure for a site request.
func (a *App) siteError(w http.ResponseWriter, err error) {
	var ue *upstream.Error
	switch {
	case errors.As(err, &ue) && ue.Status == http.StatusNotFound:
		writeJSON(w, http.StatusNotFound, map[string]string{"error": "No such site.", "code": "no_site"})
	case errors.As(err, &ue) && ue.Status == http.StatusBadRequest:
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": capitalise(ue.Message) + ".", "code": "bad_request"})
	case errors.As(err, &ue) && ue.Status == http.StatusConflict:
		writeJSON(w, http.StatusConflict, map[string]string{"error": capitalise(ue.Message) + ".", "code": "too_many_sites"})
	default:
		a.upstreamError(w, err)
	}
}

// siteQuery copies the zone parameters a caller may set.
func siteQuery(r *http.Request, keys ...string) url.Values {
	out := url.Values{}
	for _, k := range keys {
		if v := r.URL.Query().Get(k); v != "" {
			out.Set(k, v)
		}
	}
	return out
}

// apiSite is GET /api/sites/{id}: one OpenStreetMap site, for the page's
// glue and for anyone who wants it as JSON.
func (a *App) apiSite(w http.ResponseWriter, r *http.Request) {
	if !a.limit(w, r, a.apiLimit, "api") {
		return
	}
	id := r.PathValue("id")
	if !strings.HasPrefix(id, "osm:") {
		writeJSON(w, http.StatusNotFound, map[string]string{"error": "No such site.", "code": "no_site"})
		return
	}
	raw, err := a.Engine.SiteRaw(r.Context(), id, siteQuery(r, "rings", "threshold_cm", "min_coverage"))
	if err != nil {
		a.siteError(w, err)
		return
	}
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("Cache-Control", "no-store")
	web.WriteBody(w, r, raw, nil)
}

type siteAlertRequest struct {
	Subscription struct {
		Endpoint string `json:"endpoint"`
		Keys     struct {
			P256dh string `json:"p256dh"`
			Auth   string `json:"auth"`
		} `json:"keys"`
	} `json:"subscription"`
	Site        string  `json:"site"`
	City        string  `json:"city"`
	Lang        string  `json:"lang"`
	ThresholdCm float64 `json:"threshold_cm"`
}

// apiSiteAlertCreate is POST /api/sites/alerts.
func (a *App) apiSiteAlertCreate(w http.ResponseWriter, r *http.Request) {
	if a.Sites.watch == nil {
		alertErr(w, http.StatusNotFound, "alerts_off", "Push alerts are off on this gateway.")
		return
	}
	if !a.limit(w, r, a.reportLimit, "alerts") {
		return
	}
	r.Body = http.MaxBytesReader(w, r.Body, 16<<10)
	var req siteAlertRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		alertErr(w, http.StatusBadRequest, "bad_json", "The request must be JSON with subscription, site, city and lang.")
		return
	}
	s := &sites.Sub{Endpoint: req.Subscription.Endpoint, P256dh: req.Subscription.Keys.P256dh, Auth: req.Subscription.Keys.Auth, ThresholdCm: req.ThresholdCm}
	if err := a.Alerts.Pusher.CheckEndpoint(s.Endpoint); err != nil {
		alertErr(w, http.StatusBadRequest, "bad_endpoint", capitalise(err.Error())+".")
		return
	}
	if !validKey(s.P256dh, 65) || !validKey(s.Auth, 16) {
		alertErr(w, http.StatusBadRequest, "bad_keys", "The subscription's p256dh and auth keys are missing or malformed.")
		return
	}
	if s.ThresholdCm == 0 {
		s.ThresholdCm = 15
	}
	if !sites.ValidThreshold(s.ThresholdCm) {
		alertErr(w, http.StatusBadRequest, "bad_watch", "threshold_cm must be from 5 to 100.")
		return
	}
	if !strings.HasPrefix(req.Site, "osm:") {
		alertErr(w, http.StatusBadRequest, "bad_watch", "site must be the id of an OpenStreetMap site shown on /sites.")
		return
	}
	d, err := a.Engine.Site(r.Context(), req.Site, url.Values{"access": {"false"}})
	if err != nil {
		a.siteError(w, err)
		return
	}
	if req.City != "" && !strings.EqualFold(req.City, d.City) {
		alertErr(w, http.StatusBadRequest, "bad_watch", "The site is not in that city.")
		return
	}
	lang := req.Lang
	if _, ok := i18n.Get(lang); !ok {
		lang = langFrom(r.Context())
	}
	s.Site, s.City, s.Lang, s.SiteName = d.ID, d.City, lang, d.Name
	token, st, err := a.Sites.watch.Watch(r.Context(), s)
	if err != nil {
		switch {
		case errors.Is(err, sites.ErrTooMany):
			alertErr(w, http.StatusConflict, "too_many", fmt.Sprintf("This browser already watches %d sites. Stop one first.", a.Cfg.AlertsPerClient))
		case errors.Is(err, sites.ErrFull):
			alertErr(w, http.StatusServiceUnavailable, "full", "The gateway holds as many watches as it can. Try again later.")
		default:
			alertErr(w, http.StatusInternalServerError, "internal", "The watch could not be saved.")
		}
		return
	}
	n := sites.Compose(*s, sites.Change{Zone: sites.KindWatching}, st, a.Cfg.PublicURL)
	writeJSON(w, http.StatusCreated, map[string]any{
		"id": s.ID, "token": token, "site": s.Site, "status": st.Zone, "depth_cm": st.DepthCm,
		"closed_routes": st.Closed, "message": n.Body, "expires_at": s.ExpiresAt,
	})
}

// apiSiteAlertGet is GET /api/sites/alerts/{id} with X-Alert-Token.
func (a *App) apiSiteAlertGet(w http.ResponseWriter, r *http.Request) {
	if !a.limit(w, r, a.apiLimit, "api") {
		return
	}
	s, err := a.Sites.store.Get(r.PathValue("id"), r.Header.Get("X-Alert-Token"))
	if err != nil {
		alertErr(w, http.StatusNotFound, "not_found", "No such watch, or the token does not match. Watches end after their expiry date.")
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"id": s.ID, "site": s.Site, "site_name": s.SiteName, "city": s.City, "threshold_cm": s.ThresholdCm,
		"status": s.Last.Zone, "depth_cm": s.Last.DepthCm, "closed_routes": s.Last.Closed, "expires_at": s.ExpiresAt, "sent": s.Sent,
	})
}

// apiSiteAlertDelete is DELETE /api/sites/alerts/{id} with X-Alert-Token.
func (a *App) apiSiteAlertDelete(w http.ResponseWriter, r *http.Request) {
	if !a.limit(w, r, a.apiLimit, "api") {
		return
	}
	if err := a.Sites.store.Delete(r.PathValue("id"), r.Header.Get("X-Alert-Token")); err != nil {
		alertErr(w, http.StatusNotFound, "not_found", "No such watch, or the token does not match.")
		return
	}
	w.WriteHeader(http.StatusNoContent)
}

// devJSON writes an engine answer with the advisory wrapper.
func (a *App) devJSON(w http.ResponseWriter, r *http.Request, raw []byte) {
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("Cache-Control", "no-store")
	web.WriteBody(w, r, withTerms(raw, a.termsURL()), nil)
}

// devCity is the city for a developer request: ?city=, else the default.
func (a *App) devCity(w http.ResponseWriter, r *http.Request) (string, bool) {
	c := strings.ToLower(r.URL.Query().Get("city"))
	if c == "" {
		c = "chennai"
	}
	if _, ok := a.Cities.Get(c); !ok {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "city must be one of " + strings.Join(a.Cities.SortedIDs(), ", ") + ".", "code": "bad_request"})
		return "", false
	}
	return c, true
}

// devSites is GET /api/v1/sites: OpenStreetMap sites and this key's own.
func (a *App) devSites(w http.ResponseWriter, r *http.Request) {
	k, ok := a.apiKey(w, r)
	if !ok {
		return
	}
	city, ok := a.devCity(w, r)
	if !ok {
		return
	}
	q := siteQuery(r, "kind", "rings", "threshold_cm", "min_coverage", "limit", "offset")
	q.Set("city", city)
	q.Set("owner", k.ID)
	raw, err := a.Engine.SitesRaw(r.Context(), q)
	if err != nil {
		a.siteError(w, err)
		return
	}
	a.Keys.Record(k.ID, "sites")
	a.devJSON(w, r, raw)
}

// devSite is GET /api/v1/sites/{id}.
func (a *App) devSite(w http.ResponseWriter, r *http.Request) {
	k, ok := a.apiKey(w, r)
	if !ok {
		return
	}
	q := siteQuery(r, "rings", "threshold_cm", "min_coverage", "access")
	q.Set("owner", k.ID)
	raw, err := a.Engine.SiteRaw(r.Context(), r.PathValue("id"), q)
	if err != nil {
		a.siteError(w, err)
		return
	}
	a.Keys.Record(k.ID, "sites")
	a.devJSON(w, r, raw)
}

// devSiteAdd is POST /api/v1/sites: a site owned by this key.
func (a *App) devSiteAdd(w http.ResponseWriter, r *http.Request) {
	k, ok := a.apiKey(w, r)
	if !ok {
		return
	}
	r.Body = http.MaxBytesReader(w, r.Body, 8<<10)
	var in struct {
		Kind     string   `json:"kind"`
		Name     string   `json:"name"`
		Lat      *float64 `json:"lat"`
		Lon      *float64 `json:"lon"`
		Operator *string  `json:"operator"`
	}
	if err := json.NewDecoder(r.Body).Decode(&in); err != nil || in.Lat == nil || in.Lon == nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "Send JSON with kind, name, lat and lon (and optionally operator).", "code": "bad_site"})
		return
	}
	body, _ := json.Marshal(map[string]any{"owner": k.ID, "kind": in.Kind, "name": in.Name, "lat": *in.Lat, "lon": *in.Lon, "operator": in.Operator})
	raw, err := a.Engine.AddSite(r.Context(), body)
	if err != nil {
		var ue *upstream.Error
		if errors.As(err, &ue) && ue.Status == http.StatusBadRequest {
			writeJSON(w, http.StatusBadRequest, map[string]string{"error": capitalise(ue.Message) + ".", "code": "bad_site"})
			return
		}
		a.siteError(w, err)
		return
	}
	a.Log.Info("operator site added", "key", k.ID)
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusCreated)
	_, _ = w.Write(withTerms(raw, a.termsURL()))
}

// devSiteDelete is DELETE /api/v1/sites/{id}.
func (a *App) devSiteDelete(w http.ResponseWriter, r *http.Request) {
	k, ok := a.apiKey(w, r)
	if !ok {
		return
	}
	if err := a.Engine.DeleteSite(r.Context(), r.PathValue("id"), k.ID); err != nil {
		a.siteError(w, err)
		return
	}
	w.WriteHeader(http.StatusNoContent)
}

// devSiting is GET /api/v1/siting: the flood record from real reports.
func (a *App) devSiting(w http.ResponseWriter, r *http.Request) {
	k, ok := a.apiKey(w, r)
	if !ok {
		return
	}
	city, ok := a.devCity(w, r)
	if !ok {
		return
	}
	q := siteQuery(r, "limit")
	q.Set("city", city)
	q.Set("bucket", "reported")
	raw, err := a.Engine.SitingRaw(r.Context(), q)
	if err != nil {
		a.siteError(w, err)
		return
	}
	a.Keys.Record(k.ID, "sites")
	a.devJSON(w, r, raw)
}
