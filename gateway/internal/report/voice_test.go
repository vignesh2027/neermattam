package report

import "testing"

func TestParseDepthWords(t *testing.T) {
	cases := []struct {
		in    string
		depth float64
		ok    bool
	}{
		{"knee deep", 45, true},
		{"water up to my ankle", 10, true},
		{"muzhangal alavu thanni", 45, true},
		{"about 30 cm", 30, true},
		{"two feet", 0, false},
		{"2 feet of water", 61, true},
		{"10 inches", 25.4, true},
		{"waist", 95, true},
		{"", 0, false},
		{"it is raining", 0, false},
	}
	for _, c := range cases {
		got, ok := ParseDepthWords(c.in)
		if ok != c.ok || (ok && (got.DepthCm < c.depth-0.5 || got.DepthCm > c.depth+0.5)) {
			t.Errorf("%q: got %+v %v", c.in, got, ok)
		}
		if ok && got.SigmaCm < 5 {
			t.Errorf("%q: spoken depth sigma %v is too tight", c.in, got.SigmaCm)
		}
	}
}
