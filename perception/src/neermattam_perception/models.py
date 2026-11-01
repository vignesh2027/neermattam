"""Pydantic models for the perception service contract (docs/CONTRACTS.md, section 1)."""

from __future__ import annotations

import os
import time
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def new_ulid() -> str:
    """A ULID: 48-bit millisecond time then 80 random bits, Crockford base32."""
    value = (int(time.time() * 1000) << 80) | int.from_bytes(os.urandom(10), "big")
    chars = []
    for _ in range(26):
        chars.append(_CROCKFORD[value & 31])
        value >>= 5
    return "".join(reversed(chars))


def new_measurement_id() -> str:
    return f"m_{new_ulid()}"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReferenceOut(_Strict):
    kind: str
    diameter_cm: float | None = None
    diameter_sigma_cm: float | None = None
    source: str
    height_cm: float | None = None  # a person's stature prior (person_reference)
    height_sigma_cm: float | None = None


class WaterlineOut(_Strict):
    method: str
    y_px: float
    agreement_cm: float | None = None


class ModelsOut(_Strict):
    scene: str
    masks: str


class ModelEstimateOut(_Strict):
    """The scene model's own visual estimate of the depth; not a measurement."""

    depth_cm: float
    depth_sigma_cm: float
    basis: str
    model: str


class CodeOut(_Strict):
    """A reason or a decision as a stable code and its values (codes.py, CONTRACTS.md 1.3)."""

    code: str
    args: dict[str, Any] = Field(default_factory=dict)


class AgentStepOut(_Strict):
    step: str
    target: str
    model: str
    input: str
    output: str
    decision: str
    latency_ms: int
    prompt_version: str | None = None  # on steps that called the language model
    decision_code: CodeOut | None = None  # ``decision`` as a code, for translation


class CertificateOut(_Strict):
    """A signed measurement certificate (docs/CONTRACTS.md, section 1.2).

    ``payload`` is the record's canonical JSON text; ``signature`` is the
    base64url Ed25519 signature of its UTF-8 bytes."""

    format: str
    alg: str
    key_id: str
    payload: str
    signature: str


class TranscribeResponse(_Strict):
    text: str
    language: str
    languages: list[str] = Field(default_factory=list)
    place_text: str
    place_spoken: str = ""
    depth_words: str
    depth_words_en: str = ""
    depth_cm: float | None = None
    depth_sigma_cm: float | None = None
    depth_low_cm: float | None = None
    depth_high_cm: float | None = None
    depth_basis: str = ""
    model: str
    cost_usd: float = 0.0
    input_sha256: str | None = None
    prompt_version: str | None = None
    validation: list[str] = Field(default_factory=list)


class MeasureResponse(_Strict):
    measurement_id: str = Field(default_factory=new_measurement_id)
    usable: bool
    reasons: list[str]
    # ``reasons`` as codes, one for one and in the same order (CONTRACTS.md 1.3).
    reason_codes: list[CodeOut] = Field(default_factory=list)
    depth_cm: float | None = None
    depth_sigma_cm: float | None = None
    reference: ReferenceOut | None = None
    waterline: WaterlineOut | None = None
    velocity_mps: float | None = None
    velocity_sigma_mps: float | None = None
    confidence: float = 0.0
    overlay_png_b64: str | None = None
    models: ModelsOut
    timings_ms: dict[str, int] = Field(default_factory=dict)
    cost_usd: float = 0.0
    method: str | None = None  # "wheel_geometry", "person_reference" or "model_estimate"
    model_estimate: ModelEstimateOut | None = None
    agent_trace: list[AgentStepOut] = Field(default_factory=list)
    captured_at: str | None = None
    input_sha256: str | None = None
    certificate: CertificateOut | None = None
    # Head regions of the people found, in image pixels: the service blurs them in
    # any image it stores. Not part of the response body.
    _faces: list[tuple[int, int, int, int]] = PrivateAttr(default_factory=list)
