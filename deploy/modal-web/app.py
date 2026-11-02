"""Neermattam's public site on Modal: engine, perception and gateway in one CPU container.

The image is the same recipe as the Hugging Face Space: the part of
deploy/hf-space/Dockerfile above its "Space only" line, built from the same
staged sources (deploy/hf-space/push.py). In the container the Go launcher
(deploy/hf-space/launcher) runs the three services, the daily perception cap
and a front proxy on port 8000 that Modal routes the public URL to.

Deploy from the repository root:

    modal secret create neermattam-web --from-dotenv .env --force
    modal deploy deploy/modal-web/app.py

See docs/DEPLOY.md.
"""

import importlib.util
import json
import os
import signal
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import modal

APP_NAME = "neermattam-web"
# In the container this file is /root/app.py; the repository paths below are
# only used on the machine that runs `modal deploy`.
SPACE = Path(__file__).resolve().parent.parent / "hf-space"
MARKER = "# ---- Hugging Face Space only below this line ----"

# Read when the app is deployed, so a redeploy can change them.
CPU = float(os.environ.get("NEERMATTAM_WEB_CPU", "8"))
MEMORY_MB = int(os.environ.get("NEERMATTAM_WEB_MEMORY_MB", "5120"))
MEMORY_LIMIT_MB = int(os.environ.get("NEERMATTAM_WEB_MEMORY_LIMIT_MB", "8192"))
# How many X-Forwarded-For entries, counted from the right, were added by
# Modal's proxy. Checked against real requests; see docs/DEPLOY.md.
PROXY_HOPS = os.environ.get("NEERMATTAM_WEB_PROXY_HOPS", "1")
DAILY_CAP = os.environ.get("NEERMATTAM_WEB_DAILY_CAP", "150")
# The site's own address: Open Graph tags, VAPID contact and WhatsApp replies use it.
PUBLIC_URL = os.environ.get(
    "NEERMATTAM_WEB_PUBLIC_URL",
    "https://applemacbook6sep2004--neermattam-web-web-serve.us-east.modal.run",
)

FRONT_PORT = 8000
BUDGET_FILE = "/tmp/neermattam/budget.json"
BUDGET_KEY = "perception_budget"
LAUNCHER = "/opt/neermattam/bin/neermattam-launcher"
# Durable state: the gateway's data directory (real-report provenance, alerts,
# API keys and their usage, the VAPID pair when not given) and the engine's
# cell snapshot and history.
DATA = "/data"
GATEWAY_DATA_DIR = f"{DATA}/gateway"
ENGINE_SNAPSHOT_PATH = f"{DATA}/engine/cells.json"
COMMIT_EVERY_S = 30


def stage_context() -> Path:
    """Lay out the build context as push.py does, with the shared Dockerfile."""
    spec = importlib.util.spec_from_file_location("neermattam_space_push", SPACE / "push.py")
    assert spec is not None and spec.loader is not None
    push = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(push)
    osm = push.find_osm_dir()
    if osm is None:
        raise SystemExit("no folder holds all seven city road extracts (see docs/DEPLOY.md)")
    ctx = Path(tempfile.mkdtemp(prefix="neermattam-modal-"))
    push.stage(ctx, osm)
    full = (ctx / "Dockerfile").read_text()
    if MARKER not in full:
        raise SystemExit("deploy/hf-space/Dockerfile has lost its Space-only marker")
    (ctx / "Dockerfile").write_text(full.split(MARKER)[0])
    return ctx


if modal.is_local():
    _ctx = stage_context()
    image = modal.Image.from_dockerfile(_ctx / "Dockerfile", context_dir=_ctx)
else:
    # Inside the container the image already exists; the path is never read.
    image = modal.Image.from_dockerfile("/nonexistent/Dockerfile")

app = modal.App(APP_NAME)
state = modal.Dict.from_name("neermattam-web-state", create_if_missing=True)
volume = modal.Volume.from_name("neermattam-web-data", create_if_missing=True)


