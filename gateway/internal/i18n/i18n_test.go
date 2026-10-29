package i18n

import (
	"net/http/httptest"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"testing"
)

var verbRe = regexp.MustCompile(`%(\[\d\])?(\.\d)?[sdfq]`)

// verbs lists a message's placeholders. Explicit argument indexes (%[2]s)
// let a language reorder them, so they are compared by index.
func verbs(s string) []string {
	var out []string
	next := 1
	for _, m := range verbRe.FindAllStringSubmatch(s, -1) {
		idx := next
		if m[1] != "" {
			idx, _ = strconv.Atoi(m[1][1 : len(m[1])-1])
		}
		next = idx + 1
		out = append(out, strconv.Itoa(idx)+":"+regexp.MustCompile(`\[\d\]`).ReplaceAllString(m[0], ""))
	}
	sort.Strings(out)
	return out
}

var tagRe = regexp.MustCompile(`</?[a-z]+[^>]*>`)

// en is the merged English catalogue in the tests below.
var en = Catalogs[Default]

func TestEveryLanguageHasEveryKey(t *testing.T) {
	for _, l := range Languages {
		cat := Catalogs[l.Code]
		var missing, extra []string
		for k := range en {
			if _, ok := cat[k]; !ok {
				missing = append(missing, k)
			}
		}
		for k := range cat {
			if _, ok := en[k]; !ok {
				extra = append(extra, k)
			}
		}
		sort.Strings(missing)
		sort.Strings(extra)
		if len(missing) > 0 {
			t.Errorf("%s (%s) lacks %d keys: %v", l.Code, l.Name, len(missing), missing)
		}
		if len(extra) > 0 {
			t.Errorf("%s (%s) has keys English does not: %v", l.Code, l.Name, extra)
		}
	}
}

func TestTranslationsKeepPlaceholdersAndMarkup(t *testing.T) {
	for _, l := range Languages {
		for k, msg := range Catalogs[l.Code] {
			want := verbs(en[k])
			got := verbs(msg)
			if strings.Join(want, ",") != strings.Join(got, ",") {
				t.Errorf("%s %s: placeholders %v, English has %v", l.Code, k, got, want)
			}
			if strings.HasSuffix(k, ".html") {
				if a, b := len(tagRe.FindAllString(en[k], -1)), len(tagRe.FindAllString(msg, -1)); a != b {
					t.Errorf("%s %s: %d tags, English has %d", l.Code, k, b, a)
				}
			} else if strings.ContainsAny(msg, "<>") && !strings.ContainsAny(en[k], "<>") {
				t.Errorf("%s %s: markup in a plain message", l.Code, k)
			}
			if strings.ContainsAny(msg, "—→←") {
				t.Errorf("%s %s: em dash or arrow", l.Code, k)
			}
			if strings.TrimSpace(msg) == "" {
				t.Errorf("%s %s: empty", l.Code, k)
			}
		}
	}
}

// Every key the templates and code use must exist in English.
var keyRe = regexp.MustCompile(`(?:\{\{\s*th? |NM\.tr\(|\btr\(|i18n\.T\(lang, |T\(lang, )"([a-z0-9_.]+)"`)

func TestEveryUsedKeyExists(t *testing.T) {
	var files []string
	_ = filepath.Walk("..", func(p string, info os.FileInfo, err error) error {
		if err == nil && !info.IsDir() && (strings.HasSuffix(p, ".html") || strings.HasSuffix(p, ".go")) && !strings.HasSuffix(p, "_test.go") && !strings.Contains(p, "messages_") {
			files = append(files, p)
		}
		return nil
	})
	seen := 0
	for _, f := range files {
		b, _ := os.ReadFile(f)
		for _, m := range keyRe.FindAllStringSubmatch(string(b), -1) {
			seen++
			k := m[1]
			if strings.HasSuffix(k, ".") || strings.HasSuffix(k, "_") {
				continue // a prefix completed at run time, checked below
			}
			if _, ok := en[k]; !ok {
				t.Errorf("%s uses missing key %q", f, k)
			}
		}
	}
	if seen < 200 {
		t.Fatalf("only %d key uses found; the scan is broken", seen)
	}
	// Keys built at run time.
	dynamic := []string{
		"js.real_photo", "js.real_video", "js.real_voice",
		"rule.confidence_floor", "rule.photo_freshness", "rule.source", "rule.neighbour_agreement", "rule.rainfall_plausibility",
		"outcome.pass", "outcome.lower", "outcome.reject", "outcome.skip",
		"step.scene", "step.segment", "step.verify", "step.confirm", "step.refine", "step.giveup", "step.geometry", "step.estimate",
		"vehicle.pedestrian", "vehicle.two_wheeler", "vehicle.auto", "vehicle.car", "vehicle.bus",
		"timing.scene", "timing.masks", "timing.geometry",
		"title.home", "title.map", "title.report", "title.route", "title.about",
		"ref.two_wheeler_wheel", "ref.car_wheel", "ref.auto_wheel", "ref.bus_wheel", "ref.kerb", "ref.person", "ref.wheel",
	}
	for i := 1; i <= 12; i++ {
		dynamic = append(dynamic, "month."+strconv.Itoa(i))
	}
	for _, k := range dynamic {
		if _, ok := en[k]; !ok {
			t.Errorf("dynamic key %q missing", k)
		}
	}
}

func TestDetect(t *testing.T) {
	r := httptest.NewRequest("GET", "/?lang=ta", nil)
	if l, explicit := Detect(r); l != "ta" || !explicit {
		t.Fatalf("query: %s %v", l, explicit)
	}
	r = httptest.NewRequest("GET", "/", nil)
	r.Header.Set("Cookie", CookieName+"=ml")
	r.Header.Set("Accept-Language", "hi-IN,hi;q=0.9")
	if l, explicit := Detect(r); l != "ml" || explicit {
		t.Fatalf("cookie beats the header: %s", l)
	}
	for h, want := range map[string]string{
		"hi-IN,hi;q=0.9,en;q=0.8": "hi",
		"fr-FR,fr;q=0.9,bn;q=0.5": "bn",
		"en-GB,ta;q=0.9":          "en",
		"de-DE":                   "en",
		"kn;q=0.2,te;q=0.7":       "te",
		"":                        "en",
		"mr;q=0":                  "en",
	} {
		if got := FromAcceptLanguage(h); got != want {
			t.Errorf("Accept-Language %q: %s, want %s", h, got, want)
		}
	}
	r = httptest.NewRequest("GET", "/?lang=xx", nil)
	if l, _ := Detect(r); l != "en" {
		t.Fatalf("unknown lang: %s", l)
	}
}

func TestTFallsBackToEnglish(t *testing.T) {
	if T("ta", "no.such.key") != "no.such.key" {
		t.Fatal("unknown key should come back as itself")
	}
	if T("xx", "nav.about") != "About" {
		t.Fatal("unknown language falls back to English")
	}
	if got := T("en", "home.preview_count", 12); got != "12 cells with evidence" {
		t.Fatal(got)
	}
}
