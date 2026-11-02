// Package facts holds the few numbers the landing page shows, each read
// from the repository's own documents, never typed in by hand:
//
//   - routing time from docs/ENGINE.md (the criterion benchmark table),
//   - synthetic depth accuracy from docs/GEOMETRY.md (the synth-eval block),
//   - real-photo status from eval/RESULTS_REAL.md, counted by method
//     (wheel, person or model estimate): the first run (the real-eval
//     block) and the photos chosen for the wheel method (the wheel-eval
//     block),
//   - an agent trace excerpt from a recorded run, kept in git as
//     gateway/internal/facts/testdata/x2.json (a copy of eval/out/x2.json,
//     which is not in git).
//
// `go generate ./internal/facts` (or `go run ./cmd/factgen`) extracts them
// into facts.json, which is embedded, because the deployed image carries no
// docs. When the docs are present at run time the gateway reads them
// afresh instead. A test fails if facts.json no longer matches the docs.
package facts

//go:generate go run ../../cmd/factgen -root ../../.. -out facts.json

import (
	_ "embed"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"

	"neermattam/gateway/internal/upstream"
)

// Routing is one row of the engine's routing benchmark.
type Routing struct {
	Trip           string `json:"trip"`
	DryMs          string `json:"dry_ms"`
	Readings200Ms  string `json:"readings_200_ms"`
	Readings2000Ms string `json:"readings_2000_ms"`
	Machine        string `json:"machine"`
	Source         string `json:"source"`
}

// Depth is the pipeline-default row of the synthetic evaluation.
type Depth struct {
	Cases    string `json:"cases"`
	Usable   string `json:"usable"`
	MAECm    string `json:"mae_cm"`
	MedianCm string `json:"median_cm"`
	Within1  string `json:"within_1_sigma"`
	Source   string `json:"source"`
}

// Real is the status of the real-photo evaluation: how many photos were
// measured from a wheel, from a person, or got a model estimate. A method
// this gateway does not know counts as an estimate.
type Real struct {
	Photos    int    `json:"photos"`
	Geometry  int    `json:"wheel_geometry"`
	Person    int    `json:"person_reference"`
	Estimates int    `json:"model_estimate"`
	Date      string `json:"date"`
	Source    string `json:"source"`
}

// Measured is the number of photos measured against a wheel or a person.
func (r Real) Measured() int { return r.Geometry + r.Person }

// Plus adds another run's counts; the date is the later run's.
func (r Real) Plus(o Real) Real {
	out := Real{Photos: r.Photos + o.Photos, Geometry: r.Geometry + o.Geometry, Person: r.Person + o.Person, Estimates: r.Estimates + o.Estimates, Date: r.Date, Source: r.Source}
	if o.Date > out.Date {
		out.Date = o.Date
	}
	return out
}

// Trace is a recorded agent run.
type Trace struct {
	Photo  string               `json:"photo"`
	Credit string               `json:"credit"`
	Date   string               `json:"date"`
	Steps  []upstream.AgentStep `json:"steps"`
	Total  int                  `json:"total_steps"`
	Source string               `json:"source"`
}

// Facts is everything the landing page quotes.
type Facts struct {
	Routing Routing `json:"routing"`
	Depth   Depth   `json:"depth"`
	Real    Real    `json:"real"`
	// RealWheel is the later run on photos chosen for the wheel method.
	// Its wheel readings include outlines that missed the tyre, which
	// eval/RESULTS_REAL.md explains; only one followed the tyre.
	RealWheel Real  `json:"real_wheel"`
	Trace     Trace `json:"trace"`
}

// RealAll is every real photo run so far: both runs together.
func (f Facts) RealAll() Real { return f.Real.Plus(f.RealWheel) }

//go:embed facts.json
var embedded []byte

// Embedded returns the facts extracted at build time.
func Embedded() Facts {
	var f Facts
	_ = json.Unmarshal(embedded, &f)
	return f
}

// Load reads the facts from a repository checkout at root, falling back to
// the embedded copy when the documents are not there.
func Load(root string) (Facts, bool) {
	if root != "" {
		if f, err := Extract(root); err == nil {
			return f, true
		}
	}
	return Embedded(), false
}

var (
	routingRow = regexp.MustCompile(`(?m)^\| (Velachery station to Guindy, [0-9.]+ km) \| ([0-9.]+ ms) \| ([0-9.]+ ms) \| ([0-9.]+ ms) \|`)
	machineRe  = regexp.MustCompile(`Measured on an? ([^,(]+?)(?: \(|,)`)
	depthRow   = regexp.MustCompile(`(?m)^\| tyre \+ rim, affine, bias-corrected \(pipeline default\) \| ([0-9]+) \| ([0-9]+%) \| ([0-9.]+) \| ([0-9.]+) \|`)
	within1Re  = regexp.MustCompile(`(?m)^\| share with abs error within 1 sigma[^|]*\| ([0-9]+%) \|`)
	realResult = regexp.MustCompile(`(?m)^Result: \*\*([a-z_]+)\*\*`)
	realDate   = regexp.MustCompile(`real_photos\.py` + "`" + ` on ([0-9]{4}-[0-9]{2}-[0-9]{2})`)
)

func block(text, name string) (string, error) {
	start := strings.Index(text, "<!-- "+name+":begin -->")
	end := strings.Index(text, "<!-- "+name+":end -->")
	if start < 0 {
		return "", fmt.Errorf("no %s block", name)
	}
	if end < 0 {
		end = len(text)
	}
	return text[start:end], nil
}

