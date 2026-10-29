"""Synthetic accuracy evaluation of the wheel-depth geometry.

Run:  uv run python -m neermattam_perception.synth_eval [--cases 500] [--write-docs PATH]

Each case renders a 3D wheel through a pinhole camera (see ``synth.py``) and
measures depth from the rendered masks. Masks come from the renderer, not from
SAM, so this isolates the geometry: the ellipse fit, the waterline, the depth
construction and the perspective error. Three depth methods are compared on the
same fits:

* ``affine``: the weak-perspective construction, no camera knowledge (the
  pipeline default, and the method behind the breakdown and the sigma check);
* ``perspective, EXIF focal``: exact correction with the true focal length,
  as when the photo carries ``FocalLengthIn35mmFilm``;
* ``perspective, assumed 26 mm``: the same correction with a typical phone main
  camera focal length assumed, as when EXIF is stripped;
* ``tyre outline only``: no rim mask, to show what the rim adds.

The true diameter is used so depth errors come from geometry alone. Diameter
uncertainty adds ``fraction * sigma_D`` on top, independently.
"""

from __future__ import annotations

import argparse
import itertools
import math
import os
import re
import sys
import time
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from numpy.typing import NDArray

from .geometry import (
    MODEL_BIAS_TABLE,
    MODEL_SIGMA_TABLE,
    DepthMeasurement,
    GeometryConfig,
    PinholeCamera,
    depth_from_line,
    measure_wheel_depth,
)
from .synth import SYNTH_DIAMETERS_CM, SynthParams, _camera, distance_m, random_params, render

DEFAULT_FOCAL_35MM = 26.0
FloatArray = NDArray[np.float64]
METHODS = ("affine", "persp_exif", "persp_26mm", "no_rim")
METHOD_LABEL = {
    "affine": "tyre + rim, affine, bias-corrected (pipeline default)",
    "persp_exif": "tyre + rim, perspective with EXIF focal, uncorrected",
    "persp_26mm": "tyre + rim, perspective with assumed 26 mm, uncorrected",
    "no_rim": "tyre outline only, affine, uncorrected",
}


@dataclass(frozen=True, slots=True)
class CaseResult:
    index: int
    params: SynthParams
    true_cm: float
    axis_ratio: float
    distance_m: float
    reason: str | None
    est_cm: dict[str, float]
    sigma_cm: float
    seconds: float
    diag: dict[str, float]


def run_case(args: tuple[int, int, int] | tuple[int, int, int, bool]) -> CaseResult:
    index, seed, draws = args[0], args[1], args[2]
    raw = len(args) > 3 and bool(args[3])
    rng = np.random.default_rng([seed, index])
    diameters = list(SYNTH_DIAMETERS_CM)
    p = random_params(rng, diameters)
    case = render(p, rng)
    cam = _camera(p, p.diameter_cm / 200.0)
    exif = PinholeCamera(cam.f, cam.cx, cam.cy)
    assumed = PinholeCamera.from_35mm(DEFAULT_FOCAL_35MM, p.width, p.height)
    t0 = time.perf_counter()
    m = measure_wheel_depth(
        case.wheel_mask,
        case.water_mask,
        p.diameter_cm,
        0.0,
        GeometryConfig(
            mc_draws=draws,
            seed=index,
            model_sigma_table=() if raw else MODEL_SIGMA_TABLE,
            model_bias_table=() if raw else MODEL_BIAS_TABLE,
        ),
        rim_mask=case.rim_mask,
    )
    secs = time.perf_counter() - t0
    if raw:
        m_plain = m
    else:
        m_plain = measure_wheel_depth(
            case.wheel_mask,
            case.water_mask,
            p.diameter_cm,
            0.0,
            GeometryConfig(mc_draws=0, seed=index, model_bias_table=()),
        )
    no_rim = m_plain.depth_cm if isinstance(m_plain, DepthMeasurement) else math.nan
    if not isinstance(m, DepthMeasurement):
        return CaseResult(
            index,
            p,
            case.depth_cm,
            case.axis_ratio,
            distance_m(p),
            m.reason.value,
            {},
            math.nan,
            secs,
            {},
        )
    line = m.waterline.line
    est = {
        "affine": m.depth_cm,
        "persp_exif": depth_from_line(m, line, exif, apply_bias=False),
        "persp_26mm": depth_from_line(m, line, assumed, apply_bias=False),
        "no_rim": no_rim,
    }
    diag = {
        "arc_covered": m.arc_covered_deg,
        "arc_span": m.arc_span_deg,
        "fraction": m.fraction,
        "tread_rel": m.outline.tread_px / max(m.ellipse.b, 1e-9),
        "fit_ratio": m.ellipse.axis_ratio,
        "fraction_sigma": m.fraction_sigma,
        "n_alt": float(len(m.outline.alternatives)),
    }
    return CaseResult(
        index,
        p,
        case.depth_cm,
        case.axis_ratio,
        distance_m(p),
        None,
        est,
        m.depth_sigma_cm,
        secs,
        diag,
    )


