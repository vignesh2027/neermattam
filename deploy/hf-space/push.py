"""Assemble the Neermattam Space and push it to Hugging Face.

Run from anywhere:

    uv run --with huggingface_hub python deploy/hf-space/push.py --hf-env PATH

It copies the engine, gateway and perception sources from the working tree,
the launcher and Space files from deploy/hf-space/, and the Chennai roads
extract, into a staging folder laid out as the Space's Dockerfile expects. It
then creates the private Space if needed, sets BASETEN_API_KEY as a Space
secret and the Baseten base URL and model as Space variables (read from the
repository .env), and uploads the folder as one commit. Files no longer in
the staging folder are deleted from the Space.

The Hugging Face token comes from the HF_TOKEN environment variable or from
the HF_TOKEN line of the file given with --hf-env. No secret is ever printed.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
NAMESPACE = "vigneshwar234"
CITIES = ("chennai", "mumbai", "bengaluru", "hyderabad", "kolkata", "delhi")
# Folders searched for <city>-roads.osm.pbf, first match wins.
DEFAULT_OSM = [ROOT / "data/osm", Path.home() / "neermattam-data/osm"]


def find_osm_dir(given: Path | None = None) -> Path | None:
    """The first folder holding every city's roads extract."""
    for d in [given] if given else DEFAULT_OSM:
        if d and all((d / f"{c}-roads.osm.pbf").is_file() for c in CITIES):
            return d
    return None


def read_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
            v = v[1:-1]
        out[k.strip().removeprefix("export ").strip()] = v
    return out


def tracked_files(sub: str) -> list[str]:
    """Files under sub that git tracks or would track (untracked but not
    ignored), read from the working tree, so uncommitted work is deployed."""
    res = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "--", sub],
        cwd=ROOT, check=True, capture_output=True, text=True,
    )
    return [f for f in res.stdout.splitlines() if (ROOT / f).is_file()]


def stage(dest: Path, osm: Path) -> None:
    for sub, keep in (
        ("engine", ("Cargo.toml", "Cargo.lock", "src/", "benches/", "tests/")),
        ("gateway", ("",)),
        ("perception", ("pyproject.toml", "uv.lock", "README.md", "src/")),
    ):
        for f in tracked_files(sub):
            rel = f[len(sub) + 1:]
            if not any(rel == k or (k.endswith("/") and rel.startswith(k)) or k == "" for k in keep):
                continue
            if "__pycache__" in rel or rel.endswith(".pyc"):
                continue
            target = dest / f
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / f, target)
    launcher = dest / "launcher"
    launcher.mkdir()
    for f in sorted((HERE / "launcher").iterdir()):
        if f.suffix == ".go" or f.name == "go.mod":
            shutil.copy2(f, launcher / f.name)
    for name in ("Dockerfile", "README.md", "DATA_LICENSE"):
        shutil.copy2(HERE / name, dest / name)
    (dest / "data/osm").mkdir(parents=True)
    for city in CITIES:
        shutil.copy2(osm / f"{city}-roads.osm.pbf", dest / "data/osm" / f"{city}-roads.osm.pbf")
    (dest / ".gitattributes").write_text("*.pbf filter=lfs diff=lfs merge=lfs -text\n")

    git = lambda *a: subprocess.run(["git", *a], cwd=ROOT, capture_output=True, text=True).stdout.strip()  # noqa: E731
    head = git("rev-parse", "HEAD")
    dirty = git("status", "--porcelain", "--", "engine", "gateway", "perception")
    deploy_dirty = git("status", "--porcelain", "--", "deploy")
    (dest / "BUILD_INFO").write_text(
        f"source commit: {head}\n"
        f"commit subject: {git('log', '-1', '--format=%s')}\n"
        f"services (engine, gateway, perception): {'UNCOMMITTED CHANGES INCLUDED' if dirty else 'clean at this commit'}\n"
        f"deploy files: {'uncommitted changes included' if deploy_dirty else 'clean at this commit'}\n"
        f"staged at: {__import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat(timespec='seconds')}\n"
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", default=f"{NAMESPACE}/neermattam")
    ap.add_argument("--hf-env", type=Path, help="file with an HF_TOKEN= line")
    ap.add_argument("--app-env", type=Path, default=ROOT / ".env",
                    help="file with BASETEN_API_KEY, BASETEN_BASE_URL, BASETEN_MODEL")
    ap.add_argument("--osm", type=Path, help="folder with every <city>-roads.osm.pbf")
    ap.add_argument("--skip-secrets", action="store_true", help="do not touch secrets or variables")
    ap.add_argument("--dry-run", action="store_true", help="stage and list the files only")
    ap.add_argument("--stage-dir", type=Path,
                    help="only stage into this new folder (for a local docker build), do not push")
    ap.add_argument("--message", default="Deploy Neermattam")
    args = ap.parse_args()

    if not args.repo.startswith(NAMESPACE + "/"):
        print(f"refusing: only the {NAMESPACE} namespace may be written", file=sys.stderr)
        return 2
    osm = find_osm_dir(args.osm)
    if osm is None:
        print("no folder holds all six city road extracts; pass --osm", file=sys.stderr)
        return 2

    if args.stage_dir:
        args.stage_dir.mkdir(parents=True, exist_ok=False)
        stage(args.stage_dir, osm)
        print(f"staged into {args.stage_dir}")
        return 0

    with tempfile.TemporaryDirectory(prefix="neermattam-space-") as tmp:
        dest = Path(tmp)
        stage(dest, osm)
        files = sorted(p for p in dest.rglob("*") if p.is_file())
        size = sum(p.stat().st_size for p in files)
        print(f"staged {len(files)} files, {size / 1e6:.1f} MB")
        if args.dry_run:
            for p in files:
                print(f"  {p.relative_to(dest)}  {p.stat().st_size}")
            return 0

        from huggingface_hub import HfApi

        token = os.environ.get("HF_TOKEN") or (read_env_file(args.hf_env).get("HF_TOKEN") if args.hf_env else None)
        if not token:
            print("no Hugging Face token: set HF_TOKEN or pass --hf-env", file=sys.stderr)
            return 2
        api = HfApi(token=token)
        if api.whoami()["name"] != NAMESPACE:
            print(f"refusing: the token does not belong to {NAMESPACE}", file=sys.stderr)
            return 2

        api.create_repo(args.repo, repo_type="space", space_sdk="docker", private=True, exist_ok=True)
        if not api.space_info(args.repo).private:
            print("note: the Space is public; this script never changes visibility")

        if not args.skip_secrets:
            app = read_env_file(args.app_env)
            if not app.get("BASETEN_API_KEY"):
                print(f"BASETEN_API_KEY missing from {args.app_env}", file=sys.stderr)
                return 2
            api.add_space_secret(args.repo, "BASETEN_API_KEY", app["BASETEN_API_KEY"],
                                 description="Baseten key for Muse Spark Open")
            print("secret BASETEN_API_KEY set")
            for name in ("BASETEN_BASE_URL", "BASETEN_MODEL"):
                if app.get(name):
                    api.add_space_variable(args.repo, name, app[name])
                    print(f"variable {name} set")

        info = api.upload_folder(
            repo_id=args.repo, repo_type="space", folder_path=str(dest),
            commit_message=args.message, delete_patterns=["*"],
        )
        print(f"uploaded: {info}")
        print(f"Space: https://huggingface.co/spaces/{args.repo}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
