package web

import (
	_ "embed"
	"encoding/json"
	"fmt"
	"math"
	"strings"
	"time"

	"neermattam/gateway/internal/i18n"
	"neermattam/gateway/internal/samples"
	"neermattam/gateway/internal/upstream"
)

// The landing page's showcase: one real recorded run, drawn step by step.
// Its data is internal/web/showcase.json, written by
// gateway/brand-src/make_landing_media.py from a run in eval/out/. To show a
// different run, change RUN in that script and run it again.

//go:embed showcase.json
var showcaseJSON []byte

// Showcase is the recorded run as showcase.json holds it.
type Showcase struct {
	ID    string `json:"id"`
	City  string `json:"city"`
	Place string `json:"place"`
	Photo struct {
		Title      string `json:"title"`
		Author     string `json:"author"`
		Licence    string `json:"licence"`
		LicenceURL string `json:"licence_url"`
		SourceURL  string `json:"source_url"`
		Taken      string `json:"taken"`
	} `json:"photo"`
	Run struct {
		Date          string            `json:"date"`
		Doc           string            `json:"doc"`
		MeasurementID string            `json:"measurement_id"`
		WallS         float64           `json:"wall_s"`
		CostUSD       float64           `json:"cost_usd"`
		Models        map[string]string `json:"models"`
	} `json:"run"`
	Result struct {
		Method        string   `json:"method"`
		DepthCm       float64  `json:"depth_cm"`
		SigmaCm       float64  `json:"sigma_cm"`
		HeightCm      *float64 `json:"height_cm"`
		HeightSigmaCm *float64 `json:"height_sigma_cm"`
		EstimateCm    *float64 `json:"estimate_cm"`
		EstimateSigma *float64 `json:"estimate_sigma_cm"`
		EstimateBasis string   `json:"estimate_basis"`
	} `json:"result"`
	Images struct {
		Photo     string     `json:"photo"`
		Measured  string     `json:"measured"`
		Person    string     `json:"person"`
		Width     int        `json:"width"`
		Height    int        `json:"height"`
		PersonBox [4]float64 `json:"person_box"`
	} `json:"images"`
	Steps []ShowcaseStep `json:"steps"`
}

// ShowcaseStep is one agent step with the image the verifier was shown.
type ShowcaseStep struct {
	upstream.AgentStep
	Thumb string `json:"thumb,omitempty"`
}

// LoadShowcase parses the embedded run.
func LoadShowcase() (*Showcase, error) {
	var s Showcase
	if err := json.Unmarshal(showcaseJSON, &s); err != nil {
		return nil, fmt.Errorf("showcase.json: %w", err)
	}
	if len(s.Steps) == 0 || s.Result.DepthCm <= 0 || s.Images.Photo == "" {
		return nil, fmt.Errorf("showcase.json: incomplete")
	}
	return &s, nil
}

// ShowcaseView is the run, ready to draw in one language.
type ShowcaseView struct {
	Photo, Measured, Person string // static paths for the asset helper
	W, H                    int
	// The measured person's box and the waterline, as CSS percentages.
	BoxLeft, BoxTop, BoxWidth, BoxHeight, WaterTop string
	Depth, Sigma                                   string
	MethodLabel                                    string
	Method                                         string // person, geometry or estimate
	Ruler                                          string // what the ruler was, in words
	Estimate, EstimateSigma, EstimateBasis         string
	Place, Taken, RunDate                          string
	Author, Licence, LicenceURL, SourceURL         string
	Title                                          string
	Doc, DocURL                                    string
	Wall, Cost                                     string
	Models                                         []string
	Verdicts                                       []Verdict
	Steps                                          []ShowcaseStepView
	Key                                            []ShowcaseStepView // the steps drawn on the timeline
	Total, Checks, Refines, GiveUps, Accepts       int
	Figure                                         *DepthFigure
}

// Verdict is one of the verifier's decisions, as a chip.
type Verdict struct {
	Target, Decision, Tone string
}

// ShowcaseStepView is a trace step with its thumbnail and raw numbers.
type ShowcaseStepView struct {
	TraceStepView
	Thumb, ThumbAlt string
	RawInput        string
	RawOutput       string
	RawDecision     string
	Words           string // the model's own words, when the step has them
}

