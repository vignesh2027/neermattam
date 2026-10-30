"""Eval gate: the geometry must not get worse than the committed baseline.

Usage (from perception/):

    uv run python ../eval/gate.py                    # check; exit 1 when worse
    uv run python ../eval/gate.py --write-baseline   # record an intended change

Runs, with fixed seeds, the synthetic wheel evaluation (``synth_eval``: 3D
wheels rendered through a pinhole camera, the pipeline's default affine method
with the bias correction and the model error) and the synthetic check of the
person method (``person_synth``), and compares each with ``eval/baseline.json``:

* mean absolute depth error may not rise by more than ``tolerance.mae_cm``;
* the share of errors within one stated sigma may not fall by more than
  ``tolerance.share``;
* the share of cases measured at all may not fall by more than
  ``tolerance.share`` either (declining hard cases would otherwise flatter the
  error).

Every case is seeded by its index, so a run gives the same numbers whatever the
number of workers; the tolerances only absorb floating-point differences
between machines. The run is smaller than the one published in
docs/GEOMETRY.md (200 scenes with 40 resampling draws instead of 500 with 100),
so it takes about a minute; its numbers are its own and are not quoted as
accuracy anywhere.

The prompt versions are recorded too. The gate cannot rerun the real photos
(they call Muse Spark Open), so when a prompt changed since the baseline it
says so: rerun ``real_photos.py`` and compare the acceptance before writing a
new baseline.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any

import numpy as np

from neermattam_perception.muse_open import PROMPT_VERSIONS
from neermattam_perception.person_synth import evaluate as person_evaluate
from neermattam_perception.synth_eval import CaseResult, run_case

HERE = Path(__file__).resolve().parent
BASELINE = HERE / "baseline.json"

SETTINGS: dict[str, Any] = {
    "synthetic": {"cases": 200, "seed": 2026, "draws": 40},
    "person": {"n": 400, "seed": 11},
}
TOLERANCE = {"mae_cm": 0.02, "share": 0.005}


def synthetic(cases: int, seed: int, draws: int, workers: int) -> dict[str, float]:
    jobs = [(i, seed, draws) for i in range(cases)]
    if workers > 1:
        with ProcessPoolExecutor(max_workers=workers) as ex:
            results: list[CaseResult] = list(ex.map(run_case, jobs, chunksize=4))
    else:
        results = [run_case(j) for j in jobs]
    ok = [r for r in results if r.reason is None]
    err = np.array([abs(r.est_cm["affine"] - r.true_cm) for r in ok])
    sig = np.array([r.sigma_cm for r in ok])
    return {
        "cases": float(cases),
        "measured_share": len(ok) / cases,
        "mae_cm": float(err.mean()),
        "within_1_sigma": float(np.mean(err <= sig)),
    }


def person(n: int, seed: int) -> dict[str, float]:
    r = person_evaluate(n, seed)
    return {
        "cases": r["cases"],
        "measured_share": r["measured"] / r["cases"],
        "mae_cm": r["mae_cm"],
        "within_1_sigma": r["within_1_sigma"],
    }


def run(workers: int) -> dict[str, Any]:
    s, p = SETTINGS["synthetic"], SETTINGS["person"]
    return {
        "synthetic": synthetic(s["cases"], s["seed"], s["draws"], workers),
        "person": person(p["n"], p["seed"]),
    }


def compare(now: dict[str, Any], base: dict[str, Any]) -> list[tuple[str, str, float, float, bool]]:
    """Rows of (eval, metric, baseline, now, ok)."""
    tol = base.get("tolerance", TOLERANCE)
    rows = []
    for ev in ("synthetic", "person"):
        b, n = base["results"][ev], now[ev]
        mae_ok = n["mae_cm"] <= b["mae_cm"] + tol["mae_cm"]
        rows.append((ev, "mae_cm", b["mae_cm"], n["mae_cm"], mae_ok))
        for m in ("within_1_sigma", "measured_share"):
            rows.append((ev, m, b[m], n[m], n[m] >= b[m] - tol["share"]))
    return rows


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--write-baseline", action="store_true", help="record these numbers")
    ap.add_argument("--baseline", type=Path, default=BASELINE)
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    args = ap.parse_args(argv)
    out = sys.stdout
    t0 = time.perf_counter()
    now = run(args.workers)
    wall = time.perf_counter() - t0
    if args.write_baseline:
        doc = {
            "generated_by": "uv run python ../eval/gate.py --write-baseline (from perception/)",
            "date": time.strftime("%Y-%m-%d"),
            "settings": SETTINGS,
            "tolerance": TOLERANCE,
            "prompt_versions": PROMPT_VERSIONS,
            "environment": {"python": platform.python_version(), "numpy": np.__version__},
            "results": now,
        }
        args.baseline.write_text(json.dumps(doc, indent=1) + "\n", encoding="utf-8")
        out.write(f"wrote {args.baseline} ({wall:.0f} s)\n")
        return 0
    base = json.loads(args.baseline.read_text(encoding="utf-8"))
    if base.get("settings") != SETTINGS:
        out.write(
            f"the baseline was made with other settings ({base.get('settings')}); "
            "write a new one with --write-baseline\n"
        )
        return 2
    rows = compare(now, base)
    out.write(f"eval gate against {args.baseline.name} ({base.get('date')}), {wall:.0f} s\n")
    out.write(f"{'eval':<10} {'metric':<15} {'baseline':>9} {'now':>9}  verdict\n")
    for ev, metric, b, n, ok in rows:
        out.write(f"{ev:<10} {metric:<15} {b:>9.4f} {n:>9.4f}  {'ok' if ok else 'WORSE'}\n")
    changed = sorted(
        name
        for name, v in PROMPT_VERSIONS.items()
        if (base.get("prompt_versions") or {}).get(name) != v
    )
    if changed:
        out.write(
            f"note: prompt versions changed since the baseline ({', '.join(changed)}); the "
            "synthetic evals do not exercise prompts, so rerun real_photos.py and compare the "
            "acceptance before writing a new baseline\n"
        )
    failed = [r for r in rows if not r[4]]
    out.write("result: " + ("pass" if not failed else f"FAIL ({len(failed)} worse)") + "\n")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