# --------------------------------------------------------------------------- report


def _stats(err: np.ndarray) -> tuple[float, float, float, float]:
    a = np.abs(err)
    return float(a.mean()), float(np.median(a)), float(np.percentile(a, 90)), float(err.mean())


def _row(label: str, rs: list[CaseResult], method: str) -> str:
    ok = [r for r in rs if r.reason is None and math.isfinite(r.est_cm.get(method, math.nan))]
    if not rs:
        return f"| {label} | 0 | | | | | | |"
    if method == "no_rim":  # its own decline decisions are not tracked separately
        rs = [r for r in rs if r.reason is None]
    usable = 100.0 * len(ok) / len(rs)
    if not ok:
        return f"| {label} | {len(rs)} | {usable:.0f}% | | | | | |"
    err = np.array([r.est_cm[method] - r.true_cm for r in ok])
    rel = np.array([abs(r.est_cm[method] - r.true_cm) / r.params.diameter_cm for r in ok])
    mae, med, p90, bias = _stats(err)
    return (
        f"| {label} | {len(rs)} | {usable:.0f}% | {mae:.2f} | {med:.2f} | {p90:.2f} | "
        f"{bias:+.2f} | {100 * rel.mean():.1f}% |"
    )


HEADER = (
    "| Subset | Cases | Usable | MAE cm | Median cm | P90 cm | Bias cm | MAE / diameter |\n"
    "|---|---|---|---|---|---|---|---|"
)