// RepoURL is where the documents named on the page live.
const RepoURL = "https://github.com/vignesh2027/neermattam/blob/main/"

// targetKind classifies a step's target for the chips: water, wheel,
// person or other.
func targetKind(t string) string {
	t = strings.ToLower(t)
	switch {
	case strings.Contains(t, "wheel"):
		return "wheel"
	case strings.Contains(t, "water"):
		return "water"
	case strings.Contains(t, "person"), strings.Contains(t, "people"):
		return "person"
	}
	return "other"
}

// modelWords is the part of a verifier's output in the model's own words:
// the text after the last "; ", when it reads as a sentence.
func modelWords(out string) string {
	i := strings.LastIndex(out, "; ")
	if i < 0 {
		return ""
	}
	w := strings.TrimSpace(out[i+2:])
	if strings.Contains(w, "=") || len(strings.Fields(w)) < 6 {
		return "" // a list of check fields, not a sentence
	}
	return w
}

// DateText writes YYYY-MM-DD as "3 Dec 2015" in a language; "" stays "".
func DateText(lang, ymd string) string {
	t, err := time.Parse("2006-01-02", ymd)
	if err != nil {
		return ymd
	}
	return fmt.Sprintf("%d %s %d", t.Day(), i18n.Month(lang, t.Month()), t.Year())
}

func pctf(f float64) string { return fmt.Sprintf("%.2f%%", 100*f) }

// NewShowcaseView renders the run in a language.
func NewShowcaseView(lang string, s *Showcase) *ShowcaseView {
	if s == nil {
		return nil
	}
	tr := func(k string, a ...any) string { return i18n.T(lang, k, a...) }
	b := s.Images.PersonBox
	v := &ShowcaseView{
		Photo: s.Images.Photo, Measured: s.Images.Measured, Person: s.Images.Person,
		W: s.Images.Width, H: s.Images.Height,
		BoxLeft: pctf(b[0]), BoxTop: pctf(b[1]), BoxWidth: pctf(b[2] - b[0]), BoxHeight: pctf(b[3] - b[1]), WaterTop: pctf(b[3]),
		Depth: fmt.Sprintf("%.0f", s.Result.DepthCm), Sigma: fmt.Sprintf("%.0f", s.Result.SigmaCm),
		Place: s.Place + ", " + tr("city."+s.City), Taken: DateText(lang, s.Photo.Taken), RunDate: DateText(lang, s.Run.Date),
		Author: s.Photo.Author, Licence: s.Photo.Licence, LicenceURL: s.Photo.LicenceURL, SourceURL: s.Photo.SourceURL, Title: s.Photo.Title,
		Doc: s.Run.Doc, DocURL: RepoURL + s.Run.Doc,
		Wall: fmt.Sprintf("%.1f s", s.Run.WallS), Cost: fmt.Sprintf("USD %.3f", s.Run.CostUSD),
		Total: len(s.Steps),
	}
	for _, k := range []string{"scene", "masks"} {
		if m := s.Run.Models[k]; m != "" {
			v.Models = append(v.Models, m)
		}
	}
	switch s.Result.Method {
	case upstream.MethodPersonReference:
		v.Method, v.MethodLabel = "person", tr("result.person_chip")
		if s.Result.HeightCm != nil && s.Result.HeightSigmaCm != nil {
			v.Ruler = tr("lp.sc_ruler_person", fmt.Sprintf("%.0f", *s.Result.HeightCm), fmt.Sprintf("%.0f", *s.Result.HeightSigmaCm))
		}
	case upstream.MethodWheelGeometry:
		v.Method, v.MethodLabel = "geometry", tr("result.measured_from", strings.ToLower(tr("ref.wheel")))
		v.Ruler = tr("lp.sc_ruler_wheel")
	default:
		v.Method, v.MethodLabel = "estimate", tr("result.model_estimate")
	}
	if e := s.Result.EstimateCm; e != nil {
		v.Estimate = fmt.Sprintf("%.0f", *e)
		if s.Result.EstimateSigma != nil {
			v.EstimateSigma = fmt.Sprintf("%.0f", *s.Result.EstimateSigma)
		}
		v.EstimateBasis = s.Result.EstimateBasis
	}
	for i, st := range s.Steps {
		tv := NewTraceStep(lang, i, st.AgentStep)
		tv.DelayMs = 0
		sv := ShowcaseStepView{TraceStepView: tv, Thumb: st.Thumb, RawInput: st.Input, RawOutput: st.Output, RawDecision: st.Decision}
		if st.Step == "verify" {
			v.Checks++
			sv.Words = modelWords(st.Output)
			kind := targetKind(st.Target)
			sv.ThumbAlt = tr("lp.thumb_alt_"+kind, i+1)
			v.Verdicts = append(v.Verdicts, Verdict{Target: tr("lp.t_" + kind), Decision: tv.Decision, Tone: tv.Tone})
			switch tv.Tone {
			case "ok":
				v.Accepts++
			case "refine":
				v.Refines++
			case "stop":
				v.GiveUps++
			}
		}
		v.Steps = append(v.Steps, sv)
		// The timeline draws the scene reading, every check, and the
		// measurement that was used.
		if st.Step == "scene" || st.Step == "verify" || st.Step == "estimate" || (st.Step == "geometry" && strings.Contains(st.Output, fmt.Sprintf("%.1f cm", s.Result.DepthCm))) {
			v.Key = append(v.Key, sv)
		}
	}
	est := v.Method == "estimate"
	f := NewDepthFigure(lang, s.Result.DepthCm, s.Result.SigmaCm, est)
	v.Figure = &f
	return v
}

