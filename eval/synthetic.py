"""Synthetic evaluation of the wheel geometry: a thin wrapper.

Runs ``neermattam_perception.synth_eval`` (3D wheels rendered through a pinhole
camera, see docs/GEOMETRY.md section 7) with the same arguments, from
perception/:

    uv run python ../eval/synthetic.py --cases 500 --seed 2026 --draws 100
    uv run python ../eval/synthetic.py --cases 500 --seed 2026 --draws 100 \\
        --write-docs ../docs/GEOMETRY.md
    uv run python ../eval/synthetic.py --calibrate            # refit the model tables
"""

from __future__ import annotations

import sys

from neermattam_perception.synth_eval import main

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