// Extract reads every fact from the documents under root (the repository).
func Extract(root string) (Facts, error) {
	var f Facts
	engine, err := os.ReadFile(filepath.Join(root, "docs", "ENGINE.md"))
	if err != nil {
		return f, err
	}
	m := routingRow.FindStringSubmatch(string(engine))
	if m == nil {
		return f, errors.New("ENGINE.md: routing benchmark row not found")
	}
	f.Routing = Routing{Trip: m[1], DryMs: m[2], Readings200Ms: m[3], Readings2000Ms: m[4], Source: "docs/ENGINE.md"}
	if mm := machineRe.FindStringSubmatch(string(engine)); mm != nil {
		f.Routing.Machine = strings.TrimSpace(mm[1])
	}

	geo, err := os.ReadFile(filepath.Join(root, "docs", "GEOMETRY.md"))
	if err != nil {
		return f, err
	}
	b, err := block(string(geo), "synth-eval")
	if err != nil {
		return f, fmt.Errorf("GEOMETRY.md: %w", err)
	}
	d := depthRow.FindStringSubmatch(b)
	w := within1Re.FindStringSubmatch(b)
	if d == nil || w == nil {
		return f, errors.New("GEOMETRY.md: synthetic accuracy rows not found")
	}
	f.Depth = Depth{Cases: d[1], Usable: d[2], MAECm: d[3], MedianCm: d[4], Within1: w[1], Source: "docs/GEOMETRY.md"}

	real, err := os.ReadFile(filepath.Join(root, "eval", "RESULTS_REAL.md"))
	if err != nil {
		return f, err
	}
	if f.Real, err = countReal(string(real), "real-eval"); err != nil {
		return f, err
	}
	if f.RealWheel, err = countReal(string(real), "wheel-eval"); err != nil {
		return f, err
	}

	f.Trace, err = extractTrace(root)
	return f, err
}

// countReal counts one generated block of eval/RESULTS_REAL.md by the
// method that answered each photo.
func countReal(text, name string) (Real, error) {
	r := Real{Source: "eval/RESULTS_REAL.md"}
	b, err := block(text, name)
	if err != nil {
		return r, fmt.Errorf("RESULTS_REAL.md: %w", err)
	}
	for _, m := range realResult.FindAllStringSubmatch(b, -1) {
		r.Photos++
		switch upstream.NormalizeMethod(m[1]) {
		case upstream.MethodWheelGeometry:
			r.Geometry++
		case upstream.MethodPersonReference:
			r.Person++
		case upstream.MethodModelEstimate:
			r.Estimates++
		}
	}
	if r.Photos == 0 {
		return r, fmt.Errorf("RESULTS_REAL.md: no results in the %s block", name)
	}
	if dm := realDate.FindStringSubmatch(b); dm != nil {
		r.Date = dm[1]
	}
	return r, nil
}

// TracePath is the recorded run, relative to the repository root.
const TracePath = "gateway/internal/facts/testdata/x2.json"

// traceSteps picks a short, representative excerpt of the recorded run:
// the scene reading, the water mask and its refinement and acceptance, a
// wheel being refined and given up, and the final estimate.
func extractTrace(root string) (Trace, error) {
	raw, err := os.ReadFile(filepath.Join(root, filepath.FromSlash(TracePath)))
	if err != nil {
		return Trace{}, err
	}
	var rec struct {
		RecordedOn string               `json:"recorded_on"`
		AgentTrace []upstream.AgentStep `json:"agent_trace"`
	}
	if err := json.Unmarshal(raw, &rec); err != nil {
		return Trace{}, err
	}
	t := Trace{
		Photo:  "India - Chennai - Monsoon - 02",
		Credit: "McKay Savage, CC BY 2.0, via Wikimedia Commons",
		Date:   rec.RecordedOn,
		Total:  len(rec.AgentTrace),
		Source: TracePath,
	}
	pick := func(match func(upstream.AgentStep) bool) {
		for _, s := range rec.AgentTrace {
			if match(s) {
				for _, have := range t.Steps {
					if have == s {
						return
					}
				}
				t.Steps = append(t.Steps, s)
				return
			}
		}
	}
	pick(func(s upstream.AgentStep) bool { return s.Step == "scene" })
	pick(func(s upstream.AgentStep) bool { return s.Step == "segment" && s.Target == "water" })
	pick(func(s upstream.AgentStep) bool { return s.Target == "water" && strings.HasPrefix(s.Decision, "refine") })
	pick(func(s upstream.AgentStep) bool { return s.Target == "water" && s.Decision == "accept" })
	pick(func(s upstream.AgentStep) bool { return s.Target != "water" && strings.HasPrefix(s.Decision, "refine") })
	pick(func(s upstream.AgentStep) bool { return strings.HasPrefix(s.Decision, "give up") })
	pick(func(s upstream.AgentStep) bool { return s.Step == "estimate" || s.Step == "geometry" })
	if len(t.Steps) < 3 {
		return t, errors.New("x2.json: too few steps for an excerpt")
	}
	return t, nil
}

// Int parses a numeric fact for templates that compare.
func Int(s string) int {
	n, _ := strconv.Atoi(strings.TrimSuffix(s, "%"))
	return n
}