// SampleCard is one sample photo on the page.
type SampleCard struct {
	ID, Thumb, Place, Year, Credit, Kind string
	Href                                 string
}

// SampleCards lists the sample photos in a language.
func SampleCards(lang string) []SampleCard {
	var out []SampleCard
	for _, s := range samples.All() {
		out = append(out, SampleCard{
			ID: s.ID, Thumb: s.Thumb(), Place: s.Place, Year: s.Year(), Kind: i18n.T(lang, "lp.sample_kind_"+s.Kind),
			Credit: s.Author + ", " + s.Licence, Href: "/report?sample=" + s.ID + "#try",
		})
	}
	return out
}

// India is GET /api/india as its contract defines it. Nulls mean unknown.
type India struct {
	GeneratedAt string `json:"generated_at"`
	Cities      []struct {
		ID                string   `json:"id"`
		Name              string   `json:"name"`
		RainMm1h          *float64 `json:"rain_mm_1h"`
		RainMm24h         *float64 `json:"rain_mm_24h"`
		RainAt            *string  `json:"rain_at"`
		RainSource        string   `json:"rain_source"`
		RealReports24h    *int     `json:"real_reports_24h"`
		CellsWithEvidence *int     `json:"cells_with_evidence"`
		DeepestCm         *float64 `json:"deepest_cm"`
		MeasuredPhotos    *int     `json:"measured_photos"`
		CriticalSites     *int     `json:"critical_sites"`
	} `json:"cities"`
	MeasuredPhotos []struct {
		ID        string   `json:"id"`
		City      string   `json:"city"`
		Place     string   `json:"place"`
		PhotoDate string   `json:"photo_date"`
		DepthCm   *float64 `json:"depth_cm"`
		SigmaCm   *float64 `json:"sigma_cm"`
		Method    string   `json:"method"`
		ThumbURL  string   `json:"thumb_url"`
		VerifyURL string   `json:"verify_url"`
		Credit    string   `json:"credit"`
		SourceURL string   `json:"source_url"`
	} `json:"measured_photos"`
}

// IndiaCity is one city tile.
type IndiaCity struct {
	ID, Href                    string
	Rain24, Rain1               string // "34 mm", or "" when unknown
	Reports, Cells, Sites, Shot string // counts, or "" when unknown
	Deepest                     string // "45 cm", or ""
	Simulated, Current          bool
	Routing                     string
}

// IndiaPhoto is one measured photo in the strip.
type IndiaPhoto struct {
	Place, City, Date, Depth, Sigma, Method, MethodLabel string
	Thumb, Verify, Credit, Source                        string
}

