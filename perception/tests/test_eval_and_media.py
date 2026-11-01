from __future__ import annotations

import os
import tempfile
from pathlib import Path

import cv2
import numpy as np
import pytest

from neermattam_perception import media, synth_eval
from neermattam_perception.geometry import DepthMeasurement, GeometryConfig, measure_wheel_depth
from neermattam_perception.synth import SYNTH_DIAMETERS_CM, random_params, render


def test_video_key_frame_is_the_sharpest() -> None:
    fd, path = tempfile.mkstemp(suffix=".mp4")
    os.close(fd)
    try:
        out = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*"mp4v"), 10.0, (160, 120))
        rng = np.random.default_rng(0)
        sharp = (rng.random((120, 160, 3)) * 255).astype(np.uint8)
        for i in range(12):
            frame = sharp if i == 7 else cv2.GaussianBlur(sharp, (0, 0), 6)
            out.write(frame)
        out.release()
        data = Path(path).read_bytes()
    finally:
        os.unlink(path)
    v = media.load_video(data)
    assert v.fps == pytest.approx(10.0) and v.n_frames == 12
    assert v.key_index == 7
    assert (v.key_frame.width, v.key_frame.height) == (160, 120)
    with pytest.raises(media.MediaRejected):
        media.load_video(b"not a video")


def test_synthetic_accuracy_regression_guard() -> None:
    """A small fixed sample of shallow-to-mid scenes stays accurate (full table: synth_eval)."""
    diam = list(SYNTH_DIAMETERS_CM)
    errs = []
    for i in range(10):
        rng = np.random.default_rng([77, i])
        p = random_params(rng, diam)
        if p.depth_fraction > 0.5:
            continue
        c = render(p, rng)
        m = measure_wheel_depth(
            c.wheel_mask,
            c.water_mask,
            p.diameter_cm,
            0.0,
            GeometryConfig(mc_draws=0, seed=i),
            rim_mask=c.rim_mask,
        )
        if isinstance(m, DepthMeasurement):
            errs.append(abs(m.depth_cm - c.depth_cm) / p.diameter_cm)
    assert len(errs) >= 3
    assert float(np.median(errs)) < 0.05


def test_eval_report_and_docs_markers(tmp_path: Path) -> None:
    results = [synth_eval.run_case((i, 5, 0)) for i in range(4)]
    body = synth_eval.report(results, seed=5, draws=0, wall=1.0)
    assert "| Subset | Cases | Usable |" in body
    assert "Uncertainty calibration" in body or "Cases declined" in body
    doc = tmp_path / "G.md"
    doc.write_text("intro\n<!-- synth-eval:begin -->\nSTALE TABLE\n<!-- synth-eval:end -->\nend\n")
    synth_eval.write_docs(doc, body)
    text = doc.read_text()
    assert "STALE TABLE" not in text and body in text and text.endswith("end\n")


