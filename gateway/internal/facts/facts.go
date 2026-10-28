// Package facts holds the few numbers the landing page shows, each read
// from the repository's own documents, never typed in by hand:
//
//   - routing time from docs/ENGINE.md (the criterion benchmark table),
//   - synthetic depth accuracy from docs/GEOMETRY.md (the synth-eval block),
//   - real-photo status from eval/RESULTS_REAL.md (the real-eval block),
//   - an agent trace excerpt from a recorded run, eval/out/x2.json.
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

// Real is the status of the real-photo evaluation.
type Real struct {
	Photos    int    `json:"photos"`
	Geometry  int    `json:"wheel_geometry"`
	Estimates int    `json:"model_estimate"`
	Date      string `json:"date"`
	Source    string `json:"source"`
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
	Trace   Trace   `json:"trace"`
}

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
	rb, err := block(string(real), "real-eval")
	if err != nil {
		return f, fmt.Errorf("RESULTS_REAL.md: %w", err)
	}
	for _, r := range realResult.FindAllStringSubmatch(rb, -1) {
		f.Real.Photos++
		switch r[1] {
		case "wheel_geometry":
			f.Real.Geometry++
		case "model_estimate":
			f.Real.Estimates++
		}
	}
	if f.Real.Photos == 0 {
		return f, errors.New("RESULTS_REAL.md: no results found")
	}
	if dm := realDate.FindStringSubmatch(rb); dm != nil {
		f.Real.Date = dm[1]
	}
	f.Real.Source = "eval/RESULTS_REAL.md"

	f.Trace, err = extractTrace(root)
	f.Trace.Date = f.Real.Date
	return f, err
}

// traceSteps picks a short, representative excerpt of the recorded run:
// the scene reading, the water mask and its refinement and acceptance, a
// wheel being refined and given up, and the final estimate.
func extractTrace(root string) (Trace, error) {
	raw, err := os.ReadFile(filepath.Join(root, "eval", "out", "x2.json"))
	if err != nil {
		return Trace{}, err
	}
	var rec struct {
		AgentTrace []upstream.AgentStep `json:"agent_trace"`
	}
	if err := json.Unmarshal(raw, &rec); err != nil {
		return Trace{}, err
	}
	t := Trace{
		Photo:  "India - Chennai - Monsoon - 02",
		Credit: "McKay Savage, CC BY 2.0, via Wikimedia Commons",
		Total:  len(rec.AgentTrace),
		Source: "eval/out/x2.json",
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
	// The run date is recorded in RESULTS_REAL.md, next to the photos.
	return t, nil
}

// Int parses a numeric fact for templates that compare.
func Int(s string) int {
	n, _ := strconv.Atoi(strings.TrimSuffix(s, "%"))
	return n
}