// IndiaView is the "Live across India" section.
type IndiaView struct {
	Live      bool   // the live numbers arrived
	Updated   string // when they were made, in words
	RainSrc   string
	Cities    []IndiaCity
	Photos    []IndiaPhoto
	TotalRain string
}

// IndiaCityRef is what the page knows about a city without the live
// numbers.
type IndiaCityRef struct {
	ID, Routing string
	Simulated   bool
	Cells       int // cells with evidence, from the engine's city list
}

func countText(p *int) string {
	if p == nil {
		return ""
	}
	return fmt.Sprintf("%d", *p)
}

func mmText(p *float64) string {
	if p == nil || math.IsNaN(*p) {
		return ""
	}
	if *p < 10 {
		return fmt.Sprintf("%.1f mm", *p)
	}
	return fmt.Sprintf("%.0f mm", *p)
}

// safePath accepts a same-site path under one of the prefixes.
func safePath(p string, prefixes ...string) string {
	if strings.ContainsAny(p, "\"'<> \\") || strings.Contains(p, "..") {
		return ""
	}
	for _, pre := range prefixes {
		if strings.HasPrefix(p, pre) {
			return p
		}
	}
	return ""
}

// NewIndiaView builds the section from the city list and, when it could be
// read, GET /api/india. Without the live numbers the tiles still name every
// city and its routing state, and every number reads "no data".
func NewIndiaView(lang string, refs []IndiaCityRef, current string, data *India, now time.Time) *IndiaView {
	v := &IndiaView{}
	byID := map[string]int{}
	if data != nil {
		v.Live = true
		for i, c := range data.Cities {
			byID[c.ID] = i
			if c.RainSource != "" && v.RainSrc == "" {
				v.RainSrc = c.RainSource
			}
		}
		if t, err := time.Parse(time.RFC3339, data.GeneratedAt); err == nil {
			v.Updated = Ago(lang, t)
			if now.Sub(t) < time.Minute {
				v.Updated = i18n.T(lang, "js.just_now")
			}
		}
	}
	for _, r := range refs {
		c := IndiaCity{ID: r.ID, Href: "/map?city=" + r.ID, Simulated: r.Simulated, Current: r.ID == current, Routing: r.Routing, Cells: fmt.Sprintf("%d", r.Cells)}
		if i, ok := byID[r.ID]; ok {
			d := data.Cities[i]
			c.Rain24, c.Rain1 = mmText(d.RainMm24h), mmText(d.RainMm1h)
			c.Reports, c.Cells, c.Sites, c.Shot = countText(d.RealReports24h), countText(d.CellsWithEvidence), countText(d.CriticalSites), countText(d.MeasuredPhotos)
			if d.DeepestCm != nil {
				c.Deepest = fmt.Sprintf("%.0f cm", *d.DeepestCm)
			}
		}
		v.Cities = append(v.Cities, c)
	}
	if data != nil {
		for _, p := range data.MeasuredPhotos {
			thumb := safePath(p.ThumbURL, "/static/", "/api/")
			if thumb == "" || p.DepthCm == nil || p.SigmaCm == nil {
				continue
			}
			ph := IndiaPhoto{
				Place: p.Place, City: p.City, Date: DateText(lang, p.PhotoDate),
				Depth: fmt.Sprintf("%.0f", *p.DepthCm), Sigma: fmt.Sprintf("%.0f", *p.SigmaCm),
				Thumb: thumb, Verify: safePath(p.VerifyURL, "/verify/"), Credit: p.Credit,
			}
			if strings.HasPrefix(p.SourceURL, "https://") {
				ph.Source = p.SourceURL
			}
			switch p.Method {
			case upstream.MethodPersonReference:
				ph.Method, ph.MethodLabel = "person", i18n.T(lang, "result.person_chip")
			case upstream.MethodWheelGeometry:
				ph.Method, ph.MethodLabel = "geometry", i18n.T(lang, "result.measured_from", strings.ToLower(i18n.T(lang, "ref.wheel")))
			default:
				ph.Method, ph.MethodLabel = "estimate", i18n.T(lang, "result.model_estimate")
			}
			v.Photos = append(v.Photos, ph)
			if len(v.Photos) == 6 {
				break
			}
		}
	}
	return v
}
