"""Signed measurement certificates (docs/CONTRACTS.md, section 1.2; docs/VERIFY.md).

A certificate is a compact record of one measurement: the hash of the photo,
the models and prompt versions used, the agent's steps and decisions, the
geometry numbers the depth was computed from, and the final decision. Its
canonical JSON text is signed with Ed25519, and the envelope carries that text
verbatim, so a verifier checks the signature over exactly the bytes signed and
never has to re-serialise anything (Go's ``encoding/json`` and Python's
``json`` write numbers differently).

The private key is a 32-byte Ed25519 seed in ``NEERMATTAM_SIGNING_KEY``
(base64url), read from the environment or the repository ``.env``. It is never
logged, printed or returned; only the public key leaves the process.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

FORMAT = "neermattam-certificate/1"
ALG = "Ed25519"
ENV_KEY = "NEERMATTAM_SIGNING_KEY"

log = logging.getLogger(__name__)


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def unb64url(text: str) -> bytes:
    """Base64url with or without padding; standard base64 is accepted too."""
    t = text.strip().replace("+", "-").replace("/", "_")
    return base64.urlsafe_b64decode(t + "=" * (-len(t) % 4))


def canonical(record: Mapping[str, Any]) -> str:
    """The record's canonical JSON text: keys sorted, no spaces, ASCII only.

    ``allow_nan=False``: a NaN or infinity in a record is a bug, not a number."""
    return json.dumps(
        record, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    )


def key_id(public_key: bytes) -> str:
    """``ed25519:`` and the first 16 hex digits of the SHA-256 of the 32 key bytes."""
    return "ed25519:" + hashlib.sha256(public_key).hexdigest()[:16]


def _raw_public(pub: Ed25519PublicKey) -> bytes:
    return pub.public_bytes(Encoding.Raw, PublicFormat.Raw)


@dataclass
class Signer:
    """Signs certificates with one Ed25519 key. ``ephemeral`` is True for a key
    made for this process only (no ``NEERMATTAM_SIGNING_KEY``)."""

    _private: Ed25519PrivateKey = field(repr=False)
    ephemeral: bool = False

    @classmethod
    def from_seed(cls, seed: bytes, *, ephemeral: bool = False) -> Signer:
        if len(seed) != 32:
            raise ValueError(f"{ENV_KEY} must hold a 32-byte Ed25519 seed")
        return cls(Ed25519PrivateKey.from_private_bytes(seed), ephemeral)

    @classmethod
    def generate(cls) -> Signer:
        return cls(Ed25519PrivateKey.generate(), ephemeral=True)

    @classmethod
    def load_env(cls, start: Path | None = None) -> Signer | None:
        """The key in ``NEERMATTAM_SIGNING_KEY`` (the environment first, then the
        nearest ``.env``), or None when it is not set."""
        from .muse_open import env_from_dotenv  # the one .env reader

        value = os.environ.get(ENV_KEY) or env_from_dotenv((ENV_KEY,), start).get(ENV_KEY)
        if not value:
            return None
        try:
            return cls.from_seed(unb64url(value))
        except ValueError as exc:
            raise ValueError(f"{ENV_KEY} is not a base64url 32-byte seed") from exc

    @classmethod
    def from_env(cls, start: Path | None = None) -> Signer:
        """The configured key; otherwise a key for the life of this process, with a
        warning (certificates it signs will not verify after a restart)."""
        signer = cls.load_env(start)
        if signer is not None:
            return signer
        log.warning(
            "%s is not set: certificates are signed with a key made for this process "
            "and will not verify after a restart",
            ENV_KEY,
        )
        return cls.generate()

    @property
    def public_key(self) -> bytes:
        return _raw_public(self._private.public_key())

    @property
    def key_id(self) -> str:
        return key_id(self.public_key)

    def public_entry(self) -> dict[str, str]:
        """This key as an entry of ``GET /v1/keys``."""
        return {"key_id": self.key_id, "alg": ALG, "public_key": b64url(self.public_key)}

    def sign(self, record: Mapping[str, Any]) -> dict[str, str]:
        """The certificate envelope for ``record``."""
        payload = canonical(record)
        signature = self._private.sign(payload.encode("utf-8"))
        return {
            "format": FORMAT,
            "alg": ALG,
            "key_id": self.key_id,
            "payload": payload,
            "signature": b64url(signature),
        }


def new_seed_b64() -> str:
    """A fresh 32-byte seed, base64url, for ``NEERMATTAM_SIGNING_KEY``."""
    return b64url(os.urandom(32))


# --------------------------------------------------------------------------- verify


@dataclass(frozen=True, slots=True)
class Verification:
    valid: bool
    reason: str  # one plain sentence
    key_id: str | None = None
    record: dict[str, Any] | None = None


def envelope_of(doc: Any) -> dict[str, Any] | None:
    """The certificate envelope in ``doc``: the envelope itself, or a ``/v1/measure``
    response (or a gateway report reply) carrying one under ``certificate``."""
    if not isinstance(doc, dict):
        return None
    if isinstance(doc.get("payload"), str) and "signature" in doc:
        return doc
    for k in ("certificate", "measurement"):
        inner = doc.get(k)
        found = envelope_of(inner) if isinstance(inner, dict) else None
        if found is not None:
            return found
    return None


def verify(envelope: Any, keys: Mapping[str, bytes]) -> Verification:
    """Check a certificate's signature against trusted public keys (``key_id`` to
    the 32 raw key bytes). A key named only inside the certificate is never
    trusted."""
    if not isinstance(envelope, dict):
        return Verification(False, "not a certificate")
    if envelope.get("format") != FORMAT:
        return Verification(False, f"unknown certificate format {envelope.get('format')!r}")
    if envelope.get("alg") != ALG:
        return Verification(False, f"unknown signature algorithm {envelope.get('alg')!r}")
    kid = envelope.get("key_id")
    payload, sig = envelope.get("payload"), envelope.get("signature")
    if not isinstance(kid, str) or not isinstance(payload, str) or not isinstance(sig, str):
        return Verification(False, "the certificate lacks a key id, payload or signature")
    pub = keys.get(kid)
    if pub is None:
        return Verification(False, f"no trusted public key {kid}", kid)
    if key_id(pub) != kid:
        return Verification(False, f"the key given for {kid} does not have that id", kid)
    try:
        raw_sig = unb64url(sig)
    except ValueError:
        return Verification(False, "the signature is not base64url", kid)
    try:
        Ed25519PublicKey.from_public_bytes(pub).verify(raw_sig, payload.encode("utf-8"))
    except (InvalidSignature, ValueError):
        return Verification(False, "the signature does not match the certificate", kid)
    try:
        record = json.loads(payload)
    except json.JSONDecodeError:
        return Verification(False, "the signed payload is not JSON", kid)
    if not isinstance(record, dict) or record.get("format") != FORMAT:
        return Verification(False, "the signed payload is not a certificate record", kid)
    return Verification(True, "the signature matches", kid, record)


def keys_from_document(doc: Any) -> dict[str, bytes]:
    """Trusted keys from a ``GET /v1/keys`` (or ``.well-known``) document; entries
    whose id does not match their key are left out."""
    out: dict[str, bytes] = {}
    entries = doc.get("keys") if isinstance(doc, dict) else None
    for e in entries if isinstance(entries, list) else []:
        if not isinstance(e, dict) or e.get("alg") != ALG:
            continue
        try:
            pub = unb64url(str(e.get("public_key", "")))
        except ValueError:
            continue
        if len(pub) == 32 and e.get("key_id") == key_id(pub):
            out[key_id(pub)] = pub
    return out


def utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
