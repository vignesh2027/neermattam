// Command neermattam-keys manages developer API keys in the gateway's data
// directory. The gateway picks up changes within seconds.
//
//	neermattam-keys create -name "Acme Fleet" -rate 600
//	neermattam-keys list
//	neermattam-keys usage [id]
//	neermattam-keys disable <id>
//	neermattam-keys enable <id>
//	neermattam-keys delete <id>
//
// The data directory is -dir, else GATEWAY_DATA_DIR, else ../data/gateway
// (the gateway's default when run from gateway/). A new key is printed once;
// only its SHA-256 is stored.
package main

import (
	"errors"
	"flag"
	"fmt"
	"os"
	"strings"
	"text/tabwriter"
	"time"

	"neermattam/gateway/internal/apikeys"
)

func main() {
	if err := run(os.Args[1:]); err != nil {
		fmt.Fprintln(os.Stderr, "neermattam-keys:", err)
		os.Exit(1)
	}
}

func defaultDir() string {
	if d := strings.TrimSpace(os.Getenv("GATEWAY_DATA_DIR")); d != "" {
		return d
	}
	return "../data/gateway"
}

const usage = `usage: neermattam-keys [-dir DIR] <command> [arguments]

commands:
  create -name NAME [-rate N]   make a key (N requests per minute, default 120)
  list                          list keys (never the secrets)
  usage [ID]                    requests per key, or per day for one key
  disable ID | enable ID        stop or restore a key
  delete ID                     remove a key for good`

func run(args []string) error {
	fs := flag.NewFlagSet("neermattam-keys", flag.ContinueOnError)
	dir := fs.String("dir", defaultDir(), "gateway data directory")
	fs.Usage = func() { fmt.Fprintln(os.Stderr, usage) }
	if err := fs.Parse(args); err != nil {
		return err
	}
	rest := fs.Args()
	if len(rest) == 0 {
		fs.Usage()
		return errors.New("no command")
	}
	cmd, rest := rest[0], rest[1:]
	switch cmd {
	case "create":
		return create(*dir, rest)
	case "list":
		return list(*dir)
	case "usage":
		return showUsage(*dir, rest)
	case "disable", "enable", "delete":
		if len(rest) != 1 {
			return fmt.Errorf("%s needs a key id", cmd)
		}
		return change(*dir, cmd, rest[0])
	}
	fs.Usage()
	return fmt.Errorf("unknown command %q", cmd)
}

func create(dir string, args []string) error {
	fs := flag.NewFlagSet("create", flag.ContinueOnError)
	name := fs.String("name", "", "who the key is for")
	rate := fs.Int("rate", 120, "requests per minute")
	if err := fs.Parse(args); err != nil {
		return err
	}
	if strings.TrimSpace(*name) == "" {
		return errors.New("create needs -name")
	}
	if *rate < 1 || *rate > 100000 {
		return errors.New("-rate must be between 1 and 100000")
	}
	keys, err := apikeys.ReadKeys(dir)
	if err != nil {
		return err
	}
	secret, k, err := apikeys.New(strings.TrimSpace(*name), *rate, time.Now())
	if err != nil {
		return err
	}
	for _, o := range keys {
		if o.ID == k.ID {
			return errors.New("id collision; run create again")
		}
	}
	if err := apikeys.WriteKeys(dir, append(keys, k)); err != nil {
		return err
	}
	fmt.Printf("Created key %s for %s, %d requests per minute.\n\n  %s\n\nIt is shown only now; Neermattam keeps only its SHA-256.\n", k.ID, k.Name, k.RatePerMin, secret)
	return nil
}

func list(dir string) error {
	keys, err := apikeys.ReadKeys(dir)
	if err != nil {
		return err
	}
	if len(keys) == 0 {
		fmt.Println("No keys yet. Make one with: neermattam-keys create -name NAME")
		return nil
	}
	tw := tabwriter.NewWriter(os.Stdout, 0, 0, 2, ' ', 0)
	fmt.Fprintln(tw, "ID\tNAME\tRATE/MIN\tCREATED\tSTATE")
	for _, k := range keys {
		state := "active"
		if k.Disabled {
			state = "disabled"
		}
		fmt.Fprintf(tw, "%s\t%s\t%d\t%s\t%s\n", k.ID, k.Name, k.RatePerMin, k.CreatedAt.Format("2006-01-02"), state)
	}
	return tw.Flush()
}

func showUsage(dir string, args []string) error {
	keys, err := apikeys.ReadKeys(dir)
	if err != nil {
		return err
	}
	u, err := apikeys.ReadUsage(dir)
	if err != nil {
		return err
	}
	tw := tabwriter.NewWriter(os.Stdout, 0, 0, 2, ' ', 0)
	if len(args) == 1 {
		fmt.Fprintln(tw, "DATE (UTC)\tROUTES\tCELLS")
		for _, d := range apikeys.Days(u[args[0]]) {
			fmt.Fprintf(tw, "%s\t%d\t%d\n", d.Date, d.Routes, d.Cells)
		}
		return tw.Flush()
	}
	fmt.Fprintln(tw, "ID\tNAME\tROUTES\tCELLS\tLAST USED")
	for _, k := range keys {
		r := u[k.ID]
		if r == nil {
			r = &apikeys.Usage{}
		}
		last := "never"
		if !r.LastUsed.IsZero() {
			last = r.LastUsed.Format(time.RFC3339)
		}
		fmt.Fprintf(tw, "%s\t%s\t%d\t%d\t%s\n", k.ID, k.Name, r.Total.Routes, r.Total.Cells, last)
	}
	fmt.Fprintln(tw, "\nUsage is saved by the gateway every 30 seconds and at shutdown.")
	return tw.Flush()
}

func change(dir, cmd, id string) error {
	keys, err := apikeys.ReadKeys(dir)
	if err != nil {
		return err
	}
	out := keys[:0]
	found := false
	for _, k := range keys {
		if k.ID != id {
			out = append(out, k)
			continue
		}
		found = true
		switch cmd {
		case "disable":
			k.Disabled = true
			out = append(out, k)
		case "enable":
			k.Disabled = false
			out = append(out, k)
		}
	}
	if !found {
		return fmt.Errorf("no key with id %s", id)
	}
	if err := apikeys.WriteKeys(dir, out); err != nil {
		return err
	}
	fmt.Printf("Key %s: %s.\n", id, map[string]string{"disable": "disabled", "enable": "enabled", "delete": "deleted"}[cmd])
	return nil
}
