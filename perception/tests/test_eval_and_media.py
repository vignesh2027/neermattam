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
