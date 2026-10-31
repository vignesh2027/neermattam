package buildinfo

import (
	"os"
	"path/filepath"
	"testing"
)

func TestReadsTheDeployedBuildInfoFile(t *testing.T) {
	path := filepath.Join(t.TempDir(), "BUILD_INFO")
	text := "source commit: 2a872fc0d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6\n" +
		"commit subject: Merge branch 'sites' into fixes-gw\n" +
		"services (engine, gateway, perception): UNCOMMITTED CHANGES INCLUDED\n" +
		"deploy files: clean at this commit\n" +
		"staged at: 2026-10-30T16:40:00+00:00\n"
	if err := os.WriteFile(path, []byte(text), 0o644); err != nil {
		t.Fatal(err)
	}
	info := Read(path)
	if info.Commit != "2a872fc0d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6" || info.Short() != "2a872fc" || !info.Dirty || info.Source != "BUILD_INFO" {
		t.Fatalf("%+v", info)
	}
	if info.Subject != "Merge branch 'sites' into fixes-gw" || info.StagedAt.IsZero() {
		t.Fatalf("%+v", info)
	}
	if info.URL() != Repository+"/commit/2a872fc0d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6" {
		t.Fatal(info.URL())
	}
}

func TestLinkTimeCommitWinsAndJunkIsIgnored(t *testing.T) {
	old := Commit
	defer func() { Commit = old }()
	Commit = "ABCDEF1234567"
	if info := Read("/nonexistent"); info.Commit != "abcdef1234567" || info.Source != "ldflags" {
		t.Fatalf("%+v", info)
	}
	Commit = "not a commit"
	path := filepath.Join(t.TempDir(), "BUILD_INFO")
	os.WriteFile(path, []byte("source commit: <none>\n"), 0o644)
	info := Read(path)
	if info.Source == "BUILD_INFO" || info.Source == "ldflags" {
		t.Fatalf("a junk commit was accepted: %+v", info)
	}
	if (Info{}).URL() != Repository {
		t.Fatal("an unknown commit should link the repository")
	}
}