def _read_budget() -> dict | None:
    try:
        return json.loads(Path(BUDGET_FILE).read_text())
    except (OSError, ValueError):
        return None


@app.cls(
    image=image,
    cpu=CPU,
    memory=(MEMORY_MB, MEMORY_LIMIT_MB),
    min_containers=1,
    max_containers=1,
    timeout=24 * 3600,
    secrets=[modal.Secret.from_name("neermattam-web")],
    volumes={DATA: volume},
)
@modal.concurrent(max_inputs=400)
class Web:
    @modal.enter()
    def start(self) -> None:
        self.started = time.monotonic()
        # Restore today's perception count, so a restart does not reset the cap.
        saved = state.get(BUDGET_KEY)
        Path(BUDGET_FILE).parent.mkdir(parents=True, exist_ok=True)
        if isinstance(saved, dict):
            Path(BUDGET_FILE).write_text(json.dumps(saved))
            print(f"[modal] restored perception count {saved}", flush=True)
        env = {
            **os.environ,
            "FRONT_ADDR": f"0.0.0.0:{FRONT_PORT}",
            "PORT": "7860",
            "TRUSTED_PROXY_HOPS": PROXY_HOPS,
            "LOG_FORWARDING_HEADERS": "20",
            "BUDGET_FILE": BUDGET_FILE,
            "PERCEPTION_DAILY_CAP": os.environ.get("PERCEPTION_DAILY_CAP_OVERRIDE", DAILY_CAP),
            "MEMORY_LOG_INTERVAL": "2m",
            "GATEWAY_DATA_DIR": GATEWAY_DATA_DIR,
            "ENGINE_SNAPSHOT_PATH": ENGINE_SNAPSHOT_PATH,
        }
        Path(GATEWAY_DATA_DIR).mkdir(parents=True, exist_ok=True)
        Path(ENGINE_SNAPSHOT_PATH).parent.mkdir(parents=True, exist_ok=True)
        if PUBLIC_URL:
            env["PUBLIC_URL"] = PUBLIC_URL
        self.proc = subprocess.Popen([LAUNCHER], env=env)
        self.stopping = threading.Event()
        self.synced = saved if isinstance(saved, dict) else None
        threading.Thread(target=self._sync_budget, daemon=True).start()

    def _push_budget(self) -> None:
        cur = _read_budget()
        if cur is None or cur == self.synced:
            return
        # Never lower a count another container wrote for the same day.
        prev = state.get(BUDGET_KEY)
        if isinstance(prev, dict) and prev.get("day") == cur.get("day"):
            cur = {**cur, "used": max(int(prev.get("used", 0)), int(cur.get("used", 0)))}
        state.put(BUDGET_KEY, cur)
        self.synced = cur

    def _sync_budget(self) -> None:
        last_commit = time.monotonic()
        while not self.stopping.wait(5):
            try:
                self._push_budget()
            except Exception as exc:  # keep syncing; a missed write is retried
                print(f"[modal] budget sync failed: {type(exc).__name__}", flush=True)
            if time.monotonic() - last_commit >= COMMIT_EVERY_S:
                last_commit = time.monotonic()
                self._commit()

    def _commit(self) -> None:
        """Persist the data directory; the services write it with atomic renames."""
        try:
            volume.commit()
        except Exception as exc:
            print(f"[modal] volume commit failed: {type(exc).__name__}", flush=True)

    @modal.web_server(FRONT_PORT, startup_timeout=60)
    def serve(self) -> None:
        pass

    @modal.exit()
    def stop(self) -> None:
        self.stopping.set()
        self.proc.send_signal(signal.SIGTERM)
        try:
            self.proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        try:
            self._push_budget()
        except Exception as exc:
            print(f"[modal] final budget sync failed: {type(exc).__name__}", flush=True)
        # The services wrote their final state on SIGTERM; keep it.
        self._commit()
