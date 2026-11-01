package app

import (
	"net/http"
	"net/url"
	"strings"
)

// notFound answers a path no route matched. API paths, and clients that
// ask for JSON and not HTML, get the JSON error; everyone else gets the
// branded page in their language, with links to the main pages, and the
// 404 status.
func (a *App) notFound(w http.ResponseWriter, r *http.Request) {
	if wantsJSONError(r) {
		writeJSON(w, http.StatusNotFound, map[string]string{"error": "no such endpoint; see docs/CONTRACTS.md section 3", "code": "not_found"})
		return
	}
	p := a.page(nil, r, "notfound", false)
	p.Footer = true
	a.render(w, http.StatusNotFound, "notfound", p)
}

// wantsJSONError reports whether a 404 should be JSON: API and machine
// paths, or an Accept header that names JSON and not HTML.
func wantsJSONError(r *http.Request) bool {
	for _, prefix := range []string{"/api/", "/v1/", "/webhooks/", "/.well-known/", "/static/"} {
		if strings.HasPrefix(r.URL.Path, prefix) {
			return true
		}
	}
	accept := r.Header.Get("Accept")
	return strings.Contains(accept, "json") && !strings.Contains(accept, "text/html")
}

// toControl sends a GET of a control-room form action to the key page.
func toControl(w http.ResponseWriter, r *http.Request) {
	target := "/control"
	if q := r.URL.Query().Get("lang"); q != "" {
		target += "?lang=" + url.QueryEscape(q)
	}
	http.Redirect(w, r, target, http.StatusSeeOther)
}