def report(results: list[CaseResult], seed: int, draws: int, wall: float) -> str:
    rs = sorted(results, key=lambda r: r.index)
    ok = [r for r in rs if r.reason is None]
    lines: list[str] = []
    lines.append(
        f"Generated by `uv run python -m neermattam_perception.synth_eval --cases {len(rs)} "
        f"--seed {seed} --draws {draws}` ({draws} resampling draws per case, propagated by "
        f"batched Gauss-Newton refits; {wall:.0f} s wall time)."
    )
    lines.append("")
    lines.append("**All cases, by depth method** (errors over usable cases only)")
    lines.append("")
    lines.append(HEADER)
    for m in METHODS:
        lines.append(_row(METHOD_LABEL[m], rs, m))
    lines.append("")
    main = "affine"
    lines.append(f"**Breakdown for {METHOD_LABEL[main]}**")
    lines.append("")
    lines.append(HEADER)

    def sub(label: str, pred: Callable[[CaseResult], bool]) -> None:
        lines.append(_row(label, [r for r in rs if pred(r)], main))

    sub("depth 5 to 25% of diameter", lambda r: r.params.depth_fraction < 0.25)
    sub("depth 25 to 50%", lambda r: 0.25 <= r.params.depth_fraction < 0.5)
    sub("depth 50 to 65%", lambda r: 0.5 <= r.params.depth_fraction < 0.65)
    sub("depth 65 to 80%", lambda r: r.params.depth_fraction >= 0.65)
    sub("ellipse ratio below 0.7", lambda r: r.axis_ratio < 0.7)
    sub("ellipse ratio 0.7 to 0.85", lambda r: 0.7 <= r.axis_ratio < 0.85)
    sub("ellipse ratio 0.85 and up", lambda r: r.axis_ratio >= 0.85)
    sub("camera 1.5 to 3 m away", lambda r: r.distance_m < 3.0)
    sub("camera 3 to 6 m away", lambda r: r.distance_m >= 3.0)
    sub("tyre width under 15% of diameter", lambda r: r.params.tyre_width_frac < 0.15)
    sub("tyre width 15 to 30%", lambda r: r.params.tyre_width_frac >= 0.15)
    sub("water visible behind the wheel", lambda r: r.params.water_behind)
    sub("top of wheel hidden by bodywork", lambda r: r.params.top_occlusion is not None)
    lines.append("")

    reasons: dict[str, int] = {}
    for r in rs:
        if r.reason:
            reasons[r.reason] = reasons.get(r.reason, 0) + 1
    lines.append("**Cases declined, by reason**")
    lines.append("")
    lines.append("| Reason | Cases |")
    lines.append("|---|---|")
    for k, v in sorted(reasons.items(), key=lambda kv: -kv[1]):
        lines.append(f"| `{k}` | {v} |")
    if not reasons:
        lines.append("| none | 0 |")
    lines.append("")

    lines.append("**Accuracy against the visible-arc threshold** (main method)")
    lines.append("")
    lines.append("| Minimum visible arc | Usable | MAE cm | Median cm | P90 cm | MAE / diameter |")
    lines.append("|---|---|---|---|---|---|")
    for thr in (100, 140, 180, 220):
        sel = [r for r in ok if r.diag["arc_covered"] >= thr]
        if not sel:
            continue
        e = np.array([r.est_cm[main] - r.true_cm for r in sel])
        rel = np.mean([abs(r.est_cm[main] - r.true_cm) / r.params.diameter_cm for r in sel])
        mae, med, p90, _ = _stats(e)
        lines.append(
            f"| {thr} degrees | {100 * len(sel) / len(rs):.0f}% | {mae:.2f} | {med:.2f} | "
            f"{p90:.2f} | {100 * rel:.1f}% |"
        )
    lines.append("")

    if ok:
        err = np.array([abs(r.est_cm[main] - r.true_cm) for r in ok])
        sig = np.array([r.sigma_cm for r in ok])
        within1 = 100.0 * float(np.mean(err <= sig))
        within2 = 100.0 * float(np.mean(err <= 2 * sig))
        z = err / np.maximum(sig, 1e-9)
        lines.append(
            "**Uncertainty calibration** (depth_sigma_cm: resampled measurement noise plus the "
            "calibrated "
            "model term, on seeds disjoint from the calibration)"
        )
        lines.append("")
        lines.append("| Measure | Value |")
        lines.append("|---|---|")
        lines.append(f"| median depth_sigma_cm | {float(np.median(sig)):.2f} |")
        lines.append(f"| share with abs error within 1 sigma (ideal 68%) | {within1:.0f}% |")
        lines.append(f"| share with abs error within 2 sigma (ideal 95%) | {within2:.0f}% |")
        lines.append(f"| median abs error / sigma | {float(np.median(z)):.2f} |")
        signed = np.array([r.est_cm[main] - r.true_cm for r in ok])
        low = 100.0 * float(np.mean(-signed > 1.5 * sig))
        high = 100.0 * float(np.mean(signed > 1.5 * sig))
        lines.append(f"| share with true depth above depth + 1.5 sigma (ideal 6.7%) | {low:.1f}% |")
        lines.append(
            f"| share with true depth below depth - 1.5 sigma (ideal 6.7%) | {high:.1f}% |"
        )
        t = np.array([r.seconds for r in rs])
        lines.append(f"| median geometry time per case | {1000 * float(np.median(t)):.0f} ms |")
        lines.append("")
    return "\n".join(lines)


CALIBRATION_BINS = (100.0, 130.0, 160.0, 190.0, 220.0, 260.0, 360.0)
# Downstream blocks a road at depth + 1.5 sigma, so the share of readings whose
# true depth lies above that line must not exceed the Gaussian rate.
BLOCK_K = 1.5
BLOCK_MISS = 0.0668
ONE_SIGMA_COVER = 0.68


def _smallest_model_sigma(ok: Callable[[float], bool], hi: float = 1.0) -> float:
    """Smallest model share in [0, hi] for which ``ok`` holds (it is monotone)."""
    if ok(0.0):
        return 0.0
    lo = 0.0
    for _ in range(40):
        mid = (lo + hi) / 2.0
        lo, hi = (lo, mid) if ok(mid) else (mid, hi)
    return hi


