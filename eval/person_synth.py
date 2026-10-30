"""Synthetic check of the person reference: a thin wrapper.

Runs ``neermattam_perception.person_synth`` (front-on figures drawn with the
Drillis and Contini segment proportions, standing in water of known depth, see
docs/GEOMETRY.md section 9) with the same arguments, from perception/:

    uv run python ../eval/person_synth.py --n 400 --seed 11
"""

from __future__ import annotations

import sys

from neermattam_perception.person_synth import main

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
