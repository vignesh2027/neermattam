package web

import (
	"encoding/json"
	"os"
	"strings"
	"testing"
	"time"

	"neermattam/gateway/internal/i18n"
	"neermattam/gateway/internal/upstream"
)

func TestShowcaseIsTheRecordedPersonRun(t *testing.T) {
	s, err := LoadShowcase()
	if err != nil {
		t.Fatal(err)
	}
	// The c1 run at the end of eval/RESULTS_REAL.md: measured from a person,
	// 45.6 cm, sigma 13.0.
	if s.Result.Method != upstream.MethodPersonReference || s.Result.DepthCm != 45.6 || s.Result.SigmaCm != 13.0 {
		t.Fatalf("result %+v", s.Result)
	}
	if s.Photo.Author == "" || !strings.HasPrefix(s.Photo.SourceURL, "https://") || s.Photo.Licence == "" {
		t.Fatalf("photo credit %+v", s.Photo)
	}
	for _, p := range []string{s.Images.Photo, s.Images.Measured, s.Images.Person} {
		if _, err := os.Stat("static/" + p); err != nil {
			t.Errorf("image %s: %v", p, err)
		}
	}
	thumbs := 0
	for _, st := range s.Steps {
		if st.Thumb != "" {
			thumbs++
			if st.Step != "verify" {
				t.Errorf("thumbnail on a %s step", st.Step)
			}
			if _, err := os.Stat("static/" + st.Thumb); err != nil {
				t.Errorf("thumbnail %s: %v", st.Thumb, err)
			}
		}
	}
	if thumbs == 0 {
		t.Fatal("no verifier images")
	}
	for _, l := range i18n.Languages {
		v := NewShowcaseView(l.Code, s)
		if v.Depth != "46" || v.Sigma != "13" || v.Method != "person" || v.Total != len(s.Steps) {
			t.Fatalf("%s: view %+v", l.Code, v)
		}
		if len(v.Verdicts) != v.Checks || v.Checks != 5 || v.Accepts != 1 || v.Refines != 3 || v.GiveUps != 1 {
			t.Fatalf("%s: verdicts %d checks %d (%d accepted, %d refined, %d given up)", l.Code, len(v.Verdicts), v.Checks, v.Accepts, v.Refines, v.GiveUps)
		}
		// The timeline shows the scene reading, every check and the
		// measurement used, and the checks carry the model's own words.
		if len(v.Key) != 8 || v.Key[0].Kind != "scene" {
			t.Fatalf("%s: %d key steps", l.Code, len(v.Key))
		}
		if v.Key[1].Words == "" || !strings.Contains(v.Key[1].Words, "Magenta") {
			t.Fatalf("%s: model words %q", l.Code, v.Key[1].Words)
		}
	}
}

func TestModelWords(t *testing.T) {
	cases := map[string]string{
		"is_water=true, coverage good; Magenta tint covers the brown floodwater surface.": "Magenta tint covers the brown floodwater surface.",
		"mask 87835 px (6% of photo)":                               "",
		"chose A, shape 0.45; edge visible 0.35, waterline visible": "",
	}
	for in, want := range cases {
		if got := modelWords(in); got != want {
			t.Errorf("modelWords(%q) = %q, want %q", in, got, want)
		}
	}
}

func TestDepthFigure(t *testing.T) {
	f := NewDepthFigure("en", 45.6, 13, false)
	if f.Max != 180 || f.Depth != "46" || f.Sigma != "13" || f.Estimate {
		t.Fatalf("figure %+v", f)
	}
	under := map[string]bool{}
	for _, m := range f.Marks {
		under[m.Label] = m.Under
	}
	// 45.6 cm is just over an adult's knee (45 cm) and under mid-thigh.
	if !under["Ankle"] || !under["Knee"] || under["Mid-thigh"] || under["Waist"] {
		t.Fatalf("marks %+v", f.Marks)
	}
	if !strings.Contains(f.Aria, "46 cm, plus or minus 13 cm") {
		t.Fatalf("aria %q", f.Aria)
	}
	// Deep water widens the ruler; a model estimate is drawn as one.
	deep := NewDepthFigure("ta", 160, 30, true)
	if deep.Max != 250 || !deep.Estimate || !deep.Deeper {
		t.Fatalf("deep figure %+v", deep)
	}
	if deep.Marks[0].Label == "Ankle" {
		t.Fatal("marks not translated")
	}
}

