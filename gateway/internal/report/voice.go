package report

import (
	"math"
	"regexp"
	"strconv"
	"strings"

	"neermattam/gateway/internal/upstream"
)

// VoiceEstimate is a depth read from words such as "knee deep".
type VoiceEstimate struct {
	Words   string  `json:"words"`
	DepthCm float64 `json:"depth_cm"`
	SigmaCm float64 `json:"depth_sigma_cm"`
	Basis   string  `json:"basis"`
}

// Body landmarks for an adult, as rough heights above the ground. These are
// assumptions for turning speech into a number, with deliberately wide
// uncertainty; a voice report is fused with reduced trust on top of that.
var landmarks = []struct {
	words   []string
	depth   float64
	sigma   float64
	landmrk string
}{
	{[]string{"ankle", "kanukkal"}, 10, 5, "ankle height"},
	{[]string{"shin", "calf"}, 25, 8, "mid-shin height"},
	{[]string{"knee", "muzhangal", "muttu"}, 45, 8, "knee height"},
	{[]string{"thigh", "thodai"}, 65, 10, "thigh height"},
	{[]string{"waist", "hip", "idupu", "idup"}, 95, 12, "waist height"},
	{[]string{"chest", "nenju"}, 125, 15, "chest height"},
	{[]string{"neck", "shoulder", "kazhuthu"}, 140, 15, "neck height"},
}

var explicitDepth = regexp.MustCompile(`(\d+(?:\.\d+)?)\s*(cm|centimet\w*|inch\w*|in\b|feet|foot|ft)`)

// ParseDepthWords turns spoken depth words into a depth with uncertainty.
func ParseDepthWords(s string) (VoiceEstimate, bool) {
	t := strings.ToLower(strings.TrimSpace(s))
	if t == "" {
		return VoiceEstimate{}, false
	}
	if m := explicitDepth.FindStringSubmatch(t); m != nil {
		v, err := strconv.ParseFloat(m[1], 64)
		if err == nil && v > 0 {
			unit := m[2]
			cm := v
			switch {
			case strings.HasPrefix(unit, "in"):
				cm = v * 2.54
			case unit == "feet" || unit == "foot" || unit == "ft":
				cm = v * 30.48
			}
			if cm <= 300 {
				return VoiceEstimate{Words: s, DepthCm: round1(cm), SigmaCm: round1(max(5, 0.3*cm)), Basis: "a spoken number, given 30 percent uncertainty"}, true
			}
		}
	}
	for _, l := range landmarks {
		for _, w := range l.words {
			if strings.Contains(t, w) {
				return VoiceEstimate{Words: s, DepthCm: l.depth, SigmaCm: l.sigma, Basis: "an assumed adult " + l.landmrk}, true
			}
		}
	}
	return VoiceEstimate{}, false
}

// FromTranscript takes the voice model's own depth when it gave one (with
// the contract's floor on sigma: at least 10 cm or 30 percent), and
// otherwise reads the depth words, then the whole transcript.
func FromTranscript(t *upstream.Transcript) (VoiceEstimate, bool) {
	if t.DepthCm != nil && *t.DepthCm >= 0 {
		sigma := math.Max(10, 0.3**t.DepthCm)
		if t.DepthSigmaCm != nil {
			sigma = math.Max(sigma, *t.DepthSigmaCm)
		}
		words := t.DepthWords
		if words == "" {
			words = t.Text
		}
		basis := "the voice model's reading of the spoken words"
		if t.Model != "" {
			basis += " (" + t.Model + ")"
		}
		return VoiceEstimate{Words: words, DepthCm: round1(*t.DepthCm), SigmaCm: round1(sigma), Basis: basis}, true
	}
	if est, ok := ParseDepthWords(t.DepthWords); ok {
		return est, true
	}
	return ParseDepthWords(t.Text)
}

func round1(x float64) float64 {
	return float64(int(x*10+0.5)) / 10
}
