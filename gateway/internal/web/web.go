// Package web renders the gateway's pages with html/template and serves the
// embedded static files. The only script on any page is the vendored
// MapLibre GL JS plus small inline glue carrying a per-request CSP nonce.
package web

import (
	"bytes"
	"crypto/rand"
	"embed"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"html/template"
	"io"
	"net/http"
	"strings"
	"time"

	"neermattam/gateway/internal/i18n"
	"neermattam/gateway/internal/live"
	"neermattam/gateway/internal/places"
)

//go:embed templates/*.html
var templateFS embed.FS

//go:embed static
var staticFS embed.FS

// Page is the data every page template receives.
type Page struct {
	// Request is the request being answered (for content negotiation).
	Request   *http.Request
	Title     string
	Active    string
	Nonce     string
	PublicURL string

	// Language.
	Lang        string
	LangNative  string
	LangFont    string       // Google Fonts family for Indic scripts, empty for English
	LangFontURL template.URL // Google Fonts stylesheet for the script, built from i18n.Languages
	LangLinks   []LangLink
	Path        string
	ClientText  template.JS // interface text for the inline glue, as JSON
	NeedsMap    bool
	Footer      bool
	Demo        bool

	Stats     MapStats
	CellsJSON template.JS // initial cells, inlined so the map paints without a second request
	CellsETag string
	Feed      []live.FeedItem
	Limits    Limits
	Report    ReportForm
	Result    *ResultView
	Trace     *TraceView
	LiveTrace *TraceView
	Route     RouteForm
	Home      *HomeView

	// City.
	City      CityView
	CityLinks []CityLink

	Control *ControlView
	Alerts  *AlertsView
	Dev     *DevView

	// Sites is the Critical sites page (sites.go).
	Sites *SitesView
}

// LangLink is one entry of the language menu.
type LangLink struct {
	Code, Name, Native, Href string
	Current                  bool
}

// MapStats are the counters shown before the live data loads.
type MapStats struct {
	Cells, Deep, Max string
	Empty            bool // no reports at all in the city yet
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

// Renderer holds the parsed templates, one set per language, so every
// template can call {{t "nav.about"}} for the right language.
type Renderer struct {
	assets   *Assets
	pages    map[string]map[string]*template.Template
	partials map[string]*template.Template
	client   map[string]template.JS
}

// Page templates.
var pageNames = []string{"home", "map", "report", "route", "about", "control", "control_login", "alerts", "developers"}

func langFuncs(a *Assets, lang string) template.FuncMap {
	f := template.FuncMap{
		"depthColor": DepthColor,
		"capitalise": capitalise,
		"asset":      a.URL,
		"ago":        func(t time.Time) string { return Ago(lang, t) },
		"place":      func(p string) string { return Place(lang, p) },
	}
	for k, v := range i18n.Funcs(lang) {
		f[k] = v
	}
	return f
}

// NewRenderer parses every template for every language once at start-up.
func NewRenderer() (*Renderer, error) {
	a, err := LoadAssets()
	if err != nil {
		return nil, err
	}
	r := &Renderer{assets: a, pages: map[string]map[string]*template.Template{}, partials: map[string]*template.Template{}, client: map[string]template.JS{}}
	for _, l := range i18n.Languages {
		base, err := template.New("base").Funcs(langFuncs(a, l.Code)).ParseFS(templateFS,
			"templates/layout.html", "templates/mapcore.html", "templates/result.html", "templates/trace.html")
		if err != nil {
			return nil, err
		}
		r.pages[l.Code] = map[string]*template.Template{}
		for _, name := range pageNames {
			t, err := base.Clone()
			if err != nil {
				return nil, err
			}
			if _, err := t.ParseFS(templateFS, "templates/"+name+".html"); err != nil {
				return nil, fmt.Errorf("template %s: %w", name, err)
			}
			r.pages[l.Code][name] = t
		}
		// The route summary partial lives in route.html; the route set has
		// every partial.
		r.partials[l.Code] = r.pages[l.Code]["route"]
		r.client[l.Code] = clientText(l.Code)
	}
	return r, nil
}

// Assets returns the fingerprinted static files.
func (r *Renderer) Assets() *Assets { return r.assets }

// clientText is the interface text the inline glue needs, as JSON.
func clientText(lang string) template.JS {
	m := map[string]string{}
	for k := range i18n.Catalogs["en"] {
		if strings.HasPrefix(k, "js.") || strings.HasPrefix(k, "hazard.") || k == "sim.title_plain" {
			m[k] = i18n.T(lang, k)
		}
	}
	b, _ := json.Marshal(m)
	return template.JS(b) //nolint:gosec // json.Marshal escapes <, > and &
}

// Render writes a full page. Rendering goes to a buffer first so a
// template error becomes a clean 500 rather than half a page.
func (rd *Renderer) Render(w http.ResponseWriter, r *http.Request, status int, name string, p *Page) error {
	if _, ok := rd.pages[p.Lang]; !ok {
		p.Lang = i18n.Default
	}
	t, ok := rd.pages[p.Lang][name]
	if !ok {
		return fmt.Errorf("no page %q", name)
	}
	p.ClientText = rd.client[p.Lang]
	var buf bytes.Buffer
	if err := t.ExecuteTemplate(&buf, "layout", p); err != nil {
		http.Error(w, "page could not be rendered", http.StatusInternalServerError)
		return err
	}
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	w.Header().Set("Cache-Control", "no-store")
	// The language comes from ?lang=, the cookie or Accept-Language.
	AddVary(w.Header(), "Cookie")
	AddVary(w.Header(), "Accept-Language")
	gz := r != nil && AcceptsGzip(r) && buf.Len() > 1024
	AddVary(w.Header(), "Accept-Encoding")
	if gz {
		w.Header().Set("Content-Encoding", "gzip")
	}
	w.WriteHeader(status)
	if gz {
		_, err := w.Write(Compress(buf.Bytes()))
		return err
	}
	_, err := buf.WriteTo(w)
	return err
}

// Fragment renders a named partial to a string in a language.
func (r *Renderer) Fragment(lang, name string, data any) (string, error) {
	if _, ok := r.pages[lang]; !ok {
		lang = i18n.Default
	}
	t := r.partials[lang]
	if strings.HasPrefix(name, "control_") {
		// The control room's live panel lives in control.html.
		t = r.pages[lang]["control"]
	}
	var buf bytes.Buffer
	if err := t.ExecuteTemplate(&buf, name, data); err != nil {
		return "", err
	}
	return buf.String(), nil
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
		"style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
		"img-src 'self' data: blob: https://tiles.openfreemap.org",
		"connect-src 'self' https://tiles.openfreemap.org",
		"worker-src 'self' blob:",
		"child-src blob:",
		"font-src 'self' https://fonts.gstatic.com",
		"media-src 'self' blob:",
		"frame-ancestors 'none'",
		"base-uri 'self'",
		"form-action 'self'",
		"object-src 'none'",
	}, "; ")
}