const indiaJSON = `{"generated_at":"2026-11-01T10:00:00Z","cities":[
 {"id":"chennai","name":"Chennai","lat":13.05,"lon":80.24,"rain_mm_1h":1.2,"rain_mm_24h":34.0,"rain_at":"2026-11-01T09:00:00Z","rain_source":"Open-Meteo","real_reports_24h":0,"cells_with_evidence":0,"deepest_cm":null,"measured_photos":3,"critical_sites":862},
 {"id":"mumbai","name":"Mumbai","rain_mm_24h":null,"real_reports_24h":null,"deepest_cm":41.4,"critical_sites":null}],
 "measured_photos":[
 {"id":"a","city":"chennai","place":"Nandanam","photo_date":"2015-12-02","depth_cm":45.6,"sigma_cm":13.0,"method":"person_reference","thumb_url":"/static/x.webp","verify_url":"/verify/m_1","credit":"A, CC BY 2.0","source_url":"https://example.org/a"},
 {"id":"b","city":"chennai","place":"Bad","depth_cm":10,"sigma_cm":2,"method":"wheel_geometry","thumb_url":"https://evil.example/x.png","verify_url":"javascript:alert(1)"},
 {"id":"c","city":"chennai","place":"Odd","depth_cm":10,"sigma_cm":2,"method":"magic","thumb_url":"/static/y.webp","verify_url":"//evil","source_url":"http://plain"}]}`

func TestIndiaViewShowsUnknownAsUnknown(t *testing.T) {
	var d India
	if err := json.Unmarshal([]byte(indiaJSON), &d); err != nil {
		t.Fatal(err)
	}
	refs := []IndiaCityRef{{ID: "chennai", Routing: "ready", Simulated: true, Cells: 1325}, {ID: "mumbai", Routing: "loading"}, {ID: "delhi"}}
	now, _ := time.Parse(time.RFC3339, "2026-11-01T10:00:30Z")
	v := NewIndiaView("en", refs, "mumbai", &d, now)
	if !v.Live || v.RainSrc != "Open-Meteo" || len(v.Cities) != 3 {
		t.Fatalf("view %+v", v)
	}
	c, m, dl := v.Cities[0], v.Cities[1], v.Cities[2]
	// A real zero is a zero; a null is unknown, never zero.
	if c.Rain24 != "34 mm" || c.Reports != "0" || c.Deepest != "" || c.Sites != "862" || !c.Simulated {
		t.Fatalf("chennai %+v", c)
	}
	if m.Rain24 != "" || m.Reports != "" || m.Sites != "" || m.Deepest != "41 cm" || !m.Current {
		t.Fatalf("mumbai %+v", m)
	}
	if dl.Rain24 != "" || dl.Reports != "" {
		t.Fatalf("a city missing from the answer got numbers: %+v", dl)
	}
	// Only same-site images and certificate links are used.
	if len(v.Photos) != 2 || v.Photos[0].Verify != "/verify/m_1" || v.Photos[0].Method != "person" || v.Photos[0].Date != "2 Dec 2015" {
		t.Fatalf("photos %+v", v.Photos)
	}
	if p := v.Photos[1]; p.Verify != "" || p.Source != "" || p.Method != "estimate" {
		t.Fatalf("unsafe or unknown fields kept: %+v", p)
	}

	off := NewIndiaView("en", refs, "chennai", nil, now)
	if off.Live || len(off.Cities) != 3 || off.Cities[0].Cells != "1325" || off.Cities[0].Rain24 != "" {
		t.Fatalf("offline view %+v", off)
	}
}

func TestUnknownSitesFoldIntoTheTable(t *testing.T) {
	list := &upstream.SiteList{ThresholdCm: 15}
	for i, st := range []string{"unknown", "under", "unknown", "over", "nearby"} {
		list.Sites = append(list.Sites, upstream.Site{ID: string(rune('a' + i)), Name: "S" + st, Kind: "data_centre", Status: st})
	}
	v := NewSitesView(SitesOptions{Lang: "en", City: "chennai", Kind: "data_centre"}, list, nil, nil, nil)
	var order []string
	for _, c := range v.Cards {
		order = append(order, c.Status)
	}
	if strings.Join(order, ",") != "over,nearby,under" || len(v.Unknown) != 2 {
		t.Fatalf("cards %v, unknown %d", order, len(v.Unknown))
	}
}

func TestSampleCards(t *testing.T) {
	cards := SampleCards("en")
	if len(cards) < 3 {
		t.Fatalf("%d sample cards", len(cards))
	}
	for _, c := range cards {
		if !strings.HasPrefix(c.Href, "/report?sample="+c.ID) || c.Credit == "" || c.Kind == "" || strings.HasPrefix(c.Kind, "lp.") {
			t.Fatalf("card %+v", c)
		}
	}
}
