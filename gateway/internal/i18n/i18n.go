// Package i18n holds the gateway's user-facing text in eight languages and
// picks a language per request: ?lang= first (remembered in a cookie), then
// the cookie, then the browser's Accept-Language, then English.
//
// Messages are Go maps, one file per language. A key missing from a
// language falls back to English; tests check that none is missing.
// Numbers and units stay as numerals. Model output, place names and the
// routing engine's own explanation are not translated.
package i18n

import (
	"fmt"
	"html/template"
	"net/http"
	"sort"
	"strconv"
	"strings"
	"time"
)

// Lang is a supported language.
type Lang struct {
	Code   string // BCP 47 primary tag
	Name   string // in English
	Native string // in its own script
	// Font is the Google Fonts family for the script, empty for Latin.
	Font string
}

// Languages in menu order.
var Languages = []Lang{
	{"en", "English", "English", ""},
	{"ta", "Tamil", "தமிழ்", "Noto Sans Tamil"},
	{"hi", "Hindi", "हिन्दी", "Noto Sans Devanagari"},
	{"te", "Telugu", "తెలుగు", "Noto Sans Telugu"},
	{"kn", "Kannada", "ಕನ್ನಡ", "Noto Sans Kannada"},
	{"ml", "Malayalam", "മലയാളം", "Noto Sans Malayalam"},
	{"bn", "Bengali", "বাংলা", "Noto Sans Bengali"},
	{"mr", "Marathi", "मराठी", "Noto Sans Devanagari"},
}

// Catalogs maps a language code to its messages.
var Catalogs = map[string]map[string]string{
	"en": merge(enPages, enOps), "ta": merge(ta, taOps), "hi": merge(hi, hiOps), "te": merge(te, teOps),
	"kn": merge(kn, knOps), "ml": merge(ml, mlOps), "bn": merge(bn, bnOps), "mr": merge(mr, mrOps),
}

// merge joins a language's two message files: the pages
// (messages_<code>.go) and the city, control room, alerts and developer
// texts (messages_<code>_ops.go).
func merge(parts ...map[string]string) map[string]string {
	out := map[string]string{}
	for _, p := range parts {
		for k, v := range p {
			out[k] = v
		}
	}
	return out
}

// Default is the fallback language.
const Default = "en"

// CookieName remembers the chosen language.
const CookieName = "nm_lang"

// Get returns a language by code.
func Get(code string) (Lang, bool) {
	for _, l := range Languages {
		if l.Code == code {
			return l, true
		}
	}
	return Lang{}, false
}

// T returns the message for key in lang, formatted with args. A missing key
// falls back to English, and a key missing there is returned as is so the
// gap is visible rather than silent.
func T(lang, key string, args ...any) string {
	msg, ok := Catalogs[lang][key]
	if !ok {
		msg, ok = Catalogs[Default][key]
		if !ok {
			return key
		}
	}
	if len(args) == 0 {
		return msg
	}
	return fmt.Sprintf(msg, args...)
}

// HTML is T for messages that carry trusted markup (keys ending in ".html").
func HTML(lang, key string, args ...any) template.HTML {
	for i, a := range args {
		if s, ok := a.(string); ok {
			args[i] = template.HTMLEscapeString(s)
		}
	}
	return template.HTML(T(lang, key, args...)) //nolint:gosec // our own catalog
}

// Funcs returns the template functions bound to one language.
func Funcs(lang string) template.FuncMap {
	return template.FuncMap{
		"t":    func(key string, args ...any) string { return T(lang, key, args...) },
		"th":   func(key string, args ...any) template.HTML { return HTML(lang, key, args...) },
		"lang": func() string { return lang },
	}
}

// Detect picks the language for a request and reports whether it came from
// ?lang= (so the caller can remember it in a cookie).
func Detect(r *http.Request) (string, bool) {
	if q := strings.ToLower(r.URL.Query().Get("lang")); q != "" {
		if _, ok := Get(q); ok {
			return q, true
		}
	}
	if c, err := r.Cookie(CookieName); err == nil {
		if _, ok := Get(c.Value); ok {
			return c.Value, false
		}
	}
	return FromAcceptLanguage(r.Header.Get("Accept-Language")), false
}

// FromAcceptLanguage returns the best supported language for the header.
func FromAcceptLanguage(h string) string {
	type cand struct {
		code string
		q    float64
		pos  int
	}
	var cs []cand
	for i, part := range strings.Split(h, ",") {
		tag, params, _ := strings.Cut(strings.TrimSpace(part), ";")
		tag = strings.ToLower(strings.TrimSpace(tag))
		if tag == "" {
			continue
		}
		q := 1.0
		if v, ok := strings.CutPrefix(strings.TrimSpace(params), "q="); ok {
			if f, err := strconv.ParseFloat(v, 64); err == nil {
				q = f
			}
		}
		primary, _, _ := strings.Cut(tag, "-")
		if _, ok := Get(primary); ok && q > 0 {
			cs = append(cs, cand{primary, q, i})
		}
	}
	sort.SliceStable(cs, func(i, j int) bool { return cs[i].q > cs[j].q })
	if len(cs) > 0 {
		return cs[0].code
	}
	return Default
}

// Remember sets the language cookie for a year.
func Remember(w http.ResponseWriter, lang string) {
	http.SetCookie(w, &http.Cookie{
		Name: CookieName, Value: lang, Path: "/", MaxAge: 365 * 24 * 3600,
		SameSite: http.SameSiteLaxMode, HttpOnly: true,
	})
}

// Month returns a localised short month name.
func Month(lang string, m time.Month) string {
	return T(lang, "month."+strconv.Itoa(int(m)))
}
