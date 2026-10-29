package app

import (
	"errors"
	"net/http"
	"strconv"
	"strings"

	"neermattam/gateway/internal/apikeys"
	"neermattam/gateway/internal/web"
)

// The developer API: /api/v1/route, /api/v1/cells and /api/v1/usage, for
// fleets and apps, with an API key per customer, a rate limit per key and
// usage counted per UTC day.

func (a *App) termsURL() string { return a.Cfg.PublicURL + "/developers#terms" }

// apiKey authenticates and rate-limits a developer request. It writes the
// error itself and returns ok=false when the request must stop.
func (a *App) apiKey(w http.ResponseWriter, r *http.Request) (apikeys.Key, bool) {
	secret := ""
	if h := r.Header.Get("Authorization"); len(h) > 7 && strings.EqualFold(h[:7], "Bearer ") {
		secret = strings.TrimSpace(h[7:])
	}
	if secret == "" {
		secret = strings.TrimSpace(r.Header.Get("X-API-Key"))
	}
	k, err := a.Keys.Check(secret)
	switch {
	case errors.Is(err, apikeys.ErrMissing):
		w.Header().Set("WWW-Authenticate", `Bearer realm="neermattam"`)
		writeJSON(w, http.StatusUnauthorized, map[string]string{"error": "Send your API key as Authorization: Bearer <key>. See " + a.Cfg.PublicURL + "/developers.", "code": "missing_key"})
		return k, false
	case errors.Is(err, apikeys.ErrDisabled):
		writeJSON(w, http.StatusForbidden, map[string]string{"error": "This API key is disabled. Contact the operator of this gateway.", "code": "key_disabled"})
		return k, false
	case err != nil:
		w.Header().Set("WWW-Authenticate", `Bearer realm="neermattam", error="invalid_token"`)
		writeJSON(w, http.StatusUnauthorized, map[string]string{"error": "This API key is not valid.", "code": "invalid_key"})
		return k, false
	}
	ok, remaining, retry := a.Keys.Allow(k)
	w.Header().Set("X-RateLimit-Limit", strconv.Itoa(k.RatePerMin))
	w.Header().Set("X-RateLimit-Remaining", strconv.Itoa(remaining))
	if !ok {
		secs := int(retry.Seconds() + 0.999)
		w.Header().Set("Retry-After", strconv.Itoa(secs))
		a.Metrics.RateLimited.Inc("api_v1")
		writeJSON(w, http.StatusTooManyRequests, map[string]string{"error": "Rate limit reached for this key (" + strconv.Itoa(k.RatePerMin) + " requests per minute). Retry after " + strconv.Itoa(secs) + " seconds.", "code": "rate_limited"})
		return k, false
	}
	return k, true
}

// devRoute is GET /api/v1/route.
func (a *App) devRoute(w http.ResponseWriter, r *http.Request) {
	k, ok := a.apiKey(w, r)
	if !ok {
		return
	}
	body, rerr := a.routeFor(r)
	if rerr != nil {
		writeJSON(w, rerr.Status, map[string]string{"error": rerr.Msg, "code": rerr.Code})
		return
	}
	a.Keys.Record(k.ID, "routes")
	w.Header().Set("Content-Type", "application/geo+json")
	w.Header().Set("Cache-Control", "no-store")
	web.WriteBody(w, r, withTerms(body, a.termsURL()), nil)
}

// withTerms adds top-level advisory and terms members to a JSON object.
func withTerms(body []byte, terms string) []byte {
	if len(body) < 2 || body[0] != '{' {
		return body
	}
	head := `{"advisory":true,"terms":"` + terms + `",`
	if string(body[:2]) == "{}" {
		head = head[:len(head)-1]
	}
	out := make([]byte, 0, len(body)+len(head))
	out = append(out, head...)
	return append(out, body[1:]...)
}

// devCells is GET /api/v1/cells.
func (a *App) devCells(w http.ResponseWriter, r *http.Request) {
	k, ok := a.apiKey(w, r)
	if !ok {
		return
	}
	ans, code, msg := a.cellsRequest(r)
	if code != 0 {
		writeJSON(w, code, map[string]string{"error": msg, "code": "bad_request"})
		return
	}
	if ans.err != nil {
		a.upstreamError(w, ans.err)
		return
	}
	e := ans.entry
	e.devOnce.Do(func() {
		e.devBody = withTerms(e.body, a.termsURL())
		e.devGz = web.Compress(e.devBody)
	})
	a.Keys.Record(k.ID, "cells")
	h := w.Header()
	h.Set("ETag", e.etag)
	h.Set("Cache-Control", "private, max-age=2")
	web.AddVary(h, "Accept-Encoding")
	if etagMatch(r.Header.Get("If-None-Match"), e.etag) {
		w.WriteHeader(http.StatusNotModified)
		return
	}
	h.Set("Content-Type", "application/geo+json")
	web.WriteBody(w, r, e.devBody, e.devGz)
}

// devUsage is GET /api/v1/usage: the key's own counts.
func (a *App) devUsage(w http.ResponseWriter, r *http.Request) {
	k, ok := a.apiKey(w, r)
	if !ok {
		return
	}
	u, days := a.Keys.Usage(k.ID)
	if days == nil {
		days = []apikeys.DayCount{}
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"key": k.ID, "name": k.Name, "rate_per_min": k.RatePerMin,
		"total": u.Total, "days": days, "last_used": u.LastUsed,
	})
}

// developersPage is GET /developers.
func (a *App) developersPage(w http.ResponseWriter, r *http.Request) {
	p := a.page(w, r, "developers", false)
	p.Footer = true
	p.Dev = &web.DevView{Base: a.Cfg.PublicURL, Cities: a.Cities.SortedIDs(), Keys: a.Keys.Count() > 0}
	a.render(w, http.StatusOK, "developers", p)
}
