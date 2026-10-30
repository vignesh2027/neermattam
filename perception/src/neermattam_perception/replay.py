"""Replay a measurement certificate: check its signature, then recompute the depth.

    uv run python -m neermattam_perception.replay <certificate.json>
    uv run python -m neermattam_perception.replay cert.json --keys https://host/.well-known/neermattam-keys.json
    uv run python -m neermattam_perception.replay cert.json --public-key <base64url>

The file may be the certificate itself (as served at the gateway's
``/api/certificates/{id}``), a ``/v1/measure`` response that carries one, or a
gateway report reply. Without ``--keys`` or ``--public-key`` the public key is
derived from ``NEERMATTAM_SIGNING_KEY`` in the environment or the repository
``.env`` (the operator's own check); a key named only inside the certificate is
never trusted.

The recomputation calls the service's own geometry code on the numbers the
certificate records (the fitted ellipse, the waterline, the reference size, the
person's outline rows, the scene model's estimate), with no model call and no
mask: the submerged fraction, the bias correction, the model error at the
visible arc, the tyre squash, the second waterline's disagreement, the person's
stature scaling and the shallow-side rule that can set a wheel aside. The one
recorded input it does not redo is the bootstrap spread of a wheel's depth,
which needs the outline points (docs/VERIFY.md).

Exit status: 0 when every recomputed number matches, 1 on a mismatch, 2 when
the signature does not verify or the file is not a certificate.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from .certificate import (
    ENV_KEY,
    Signer,
    envelope_of,
    key_id,
    keys_from_document,
    unb64url,
    verify,
)
from .geometry import (
    Ellipse,
    GeometryConfig,
    Line,
    PinholeCamera,
    compose_depth,
    depth_at_line,
    fraction_for,
)
from .person import person_depth_from_rows
from .pipeline import (
    estimate_sigma,
    person_final_sigma,
    shallow_conflict,
    wheel_final,
)

# A recomputed number matches a recorded full-precision one within this (the
# same code on the same numbers gives the same answer, up to float rounding).
EXACT_TOL = 1e-6
# Published depths and sigmas are rounded to 0.1 cm.
PUBLISHED_TOL = 0.05 + 1e-9


@dataclass(frozen=True, slots=True)
class Comparison:
    name: str
    recorded: float | bool | None
    recomputed: float | bool | None
    unit: str = ""
    tol: float = EXACT_TOL

    @property
    def diff(self) -> float | None:
        if isinstance(self.recorded, bool) or isinstance(self.recomputed, bool):
            return None
        if self.recorded is None or self.recomputed is None:
            return None
        return float(self.recomputed) - float(self.recorded)

    @property
    def ok(self) -> bool:
        if isinstance(self.recorded, bool) or isinstance(self.recomputed, bool):
            return self.recorded == self.recomputed
        if self.recorded is None or self.recomputed is None:
            return self.recorded is None and self.recomputed is None
        d = self.diff
        return d is not None and abs(d) <= self.tol


@dataclass
class Replay:
    method: str | None
    comparisons: list[Comparison] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def match(self) -> bool:
        return all(c.ok for c in self.comparisons)

    def add(self, *a: Any, **kw: Any) -> None:
        self.comparisons.append(Comparison(*a, **kw))


def _num(v: Any) -> float | None:
    return None if v is None else float(v)


def _line(d: Mapping[str, Any]) -> Line:
    return Line(np.asarray(d["point"], dtype=np.float64), np.asarray(d["direction"], np.float64))


def _wheel(w: Mapping[str, Any], out: Replay) -> tuple[float, float]:
    """Recompute one wheel block; returns its published depth and sigma."""
    e = w["ellipse"]
    ellipse = Ellipse(float(e["cx"]), float(e["cy"]), float(e["a"]), float(e["b"]), e["theta"])
    wl = w["waterline"]
    line = _line(wl)
    normal = np.asarray(wl["water_normal"], dtype=np.float64)
    cam = w.get("camera")
    camera = (
        None
        if cam is None
        else PinholeCamera(float(cam["focal_px"]), float(cam["cx"]), float(cam["cy"]))
    )
    ref = w["reference"]
    diameter = float(ref["diameter_cm"])
    raw = fraction_for(ellipse, line, normal, camera)
    parts = compose_depth(
        raw,
        diameter,
        float(w["arc_covered_deg"]),
        float(w["ambiguity_spread"]),
        0.0,  # the fraction's own sigma is not published
        float(w["resample_sigma_cm"]),
        GeometryConfig(),
    )
    out.add("wheel: submerged fraction before bias", w["fraction_raw"], raw)
    out.add("wheel: bias correction", w["fraction_bias"], parts.fraction_bias)
    out.add("wheel: model error share at this arc", w["model_sigma_share"], parts.model_sigma_share)
    out.add("wheel: depth", w["depth_cm"], parts.depth_cm, "cm")
    out.add("wheel: sigma", w["depth_sigma_cm"], parts.depth_sigma_cm, "cm")
    agreement = None
    if w.get("second_waterline") is not None:
        d2 = depth_at_line(
            ellipse, _line(w["second_waterline"]), normal, diameter, parts.fraction_bias, camera
        )
        agreement = abs(d2 - parts.depth_cm)
        out.add("wheel: second waterline disagreement", w.get("agreement_cm"), agreement, "cm")
    elif w.get("agreement_cm") is not None:
        out.add("wheel: second waterline disagreement", w["agreement_cm"], None, "cm")
    return wheel_final(
        parts.depth_cm,
        parts.depth_sigma_cm,
        float(ref["squash_cm"]),
        float(ref["squash_sigma_cm"]),
        agreement,
    )


def _person(p: Mapping[str, Any], out: Replay) -> tuple[float, float]:
    n = person_depth_from_rows(
        float(p["top_y"]),
        float(p["shoulder_first_y"]),
        float(p["shoulder_full_y"]),
        float(p["bottom_y"]),
        p.get("landmark"),
    )
    out.add("person: visible share of stature", p["visible_share"], n.visible_share)
    out.add("person: depth", p["depth_cm"], n.depth_cm, "cm")
    out.add("person: sigma", p["depth_sigma_cm"], n.depth_sigma_cm, "cm")
    out.add(
        "person: depth at the named landmark",
        p.get("landmark_depth_cm"),
        n.landmark_depth_cm,
        "cm",
    )
    agreement = None
    if n.landmark_depth_cm is not None:
        agreement = abs(n.landmark_depth_cm - n.depth_cm)
    return n.depth_cm, person_final_sigma(n.depth_sigma_cm, agreement)


def replay(record: Mapping[str, Any]) -> Replay:
    """Recompute a verified certificate record's numbers with the service's own code."""
    decision = record.get("decision") or {}
    geo = record.get("geometry") or {}
    method = decision.get("method")
    out = Replay(method)
    wheel = person = est = None
    if geo.get("wheel"):
        wheel = _wheel(geo["wheel"], out)
    if geo.get("person"):
        person = _person(geo["person"], out)
    if geo.get("estimate"):
        g = geo["estimate"]
        sig = estimate_sigma(float(g["depth_cm"]), _num(g.get("low_cm")), _num(g.get("high_cm")))
        out.add("estimate: sigma", g["sigma_cm"], sig, "cm")
        est = (float(g["depth_cm"]), sig)

    expected: tuple[float, float] | None = None
    if method == "wheel_geometry":
        expected = wheel
        if wheel is None:
            out.notes.append("the certificate says wheel_geometry but records no wheel")
    elif method == "person_reference":
        expected = person
        if person is None:
            out.notes.append("the certificate says person_reference but records no person")
    elif method == "model_estimate":
        expected = est
        if est is None:
            out.notes.append("the certificate says model_estimate but records no estimate")
    elif decision.get("usable"):
        out.notes.append(f"unknown method {method!r}: nothing to recompute")
    else:
        out.notes.append("the photo was not usable: there is no depth to recompute")

    # The shallow-side rule: a wheel is set aside exactly when the scene model's
    # estimate lies above its two-sigma bound by more than half the estimate's sigma.
    if wheel is not None and geo["wheel"].get("estimate_here_cm") is not None:
        here = float(geo["wheel"]["estimate_here_cm"])
        set_aside = shallow_conflict(wheel[0], wheel[1], here, estimate_sigma(here))
        out.add("decision: wheel set aside as too shallow", method == "model_estimate", set_aside)

    if expected is None and method in ("wheel_geometry", "person_reference", "model_estimate"):
        out.add("published depth", _num(decision.get("depth_cm")), None, "cm")
    elif expected is not None:
        out.add("published depth", _num(decision.get("depth_cm")), expected[0], "cm", PUBLISHED_TOL)
        out.add(
            "published sigma",
            _num(decision.get("depth_sigma_cm")),
            expected[1],
            "cm",
            PUBLISHED_TOL,
        )
    return out


# --------------------------------------------------------------------------- command line


def _load_keys(args: argparse.Namespace) -> tuple[dict[str, bytes], str]:
    if args.public_key:
        pub = unb64url(args.public_key)
        return {key_id(pub): pub}, "--public-key"
    if args.keys:
        src = str(args.keys)
        if src.startswith(("http://", "https://")):
            import httpx

            transport = httpx.HTTPTransport(local_address="0.0.0.0", retries=2)  # IPv4
            with httpx.Client(transport=transport, timeout=30.0) as c:
                r = c.get(src)
                r.raise_for_status()
                doc = r.json()
        else:
            doc = json.loads(Path(src).read_text(encoding="utf-8"))
        return keys_from_document(doc), src
    signer = Signer.load_env()
    if signer is None:
        return {}, "none"
    return {signer.key_id: signer.public_key}, f"{ENV_KEY} in the environment or .env"


def _fmt(v: float | bool | None, unit: str) -> str:
    if v is None:
        return "none"
    if isinstance(v, bool):
        return "yes" if v else "no"
    text = f"{v:.4f}" if abs(v) < 10 else f"{v:.3f}"
    return f"{text} {unit}".rstrip()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("certificate", type=Path, help="certificate JSON, or a response carrying one")
    ap.add_argument("--keys", help="a keys document (file or URL), as GET /v1/keys")
    ap.add_argument("--public-key", help="one trusted Ed25519 public key, base64url")
    ap.add_argument("--json", action="store_true", help="print the result as JSON")
    args = ap.parse_args(argv)
    out = sys.stdout
    try:
        doc = json.loads(args.certificate.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        out.write(f"cannot read {args.certificate}: {exc}\n")
        return 2
    env = envelope_of(doc)
    if env is None:
        out.write(f"{args.certificate} holds no certificate\n")
        return 2
    keys, source = _load_keys(args)
    v = verify(env, keys)
    if not v.valid or v.record is None:
        if args.json:
            out.write(json.dumps({"signature": "invalid", "reason": v.reason}) + "\n")
        else:
            out.write(f"signature: INVALID: {v.reason} (keys from {source})\n")
        return 2
    rec = v.record
    r = replay(rec)
    if args.json:
        out.write(
            json.dumps(
                {
                    "signature": "valid",
                    "key_id": v.key_id,
                    "measurement_id": rec.get("measurement_id"),
                    "method": r.method,
                    "result": "match" if r.match else "mismatch",
                    "comparisons": [
                        {
                            "name": c.name,
                            "recorded": c.recorded,
                            "recomputed": c.recomputed,
                            "diff": c.diff,
                            "ok": c.ok,
                        }
                        for c in r.comparisons
                    ],
                    "notes": r.notes,
                },
                indent=1,
            )
            + "\n"
        )
        return 0 if r.match else 1
    inp = rec.get("input") or {}
    out.write(f"certificate {rec.get('measurement_id')} issued {rec.get('issued_at')}\n")
    out.write(f"signature: valid, key {v.key_id} (keys from {source})\n")
    out.write(f"input: {inp.get('kind')} sha256 {inp.get('sha256')}\n")
    out.write(f"method: {r.method}\n")
    width = max((len(c.name) for c in r.comparisons), default=10)
    for c in r.comparisons:
        d = c.diff
        diff = "" if d is None else f"  diff {d:+.2e}" if abs(d) < 1e-3 and d != 0 else ""
        if d is not None and not diff:
            diff = f"  diff {d:+.4f} {c.unit}".rstrip()
        out.write(
            f"  {c.name:<{width}}  recorded {_fmt(c.recorded, c.unit):>14}  "
            f"recomputed {_fmt(c.recomputed, c.unit):>14}{diff}  "
            f"{'match' if c.ok else 'MISMATCH'}\n"
        )
    for n in r.notes:
        out.write(f"  note: {n}\n")
    out.write(f"result: {'match' if r.match else 'MISMATCH'}\n")
    return 0 if r.match else 1


if __name__ == "__main__":
    raise SystemExit(main())
