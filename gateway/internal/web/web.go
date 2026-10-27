// Package web renders the gateway's pages with html/template and serves the
// embedded static files. The only script on any page is the vendored
// MapLibre GL JS plus small inline glue carrying a per-request CSP nonce.
package web

import (
	"bytes"
	"crypto/rand"
	"crypto/sha256"
	"embed"
	"encoding/base64"
	"encoding/hex"
	"fmt"
	"html/template"
	"io"
	"io/fs"
	"net/http"
	"strings"
	"time"

	"neermattam/gateway/internal/live"
	"neermattam/gateway/internal/places"
)

//go:embed templates/*.html
var templateFS embed.FS

//go:embed static
var staticFS embed.FS

// Page is the data every page template receives.
type Page struct {
	Title      string
	Active     string
	Nonce      string
	CSSVersion string
	NeedsMap   bool
	Footer     bool
	Demo       bool

	Stats     MapStats
	Feed      []live.FeedItem
	Limits    Limits
	Report    ReportForm
	Result    *ResultView
	Trace     *TraceView
	LiveTrace *TraceView
	Route     RouteForm
}

// MapStats are the counters shown before the live data loads.
type MapStats struct {
	Cells, Deep, Max string
}

// Limits are the upload limits shown on the report page.
type Limits struct {
	ImageMB, VideoMB, AudioMB, VideoSeconds int
}

// ReportForm keeps the report form's values.
type ReportForm struct {
	Lat, Lon string
	Error    string
}

// VehicleOption is one vehicle radio button.
type VehicleOption struct {
	ID, Name string
}

// RouteForm is the route page's state.
type RouteForm struct {
	From, To, Vehicle string
	Error             string
	Presets           []places.Place
	Vehicles          []VehicleOption
	View              *RouteView
}

// Vehicles in display order.
var Vehicles = []VehicleOption{
	{"pedestrian", "Walking"},
	{"two_wheeler", "Two-wheeler"},
	{"auto", "Auto"},
	{"car", "Car"},
	{"bus", "Bus"},
}

// Renderer holds the parsed templates.
type Renderer struct {
	pages      map[string]*template.Template
	partials   *template.Template
	cssVersion string
}

var funcs = template.FuncMap{
	"depthColor": DepthColor,
	"ago":        Ago,
	"capitalise": capitalise,
}

// NewRenderer parses every template once at start-up.
func NewRenderer() (*Renderer, error) {
	base, err := template.New("base").Funcs(funcs).ParseFS(templateFS,
		"templates/layout.html", "templates/mapcore.html", "templates/result.html", "templates/trace.html")
	if err != nil {
		return nil, err
	}
	r := &Renderer{pages: map[string]*template.Template{}, partials: base}
	for _, name := range []string{"map", "report", "route", "about"} {
		t, err := base.Clone()
		if err != nil {
			return nil, err
		}
		if _, err := t.ParseFS(templateFS, "templates/"+name+".html"); err != nil {
			return nil, fmt.Errorf("template %s: %w", name, err)
		}
		r.pages[name] = t
	}
	// The route summary partial is defined in route.html.
	r.partials = r.pages["route"]
	css, err := staticFS.ReadFile("static/app.css")
	if err != nil {
		return nil, err
	}
	sum := sha256.Sum256(css)
	r.cssVersion = hex.EncodeToString(sum[:6])
	return r, nil
}

// Render writes a full page. Rendering goes to a buffer first so a
// template error becomes a clean 500 rather than half a page.
func (r *Renderer) Render(w http.ResponseWriter, status int, name string, p *Page) error {
	t, ok := r.pages[name]
	if !ok {
		return fmt.Errorf("no page %q", name)
	}
	p.CSSVersion = r.cssVersion
	var buf bytes.Buffer
	if err := t.ExecuteTemplate(&buf, "layout", p); err != nil {
		http.Error(w, "page could not be rendered", http.StatusInternalServerError)
		return err
	}
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	w.Header().Set("Cache-Control", "no-store")
	w.WriteHeader(status)
	_, err := buf.WriteTo(w)
	return err
}

// Fragment renders a named partial to a string.
func (r *Renderer) Fragment(name string, data any) (string, error) {
	var buf bytes.Buffer
	if err := r.partials.ExecuteTemplate(&buf, name, data); err != nil {
		return "", err
	}
	return buf.String(), nil
}

// Static serves the embedded static directory with long-lived caching: the
// vendor path carries the MapLibre version and app.css is requested with a
// content hash, so a changed file always has a new URL.
func Static() http.Handler {
	sub, err := fs.Sub(staticFS, "static")
	if err != nil {
		panic(err)
	}
	files := &gzipStatic{fsys: sub, next: http.FileServerFS(sub), cache: map[string][]byte{}}
	return http.StripPrefix("/static/", http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if strings.HasSuffix(r.URL.Path, "/") || r.URL.Path == "" {
			http.NotFound(w, r)
			return
		}
		w.Header().Set("Cache-Control", "public, max-age=31536000, immutable")
		files.ServeHTTP(w, r)
	}))
}

// StaticFile returns an embedded static file, for tests.
func StaticFile(name string) ([]byte, error) {
	return staticFS.ReadFile("static/" + name)
}

// NewNonce returns a fresh CSP nonce.
func NewNonce() string {
	var b [16]byte
	if _, err := io.ReadFull(rand.Reader, b[:]); err != nil {
		return fmt.Sprintf("n%d", time.Now().UnixNano())
	}
	return base64.RawURLEncoding.EncodeToString(b[:]) // no + or /, which html/template would escape
}

// CSP is the Content-Security-Policy for a page with this nonce. MapLibre
// needs blob: workers and fetches style, tiles, glyphs and sprites from
// OpenFreeMap; nothing else leaves the origin.
func CSP(nonce string) string {
	return strings.Join([]string{
		"default-src 'self'",
		"script-src 'self' 'nonce-" + nonce + "'",
		"style-src 'self' 'unsafe-inline'",
		"img-src 'self' data: blob: https://tiles.openfreemap.org",
		"connect-src 'self' https://tiles.openfreemap.org",
		"worker-src 'self' blob:",
		"child-src blob:",
		"font-src 'self'",
		"media-src 'self' blob:",
		"frame-ancestors 'none'",
		"base-uri 'self'",
		"form-action 'self'",
		"object-src 'none'",
	}, "; ")
}
