"""Measurement certificates: signing, tampering, replay, and the service's keys."""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from neermattam_perception import replay as replay_mod
from neermattam_perception.certificate import (
    ENV_KEY,
    FORMAT,
    Signer,
    b64url,
    canonical,
    envelope_of,
    key_id,
    keys_from_document,
    new_seed_b64,
    unb64url,
    verify,
)
from neermattam_perception.pipeline import Pipeline
from neermattam_perception.service import create_app

from .conftest import FakeMasks, FakeScene, case_image, reading_for
from .test_pipeline import FAST
from .test_service import jpeg_with_exif

FIXTURES = Path(__file__).parent / "fixtures"
TEST_SEED = bytes(range(32))  # the fixtures' key; a test value only


def fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def trusted(signer: Signer) -> dict[str, bytes]:
    return {signer.key_id: signer.public_key}


def resign(env: dict[str, Any], record: dict[str, Any], signer: Signer) -> dict[str, Any]:
    return {**env, **signer.sign(record)}


# --------------------------------------------------------------------------- signatures


def test_signature_round_trip() -> None:
    s = Signer.from_seed(TEST_SEED)
    record = {"format": FORMAT, "measurement_id": "m_X", "note": "Velachery, முழங்கால்"}
    env = s.sign(record)
    assert env["payload"] == canonical(record) and env["payload"].isascii()
    assert env["key_id"] == key_id(s.public_key) and env["key_id"].startswith("ed25519:")
    assert len(unb64url(env["signature"])) == 64
    v = verify(env, trusted(s))
    assert v.valid and v.record == record and v.reason == "the signature matches"
    # The envelope survives any re-serialisation: the signature is over the string.
    pretty = json.loads(json.dumps(env, indent=3, ensure_ascii=False))
    assert verify(pretty, trusted(s)).valid


def test_tampered_certificates_fail() -> None:
    s = Signer.from_seed(TEST_SEED)
    env = fixture("certificate_wheel.json")
    keys = keys_from_document(fixture("certificate_keys.json"))
    assert verify(env, keys).valid
    # A deeper depth written into the payload.
    rec = json.loads(env["payload"])
    rec["decision"]["depth_cm"] = 45.0
    forged = {**env, "payload": canonical(rec)}
    assert verify(forged, keys).reason == "the signature does not match the certificate"
    # One flipped bit of the signature.
    sig = bytearray(unb64url(env["signature"]))
    sig[0] ^= 1
    assert not verify({**env, "signature": b64url(bytes(sig))}, keys).valid
    # Signed by someone else's key, even one named in the certificate.
    other = Signer.generate()
    v = verify(resign(env, rec, other), keys)
    assert not v.valid and v.reason.startswith("no trusted public key")
    # A key filed under the wrong id is not used.
    assert not verify(env, {env["key_id"]: other.public_key}).valid
    assert not verify({**env, "format": "x"}, keys).valid
    assert not verify({**env, "alg": "RS256"}, keys).valid
    assert verify(s.sign({"format": "other"}), trusted(s)).reason.startswith("the signed payload")


def test_keys_and_seeds() -> None:
    s = Signer.from_seed(TEST_SEED)
    doc = {
        "keys": [s.public_entry(), {"key_id": "ed25519:0", "alg": "Ed25519", "public_key": "AA"}]
    }
    assert keys_from_document(doc) == {s.key_id: s.public_key}
    assert len(unb64url(new_seed_b64())) == 32
    with pytest.raises(ValueError):
        Signer.from_seed(b"short")
    assert "_private" not in repr(s) or "Ed25519PrivateKey" not in repr(s)


def test_signer_from_env_and_dotenv(tmp_path: Path, monkeypatch, caplog) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv(ENV_KEY, raising=False)
    seed = new_seed_b64()
    (tmp_path / ".env").write_text(f"OTHER=1\n{ENV_KEY}={seed}\n", encoding="utf-8")
    s = Signer.load_env(tmp_path)
    assert s is not None and s.public_key == Signer.from_seed(unb64url(seed)).public_key
    empty = tmp_path / "sub"
    empty.mkdir()
    (empty / ".env").write_text("OTHER=1\n", encoding="utf-8")
    assert Signer.load_env(empty) is None
    made = Signer.from_env(empty)
    assert made.ephemeral and ENV_KEY in caplog.text and seed not in caplog.text


