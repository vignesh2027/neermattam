"""Write the recorded certificates the tests replay and verify.

    cd perception && uv run python tests/fixtures/make_certificates.py

Two measurements of synthetic scenes (a scooter wheel and a person standing in
water), made by the real pipeline with fake models, signed with the TEST key
below. The same files are copied to gateway/internal/certs/testdata/ so the Go
verifier is tested on certificates Python signed. The key is a fixed test
value (bytes 0 to 31), never used outside tests.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent))

from neermattam_perception.certificate import Signer  # noqa: E402
from neermattam_perception.geometry import GeometryConfig  # noqa: E402
from neermattam_perception.media import ImageInput, load_image  # noqa: E402
from neermattam_perception.pipeline import Pipeline, PipelineConfig  # noqa: E402
from neermattam_perception.synth import SynthParams, render  # noqa: E402
from tests.conftest import FakeMasks, FakeScene, reading_for  # noqa: E402
from tests.test_person import person_pipeline  # noqa: E402

TEST_SEED = bytes(range(32))
GO_TESTDATA = HERE.parents[2] / "gateway" / "internal" / "certs" / "testdata"


def png_input(rgb: np.ndarray) -> ImageInput:
    """The image as an upload: PNG bytes through the service's own loader, so the
    certificate records a real input hash."""
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.fromarray(rgb).save(buf, format="PNG")
    return load_image(buf.getvalue())


def main() -> None:
    signer = Signer.from_seed(TEST_SEED)
    p = SynthParams(
        diameter_cm=43.4,
        depth_fraction=0.35,
        yaw_deg=15.0,
        pitch_deg=10.0,
        roll_deg=2.0,
        wheel_px=220.0,
        tyre_width_frac=0.1,
        mask_noise_px=0.4,
        ripple_px=0.5,
        width=640,
        height=480,
    )
    case = render(p, np.random.default_rng(11))
    masks = FakeMasks({1: case.wheel_mask, 2: case.rim_mask, 16: case.water_mask})
    cfg = PipelineConfig(geometry=GeometryConfig(mc_draws=10))
    wheel = Pipeline(FakeScene(reading_for(case)), masks, cfg, signer=signer)
    r1 = asyncio.run(wheel.measure(image=png_input(case.image)))
    pp, _f, rgb, _face = person_pipeline(case, 70.0, landmark="hip")
    pp.signer = signer
    r2 = asyncio.run(pp.measure(image=png_input(rgb)))
    keys = {"keys": [signer.public_entry()]}
    out = {
        "certificate_wheel.json": r1.certificate.model_dump() if r1.certificate else None,
        "certificate_person.json": r2.certificate.model_dump() if r2.certificate else None,
        "certificate_keys.json": keys,
    }
    GO_TESTDATA.mkdir(parents=True, exist_ok=True)
    for name, body in out.items():
        assert body is not None, name
        (HERE / name).write_text(json.dumps(body, indent=1) + "\n", encoding="utf-8")
        shutil.copyfile(HERE / name, GO_TESTDATA / name)
        sys.stdout.write(f"wrote {name}\n")


if __name__ == "__main__":
    main()
