// Command factgen extracts the landing page's numbers from the repository's
// documents into internal/facts/facts.json. Run it after the docs change:
//
//	cd gateway && go generate ./internal/facts
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"os"

	"neermattam/gateway/internal/facts"
)

func main() {
	root := flag.String("root", "..", "repository root")
	out := flag.String("out", "internal/facts/facts.json", "output file")
	flag.Parse()
	f, err := facts.Extract(*root)
	if err != nil {
		fmt.Fprintln(os.Stderr, "factgen:", err)
		os.Exit(1)
	}
	b, _ := json.MarshalIndent(f, "", "  ")
	if err := os.WriteFile(*out, append(b, '\n'), 0o644); err != nil {
		fmt.Fprintln(os.Stderr, "factgen:", err)
		os.Exit(1)
	}
	fmt.Println("wrote", *out)
}
