"""Pydantic models for the perception service contract (docs/CONTRACTS.md, section 1)."""

from __future__ import annotations

import os
import time

from pydantic import BaseModel, ConfigDict, Field

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
    diameter_cm: float
    diameter_sigma_cm: float
    source: str


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


class AgentStepOut(_Strict):
    step: str
    target: str
    model: str
    input: str
    output: str
    decision: str
    latency_ms: int


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


class MeasureResponse(_Strict):
    measurement_id: str = Field(default_factory=new_measurement_id)
    usable: bool
    reasons: list[str]
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
    method: str | None = None  # "wheel_geometry" or "model_estimate"
    model_estimate: ModelEstimateOut | None = None
    agent_trace: list[AgentStepOut] = Field(default_factory=list)
    captured_at: str | None = None