# --------------------------------------------------------------------------- replay


@pytest.mark.parametrize(
    ("name", "method"),
    [("certificate_wheel.json", "wheel_geometry"), ("certificate_person.json", "person_reference")],
)
def test_replay_matches_recorded_certificates(name: str, method: str, capsys) -> None:  # type: ignore[no-untyped-def]
    keys = str(FIXTURES / "certificate_keys.json")
    assert replay_mod.main([str(FIXTURES / name), "--keys", keys]) == 0
    out = capsys.readouterr().out
    assert "signature: valid" in out and f"method: {method}" in out
    assert out.rstrip().endswith("result: match") and "MISMATCH" not in out
    v = verify(fixture(name), keys_from_document(fixture("certificate_keys.json")))
    r = replay_mod.replay(v.record or {})
    assert r.match and r.method == method
    names = {c.name for c in r.comparisons}
    assert {"published depth", "published sigma"} <= names
    assert any(n.startswith("wheel:" if method == "wheel_geometry" else "person:") for n in names)


def test_replay_refuses_a_tampered_certificate(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    env = fixture("certificate_person.json")
    rec = json.loads(env["payload"])
    rec["geometry"]["person"]["bottom_y"] += 30  # a lower waterline: deeper water
    path = tmp_path / "forged.json"
    path.write_text(json.dumps({**env, "payload": canonical(rec)}), encoding="utf-8")
    keys = str(FIXTURES / "certificate_keys.json")
    assert replay_mod.main([str(path), "--keys", keys]) == 2
    assert "signature: INVALID" in capsys.readouterr().out


def test_replay_catches_numbers_that_do_not_add_up(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    """Even with a valid signature (the key holder lying), the published depth must
    follow from the recorded geometry."""
    s = Signer.from_seed(TEST_SEED)
    env = fixture("certificate_wheel.json")
    rec = json.loads(env["payload"])
    rec["decision"]["depth_cm"] = round(rec["decision"]["depth_cm"] + 6.0, 1)
    rec["geometry"]["wheel"]["ellipse"]["a"] *= 1.02
    path = tmp_path / "lie.json"
    path.write_text(json.dumps(resign(env, rec, s)), encoding="utf-8")
    keys = str(FIXTURES / "certificate_keys.json")
    assert replay_mod.main([str(path), "--keys", keys]) == 1
    out = capsys.readouterr().out
    assert "signature: valid" in out and "MISMATCH" in out
    assert out.rstrip().endswith("result: MISMATCH")
    bad = [line for line in out.splitlines() if "MISMATCH" in line and "published depth" in line]
    assert bad and "diff -5.9" in bad[0]


def test_replay_of_an_estimate_and_of_a_wheel_set_aside(easy_case) -> None:  # type: ignore[no-untyped-def]
    s = Signer.from_seed(TEST_SEED)
    masks = FakeMasks({1: easy_case.wheel_mask, 2: easy_case.rim_mask, 16: easy_case.water_mask})
    # The scene model says 80 cm at the wheel: far above what the wheel measures.
    base = reading_for(easy_case)
    deep = replace(
        base,
        references=(replace(base.references[0], depth_cm=80.0),),
        overall_depth_cm=80.0,
        overall_low_cm=60.0,
        overall_high_cm=100.0,
    )
    r = asyncio.run(
        Pipeline(FakeScene(deep), masks, FAST, signer=s).measure(image=case_image(easy_case))
    )
    assert r.method == "model_estimate" and r.certificate is not None
    v = verify(r.certificate.model_dump(), trusted(s))
    rp = replay_mod.replay(v.record or {})
    assert rp.match, [c for c in rp.comparisons if not c.ok]
    aside = next(c for c in rp.comparisons if c.name.startswith("decision: wheel set aside"))
    assert aside.recorded is True and aside.recomputed is True
    # No wheel at all: the estimate alone.
    only = replace(
        base, references=(), overall_depth_cm=30.0, overall_low_cm=None, overall_high_cm=None
    )
    r2 = asyncio.run(
        Pipeline(FakeScene(only), masks, FAST, signer=s).measure(image=case_image(easy_case))
    )
    assert r2.method == "model_estimate" and r2.certificate is not None
    rp2 = replay_mod.replay(verify(r2.certificate.model_dump(), trusted(s)).record or {})
    assert rp2.match and [c.name for c in rp2.comparisons] == [
        "estimate: sigma",
        "published depth",
        "published sigma",
    ]


def test_unusable_photo_still_gets_a_certificate(easy_case) -> None:  # type: ignore[no-untyped-def]
    s = Signer.from_seed(TEST_SEED)
    p = Pipeline(FakeScene(reading_for(easy_case, water=False)), FakeMasks({}), FAST, signer=s)
    r = asyncio.run(p.measure(image=case_image(easy_case)))
    assert not r.usable and r.certificate is not None
    rec = verify(r.certificate.model_dump(), trusted(s)).record or {}
    assert rec["decision"]["usable"] is False and rec["geometry"]["wheel"] is None
    assert replay_mod.replay(rec).notes == [
        "the photo was not usable: there is no depth to recompute"
    ]


def test_certificate_record_contents(easy_case) -> None:  # type: ignore[no-untyped-def]
    rec = json.loads(fixture("certificate_wheel.json")["payload"])
    assert rec["format"] == FORMAT and rec["measurement_id"].startswith("m_")
    assert set(rec) == {
        "checks",
        "decision",
        "format",
        "geometry",
        "input",
        "issued_at",
        "measurement_id",
        "models",
        "prompts",
        "steps",
    }
    assert len(rec["input"]["sha256"]) == 64 and rec["input"]["kind"] == "image"
    assert [m["role"] for m in rec["models"]] == ["scene", "masks"]
    assert rec["checks"]["rules"] == ["schema", "grid", "inside image", "allowed values"]
    assert {"step", "target", "model", "prompt_version", "decision", "latency_ms"} == set(
        rec["steps"][0]
    )
    w = rec["geometry"]["wheel"]
    assert set(w["ellipse"]) == {"cx", "cy", "a", "b", "theta"}
    assert {"point", "direction", "water_normal"} <= set(w["waterline"])
    assert len(fixture("certificate_wheel.json")["payload"]) < 6000  # compact


# --------------------------------------------------------------------------- the service


def test_service_publishes_its_key_and_signs_every_measurement(easy_case) -> None:  # type: ignore[no-untyped-def]
    s = Signer.from_seed(TEST_SEED)
    masks = FakeMasks({1: easy_case.wheel_mask, 2: easy_case.rim_mask, 16: easy_case.water_mask})
    pipe = Pipeline(FakeScene(reading_for(easy_case)), masks, FAST, signer=s)
    client = TestClient(create_app(pipe))
    keys = client.get("/v1/keys").json()
    assert keys == {"keys": [s.public_entry()]}
    assert TEST_SEED.hex() not in json.dumps(keys) and b64url(TEST_SEED) not in json.dumps(keys)
    data = jpeg_with_exif(easy_case.image)
    r = client.post("/v1/measure", files={"image": ("p.jpg", data, "image/jpeg")})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["input_sha256"] == hashlib.sha256(data).hexdigest()
    env = envelope_of(body)
    v = verify(env, keys_from_document(keys))
    assert v.valid and v.record is not None
    assert v.record["input"]["sha256"] == body["input_sha256"]
    assert v.record["measurement_id"] == body["measurement_id"]
    assert v.record["decision"]["depth_cm"] == body["depth_cm"]
    assert replay_mod.replay(v.record).match
    # The streamed result carries the same certificate shape.
    sr = client.post("/v1/measure/stream", files={"image": ("p.jpg", data, "image/jpeg")})
    result = next(ln for ln in sr.text.split("\n\n") if ln.startswith("event: result"))
    streamed = json.loads(result.split("data: ", 1)[1])
    assert verify(streamed["certificate"], keys_from_document(keys)).valid


def test_service_without_a_signer_has_no_keys(easy_case) -> None:  # type: ignore[no-untyped-def]
    pipe = Pipeline(FakeScene(reading_for(easy_case)), FakeMasks({}), FAST)
    client = TestClient(create_app(pipe))
    assert client.get("/v1/keys").json() == {"keys": []}