def test_real_photo_report_and_commons_metadata(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """eval/real_photos.py: every photo has a licence and author, and the report renders."""
    import importlib.util
    import json

    root = Path(__file__).resolve().parents[2] / "eval"
    meta = json.loads((root / "commons_photos.json").read_text())["photos"]
    assert {m["id"] for m in meta if m["set"] == "brief"} == {"c1", "c2", "c3", "c4"}
    for m in meta:
        assert m["licence"] and m["licence_url"].startswith("https://creativecommons.org/")
        assert m["author"] and m["download"].startswith("https://upload.wikimedia.org/")
    spec = importlib.util.spec_from_file_location("real_photos", root / "real_photos.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "RESULTS", tmp_path / "R.md")
    body = {
        "usable": True,
        "method": "model_estimate",
        "depth_cm": 30.0,
        "depth_sigma_cm": 20.0,
        "confidence": 0.2,
        "model_estimate": {"depth_cm": 30.0, "depth_sigma_cm": 20.0, "basis": "knee deep"},
        "captured_at": None,
        "reasons": ["standing water visible"],
        "timings_ms": {"scene": 20000},
        "cost_usd": 0.01,
        "agent_trace": [
            {
                "step": "scene",
                "target": "photo",
                "model": "m",
                "input": "a|b",
                "output": "o",
                "decision": "d",
                "latency_ms": 1,
            }
        ],
    }
    text = mod.summarise("c1", meta[0], body, 20.5)
    assert "named in the brief" in text and "a/b" in text and "CC0" in text
    mod.write_results([text], "header")
    mod.write_results([text.replace("30.0", "31.0")], "header 2")
    out = (tmp_path / "R.md").read_text()
    assert out.count("<!-- real-eval:begin -->") == 1 and "header 2" in out and "31.0" in out


def test_wheel_photo_set_is_credited_and_in_its_own_block(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """eval/wheel_photos.json: permissive licences only, every photo credited; its
    generated block sits beside the main one without replacing it."""
    import importlib.util
    import json

    root = Path(__file__).resolve().parents[2] / "eval"
    meta = json.loads((root / "wheel_photos.json").read_text())["photos"]
    assert 15 <= len(meta) <= 25 and {m["set"] for m in meta} == {"wheel"}
    assert len({m["id"] for m in meta}) == len(meta)
    allowed = {
        "commons": ("CC0", "CC BY", "CC BY-SA"),
        "unsplash": ("Unsplash License",),
        "pexels": ("Pexels License",),
    }
    for m in meta:
        assert m["licence"].startswith(allowed[m["source"]]), m["id"]
        assert m["author"] and m["page"].startswith("https://") and m["licence_url"]
        assert m["download"].startswith("https://")
    spec = importlib.util.spec_from_file_location("real_photos", root / "real_photos.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.SETS["wheel"] == "wheel_photos.json"
    doc = tmp_path / "R.md"
    doc.write_text("# R\n\n<!-- real-eval:begin -->\nold\n<!-- real-eval:end -->\n")
    monkeypatch.setattr(mod, "RESULTS", doc)
    mod.write_results(["wheel one"], "wheel header", "wheel-eval")
    mod.write_results(["wheel two"], "wheel header 2", "wheel-eval")
    out = doc.read_text()
    assert "old" in out and out.count("<!-- wheel-eval:begin -->") == 1
    assert "wheel two" in out and "wheel one" not in out
    assert out.index("real-eval:end") < out.index("wheel-eval:begin")
    text = mod.summarise("x", meta[0], {"usable": False, "reasons": []}, 1.0)
    assert "chosen for the wheel method" in text


def test_eval_gate_compares_against_the_committed_baseline() -> None:
    """eval/gate.py: the committed baseline matches its settings, and worse numbers fail."""
    import importlib.util
    import json

    root = Path(__file__).resolve().parents[2] / "eval"
    spec = importlib.util.spec_from_file_location("gate", root / "gate.py")
    assert spec is not None and spec.loader is not None
    gate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gate)
    base = json.loads((root / "baseline.json").read_text(encoding="utf-8"))
    assert base["settings"] == gate.SETTINGS and base["tolerance"] == gate.TOLERANCE
    now = json.loads(json.dumps(base["results"]))
    assert all(ok for *_, ok in gate.compare(now, base))
    # Within the tolerances: still a pass.
    now["synthetic"]["mae_cm"] += 0.01
    now["person"]["within_1_sigma"] -= 0.004
    assert all(ok for *_, ok in gate.compare(now, base))
    # Beyond them: each one fails on its own.
    for ev, metric, delta in (
        ("synthetic", "mae_cm", 0.05),
        ("synthetic", "within_1_sigma", -0.01),
        ("person", "measured_share", -0.01),
        ("person", "mae_cm", 0.05),
    ):
        worse = json.loads(json.dumps(base["results"]))
        worse[ev][metric] += delta
        failed = [(e, m) for e, m, *_, ok in gate.compare(worse, base) if not ok]
        assert failed == [(ev, metric)], (ev, metric, failed)
