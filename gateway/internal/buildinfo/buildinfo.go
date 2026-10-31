// Package buildinfo says which source commit this gateway was built from,
// for the About page.
//
// The deployed image carries /opt/neermattam/BUILD_INFO, written by
// deploy/hf-space/push.py when deploy/modal-web/app.py stages the build
// context (the context has no .git, so Go cannot stamp the binary itself).
// The order is:
//
//  1. Commit, when set at link time: -ldflags "-X neermattam/gateway/internal/buildinfo.Commit=<sha>".
//  2. The file named by BUILD_INFO_PATH (default /opt/neermattam/BUILD_INFO).
//  3. The VCS stamp Go writes into a binary built inside a git checkout.
//
// Otherwise the commit is unknown and the page says so.
package buildinfo

import (
	"bufio"
	"os"
	"regexp"
	"runtime/debug"
	"strings"
	"time"
)

// Commit can be set at link time.
var Commit string

// DefaultPath is where the deployed image keeps BUILD_INFO.
const DefaultPath = "/opt/neermattam/BUILD_INFO"

// Repository is the public source repository.
const Repository = "https://github.com/vignesh2027/neermattam"

// Info is what is known about the build.
type Info struct {
	Commit  string // full hex commit, or ""
	Subject string // its subject line, when known
	// Dirty says the build included uncommitted changes to the services.
	Dirty bool
	// StagedAt is when the deploy context was staged, when known.
	StagedAt time.Time
	// Source says where the commit came from: ldflags, BUILD_INFO or vcs.
	Source string
}

// Short is the first 7 characters of the commit.
func (i Info) Short() string {
	if len(i.Commit) > 7 {
		return i.Commit[:7]
	}
	return i.Commit
}

// URL links the commit on the repository, or the repository itself.
func (i Info) URL() string {
	if i.Commit == "" {
		return Repository
	}
	return Repository + "/commit/" + i.Commit
}

var hexCommit = regexp.MustCompile(`^[0-9a-f]{7,40}$`)

// Read finds the build information. path is the BUILD_INFO file to read;
// empty means DefaultPath.
func Read(path string) Info {
	if c := strings.ToLower(strings.TrimSpace(Commit)); hexCommit.MatchString(c) {
		return Info{Commit: c, Source: "ldflags"}
	}
	if path == "" {
		path = DefaultPath
	}
	if f, err := os.Open(path); err == nil {
		defer f.Close()
		if info, ok := parse(bufio.NewScanner(f)); ok {
			return info
		}
	}
	if bi, ok := debug.ReadBuildInfo(); ok {
		var info Info
		for _, s := range bi.Settings {
			switch s.Key {
			case "vcs.revision":
				info.Commit = strings.ToLower(s.Value)
			case "vcs.modified":
				info.Dirty = s.Value == "true"
			case "vcs.time":
				info.StagedAt, _ = time.Parse(time.RFC3339, s.Value)
			}
		}
		if hexCommit.MatchString(info.Commit) {
			info.Source = "vcs"
			return info
		}
	}
	return Info{}
}

// parse reads the lines push.py writes:
//
//	source commit: <sha>
//	commit subject: <text>
//	services (engine, gateway, perception): clean at this commit | UNCOMMITTED CHANGES INCLUDED
//	deploy files: ...
//	staged at: <RFC 3339>
func parse(sc *bufio.Scanner) (Info, bool) {
	info := Info{Source: "BUILD_INFO"}
	for sc.Scan() {
		k, v, ok := strings.Cut(sc.Text(), ":")
		if !ok {
			continue
		}
		v = strings.TrimSpace(v)
		switch strings.TrimSpace(k) {
		case "source commit":
			info.Commit = strings.ToLower(v)
		case "commit subject":
			info.Subject = v
		case "services (engine, gateway, perception)":
			info.Dirty = strings.Contains(strings.ToUpper(v), "UNCOMMITTED")
		case "staged at":
			info.StagedAt, _ = time.Parse(time.RFC3339, v)
		}
	}
	return info, hexCommit.MatchString(info.Commit)
}
