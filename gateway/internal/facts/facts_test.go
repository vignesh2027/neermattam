package facts

import (
	"os"
	"reflect"
	"testing"
)

const root = "../../.."

func TestEmbeddedFactsMatchTheDocs(t *testing.T) {
	if _, err := os.Stat(root + "/docs/ENGINE.md"); err != nil {
		t.Skip("docs are not in this build context (the deploy image copies gateway/ only)")
	}
	got, err := Extract(root)
	if err != nil {
		t.Fatal(err)
	}
	if want := Embedded(); !reflect.DeepEqual(got, want) {
		t.Fatalf("facts.json is stale; run `go generate ./internal/facts`.\ndocs say:     %+v\nembedded has: %+v", got, want)
	}
}

func TestEmbeddedFactsAreComplete(t *testing.T) {
	f := Embedded()
	if f.Routing.DryMs == "" || f.Depth.MAECm == "" || f.Real.Photos == 0 || len(f.Trace.Steps) < 3 {
		t.Fatalf("incomplete facts: %+v", f)
	}
	if f.Real.Geometry+f.Real.Estimates > f.Real.Photos {
		t.Fatalf("counts do not add up: %+v", f.Real)
	}
}
