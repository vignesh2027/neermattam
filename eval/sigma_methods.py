"""Batched Gauss-Newton sigma against the full Monte Carlo, and their run time.

From perception/:

    uv run python ../eval/sigma_methods.py --seed 9945 --cases 160 --workers 7   # calibration
    uv run python ../eval/sigma_methods.py --seed 9945 --cases 40 --workers 1    # timing

Each synthetic case (``synth.py``) is measured twice with the same masks and
seed: ``uncertainty="monte_carlo"`` (every draw refitted by the nonlinear
solver) and ``uncertainty="linear"`` (all draws refitted together by batched
Gauss-Newton steps). Neither the model-error table nor the bias table is used
and the batched sigma is taken before ``LINEAR_SIGMA_SCALE`` is applied, so the
ratio isolates the propagation of measurement noise. Seed 9945 is the
calibration seed of docs/GEOMETRY.md; the evaluation seeds are 2026 and 4242.
Times are wall time per wheel, so only a run with ``--workers 1`` gives
latencies that mean anything.
"""

from __future__ import annotations

import argparse
import sys
import time
from concurrent.futures import ProcessPoolExecutor

import numpy as np

from neermattam_perception import geometry
from neermattam_perception.geometry import DepthMeasurement, GeometryConfig, measure_wheel_depth
from neermattam_perception.references import CATALOGUE
from neermattam_perception.synth import random_params, render

DIAMETERS = sorted({e.diameter_cm for e in CATALOGUE})


def one(args: tuple[int, int, int]) -> tuple[float, float, float, float] | None:
    seed, i, draws = args
    geometry.LINEAR_SIGMA_SCALE = 1.0  # compare the unscaled propagation
    rng = np.random.default_rng([seed, i])
    p = random_params(rng, DIAMETERS)
    c = render(p, rng)
    out: list[float] = []
    for method in ("monte_carlo", "linear"):
        cfg = GeometryConfig(
            mc_draws=draws, seed=i, uncertainty=method, model_sigma_table=(), model_bias_table=()
        )
        t0 = time.perf_counter()
        m = measure_wheel_depth(
            c.wheel_mask, c.water_mask, p.diameter_cm, 0.0, cfg, rim_mask=c.rim_mask
        )
        dt = time.perf_counter() - t0
        if not isinstance(m, DepthMeasurement):
            return None
        out += [m.depth_sigma_cm, dt]
    return out[0], out[1], out[2], out[3]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--seed", type=int, default=9945)
    ap.add_argument("--cases", type=int, default=160)
    ap.add_argument("--draws", type=int, default=100)
    ap.add_argument("--workers", type=int, default=1)
    a = ap.parse_args(argv)
    jobs = [(a.seed, i, a.draws) for i in range(a.cases)]
    if a.workers > 1:
        with ProcessPoolExecutor(a.workers) as ex:
            rows = [r for r in ex.map(one, jobs) if r]
    else:
        rows = [r for r in map(one, jobs) if r]
    x = np.array(rows)
    ratio = x[:, 2] / x[:, 0]
    rms = float(np.sqrt(np.mean(x[:, 0] ** 2) / np.mean(x[:, 2] ** 2)))
    t_mc, t_lin = 1000 * x[:, 1], 1000 * x[:, 3]
    sys.stdout.write(
        f"seed {a.seed}, {len(x)} usable of {a.cases} cases, {a.draws} draws, {a.workers} workers\n"
        f"batched / Monte Carlo sigma: median {np.median(ratio):.3f}, "
        f"10th percentile {np.percentile(ratio, 10):.3f}, 90th {np.percentile(ratio, 90):.3f}\n"
        f"root-mean-square Monte Carlo sigma over batched sigma: {rms:.3f} "
        "(the service scales the batched sigma by geometry.LINEAR_SIGMA_SCALE)\n"
        f"time per wheel, median: Monte Carlo {np.median(t_mc):.0f} ms, "
        f"batched {np.median(t_lin):.0f} ms; 90th percentile: "
        f"{np.percentile(t_mc, 90):.0f} ms and {np.percentile(t_lin, 90):.0f} ms\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