def calibrate(cases: int, seed: int, workers: int) -> int:
    """Model-bias and model-error tables, as shares of the diameter, per visible-arc bin.

    Uses the geometry with neither table, on seeds disjoint from the evaluation.
    The errors are one-sided with a heavy low tail: short arcs read low, never
    high. So in each bin:

    * the bias is the median signed error, clamped to be no more than zero, so
      that the correction can only raise a depth;
    * after removing it, the model sigma is the smallest value, combined in
      quadrature with each case's Monte Carlo sigma, for which both hold:
      68 percent of abs errors fall within one sigma, and no more than 6.7
      percent of true depths lie above ``depth + 1.5 sigma`` (the line the
      engine blocks at). The second condition is what the heavy tail costs.

    Both tables are then made monotone in arc (more visible arc never hurts):
    the sigma non-increasing, the bias no smaller in magnitude at a shorter arc.
    """
    jobs = [(i, seed, 30, True) for i in range(cases)]
    with ProcessPoolExecutor(max_workers=workers) as ex:
        results = [r for r in ex.map(run_case, jobs, chunksize=4) if r.reason is None]
    rows: list[tuple[float, float, float, int]] = []
    for lo, hi in itertools.pairwise(CALIBRATION_BINS):
        sel = [r for r in results if lo <= r.diag["arc_covered"] < hi]
        if len(sel) < 5:
            continue
        rel = np.array([(r.est_cm["affine"] - r.true_cm) / r.params.diameter_cm for r in sel])
        mc = np.array([r.sigma_cm / r.params.diameter_cm for r in sel])
        bias = min(0.0, float(np.median(rel)))
        res = rel - bias

        def covers(m: float, res: FloatArray = res, mc: FloatArray = mc) -> bool:
            tot = np.sqrt(mc**2 + m**2)
            one = float(np.mean(np.abs(res) <= tot)) >= ONE_SIGMA_COVER
            miss = float(np.mean(-res > BLOCK_K * tot)) <= BLOCK_MISS
            return one and miss

        rows.append(((lo + hi) / 2.0, bias, _smallest_model_sigma(covers), len(sel)))
    for i in range(len(rows) - 2, -1, -1):  # monotone in arc
        c, b, v, n = rows[i]
        rows[i] = (c, min(b, rows[i + 1][1]), max(v, rows[i + 1][2]), n)
    sys.stdout.write(f"calibration seed {seed}, {len(results)} usable cases\n")
    sys.stdout.write("MODEL_SIGMA_TABLE = (\n")
    for centre, _, model, n in rows:
        sys.stdout.write(f"    ({centre:.0f}.0, {model:.4f}),  # n={n}\n")
    sys.stdout.write(")\nMODEL_BIAS_TABLE = (\n")
    for centre, bias, _, n in rows:
        sys.stdout.write(f"    ({centre:.0f}.0, {bias:.4f}),  # n={n}\n")
    sys.stdout.write(")\n")
    return 0


def write_docs(path: Path, body: str) -> None:
    text = path.read_text(encoding="utf-8")
    begin, end = "<!-- synth-eval:begin -->", "<!-- synth-eval:end -->"
    if begin not in text or end not in text:
        raise SystemExit(f"{path} lacks the {begin} / {end} markers")
    pattern = re.compile(re.escape(begin) + ".*?" + re.escape(end), re.DOTALL)
    path.write_text(pattern.sub(f"{begin}\n{body}\n{end}", text), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--cases", type=int, default=500)
    ap.add_argument("--seed", type=int, default=2026)
    ap.add_argument("--draws", type=int, default=40, help="resampling draws per case")
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    ap.add_argument("--write-docs", type=Path, default=None)
    ap.add_argument(
        "--calibrate",
        action="store_true",
        help="fit the model-error table on seeds disjoint from the evaluation and print it",
    )
    args = ap.parse_args(argv)
    if args.calibrate:
        return calibrate(args.cases, args.seed + 7919, args.workers)
    t0 = time.perf_counter()
    jobs = [(i, args.seed, args.draws) for i in range(args.cases)]
    if args.workers > 1:
        with ProcessPoolExecutor(max_workers=args.workers) as ex:
            results = list(ex.map(run_case, jobs, chunksize=4))
    else:
        results = [run_case(j) for j in jobs]
    body = report(results, args.seed, args.draws, time.perf_counter() - t0)
    sys.stdout.write(body + "\n")
    if args.write_docs is not None:
        write_docs(args.write_docs, body)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
