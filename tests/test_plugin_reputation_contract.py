import hashlib
import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import (
    decode_dss_signature,
    encode_dss_signature,
)

from fluxgram_backend.reputation.protocol import (
    ArchiveStatus,
    KeyStatus,
    ProtocolError,
    ReputationQuery,
    ReputationResponse,
    SignatureEnvelope,
    TransportFrame,
)
from fluxgram_backend.reputation.verification import (
    P256_ORDER,
    VerificationError,
    canonical_message,
    verify_signed_record,
)

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def golden() -> dict[str, str]:
    path = ROOT / "fixtures/plugin-reputation/v1/golden.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def test_java_golden_vector_round_trips_and_verifies(golden: dict[str, str]) -> None:
    query_bytes = bytes.fromhex(golden["query"])
    response_bytes = bytes.fromhex(golden["response"])
    envelope_bytes = bytes.fromhex(golden["envelope"])
    frame_bytes = bytes.fromhex(golden["frame"])

    query = ReputationQuery.decode(query_bytes)
    response = ReputationResponse.decode(response_bytes)
    envelope = SignatureEnvelope.decode(envelope_bytes)
    frame = TransportFrame.decode(frame_bytes)

    assert query == ReputationQuery(golden["archive_sha256"], golden["developer_key_id"])
    assert response == ReputationResponse(
        golden["archive_sha256"],
        golden["developer_key_id"],
        7,
        1_700_000_000_000,
        1_700_086_400_000,
        ArchiveStatus.CLEAR,
        KeyStatus.CLEAR,
    )
    assert query.encode() == query_bytes
    assert response.encode() == response_bytes
    assert envelope.encode() == envelope_bytes
    assert frame.encode() == frame_bytes
    assert frame == TransportFrame(response_bytes, envelope_bytes)
    assert envelope.public_key.hex() == golden["public_key"]
    assert envelope.signature.hex() == golden["signature"]
    assert canonical_message(response, envelope.algorithm, envelope.public_key).hex() == (
        golden["canonical_message"]
    )

    verify_signed_record(
        query,
        response_bytes,
        envelope_bytes,
        golden["authority_key_id"],
        1_700_043_200_000,
    )


def test_no_developer_key_variant_round_trips() -> None:
    query = ReputationQuery("a" * 64)
    response = ReputationResponse(
        "a" * 64,
        None,
        1,
        100,
        200,
        ArchiveStatus.MALICIOUS,
        KeyStatus.NOT_APPLICABLE,
    )

    assert len(query.encode()) == 44
    assert len(response.encode()) == 76
    assert ReputationQuery.decode(query.encode()) == query
    assert ReputationResponse.decode(response.encode()) == response


@pytest.mark.parametrize(
    "encoded",
    [
        b"",
        b"\x00" * 44,
        bytes.fromhex("464752510000000100000002") + b"\x00" * 32,
        bytes.fromhex("464752510000000100000001") + b"\x00" * 32,
        bytes.fromhex("464752510000000100000000") + b"\x00" * 33,
    ],
)
def test_query_rejects_malformed_or_noncanonical_input(encoded: bytes) -> None:
    with pytest.raises(ProtocolError):
        ReputationQuery.decode(encoded)


def test_verifier_rejects_identity_authority_signature_and_time(golden: dict[str, str]) -> None:
    query = ReputationQuery.decode(bytes.fromhex(golden["query"]))
    response = bytes.fromhex(golden["response"])
    envelope = bytes.fromhex(golden["envelope"])

    cases = (
        (
            ReputationQuery("c" * 64, query.developer_key_id),
            response,
            envelope,
            golden["authority_key_id"],
            1_700_043_200_000,
        ),
        (query, response, envelope, "p256:" + "d" * 64, 1_700_043_200_000),
        (
            query,
            response,
            envelope[:-1] + bytes([envelope[-1] ^ 1]),
            golden["authority_key_id"],
            1_700_043_200_000,
        ),
        (query, response, envelope, golden["authority_key_id"], 1_699_999_999_999),
        (query, response, envelope, golden["authority_key_id"], 1_700_086_400_000),
    )

    for arguments in cases:
        with pytest.raises((ProtocolError, VerificationError)):
            verify_signed_record(*arguments)


def test_verifier_rejects_high_s_signature(golden: dict[str, str]) -> None:
    query = ReputationQuery.decode(bytes.fromhex(golden["query"]))
    response = bytes.fromhex(golden["response"])
    envelope = SignatureEnvelope.decode(bytes.fromhex(golden["envelope"]))
    r, s = decode_dss_signature(envelope.signature)
    high_s = P256_ORDER - s
    high_envelope = SignatureEnvelope(
        envelope.algorithm,
        envelope.public_key,
        encode_dss_signature(r, high_s),
    ).encode()

    with pytest.raises(VerificationError, match="low-S"):
        verify_signed_record(
            query,
            response,
            high_envelope,
            golden["authority_key_id"],
            1_700_043_200_000,
        )


def test_verifier_serves_expired_block_but_rejects_expired_clear() -> None:
    query = ReputationQuery("a" * 64)
    clear = ReputationResponse(
        query.archive_sha256,
        None,
        1,
        100,
        200,
        ArchiveStatus.CLEAR,
        KeyStatus.NOT_APPLICABLE,
    )
    blocked = ReputationResponse(
        query.archive_sha256,
        None,
        2,
        100,
        200,
        ArchiveStatus.MALICIOUS,
        KeyStatus.NOT_APPLICABLE,
    )
    private_key = ec.generate_private_key(ec.SECP256R1())
    public_key = private_key.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    authority_key_id = "p256:" + hashlib.sha256(public_key).hexdigest()

    clear_envelope = _sign_response(clear, private_key, public_key)
    blocked_envelope = _sign_response(blocked, private_key, public_key)

    with pytest.raises(VerificationError, match="expired"):
        verify_signed_record(query, clear.encode(), clear_envelope, authority_key_id, 200)

    verify_signed_record(query, blocked.encode(), blocked_envelope, authority_key_id, 200)


def test_verifier_serves_expired_revoked_key() -> None:
    query = ReputationQuery("a" * 64, "p256:" + "b" * 64)
    response = ReputationResponse(
        query.archive_sha256,
        query.developer_key_id,
        3,
        100,
        200,
        ArchiveStatus.CLEAR,
        KeyStatus.REVOKED,
    )
    private_key = ec.generate_private_key(ec.SECP256R1())
    public_key = private_key.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    authority_key_id = "p256:" + hashlib.sha256(public_key).hexdigest()
    envelope = _sign_response(response, private_key, public_key)

    verify_signed_record(query, response.encode(), envelope, authority_key_id, 200)


def test_verifier_rejects_unsupported_algorithm_and_curve(golden: dict[str, str]) -> None:
    query = ReputationQuery.decode(bytes.fromhex(golden["query"]))
    response = bytes.fromhex(golden["response"])
    envelope = SignatureEnvelope.decode(bytes.fromhex(golden["envelope"]))
    unsupported_algorithm = SignatureEnvelope(
        2,
        envelope.public_key,
        envelope.signature,
    ).encode()

    with pytest.raises(VerificationError, match="algorithm"):
        verify_signed_record(
            query,
            response,
            unsupported_algorithm,
            golden["authority_key_id"],
            1_700_043_200_000,
        )

    wrong_key = ec.generate_private_key(ec.SECP384R1()).public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    wrong_curve = SignatureEnvelope(1, wrong_key, envelope.signature).encode()
    wrong_authority = "p256:" + hashlib.sha256(wrong_key).hexdigest()

    with pytest.raises(VerificationError, match="P-256"):
        verify_signed_record(
            query,
            response,
            wrong_curve,
            wrong_authority,
            1_700_043_200_000,
        )


def test_response_envelope_and_frame_reject_malformed_fields(golden: dict[str, str]) -> None:
    response = bytearray.fromhex(golden["response"])
    envelope = bytearray.fromhex(golden["envelope"])
    frame = bytearray.fromhex(golden["frame"])

    malformed_response = (
        bytes(response[:7]),
        bytes([response[0] ^ 1]) + bytes(response[1:]),
        bytes(response) + b"\x00",
        bytes(response[:40]) + b"\x00\x00\x00\x02" + bytes(response[44:]),
        bytes(response[:-4]) + b"\x00\x00\x00\x03",
    )
    malformed_envelope = (
        bytes(envelope[:19]),
        bytes([envelope[0] ^ 1]) + bytes(envelope[1:]),
        bytes(envelope) + b"\x00",
        bytes(envelope[:12]) + b"\x00\x00\x00\x00" + bytes(envelope[16:]),
    )
    malformed_frame = (
        bytes(frame[:21]),
        bytes([frame[0] ^ 1]) + bytes(frame[1:]),
        bytes(frame) + b"\x00",
        bytes(frame[:12]) + b"\x00\x00\x00\x00" + bytes(frame[16:]),
    )

    for encoded in malformed_response:
        with pytest.raises(ProtocolError):
            ReputationResponse.decode(encoded)
    for encoded in malformed_envelope:
        with pytest.raises(ProtocolError):
            SignatureEnvelope.decode(encoded)
    for encoded in malformed_frame:
        with pytest.raises(ProtocolError):
            TransportFrame.decode(encoded)


def test_verifier_rejects_developer_identity_mismatch(golden: dict[str, str]) -> None:
    query = ReputationQuery.decode(bytes.fromhex(golden["query"]))

    with pytest.raises(VerificationError, match="developer identity"):
        verify_signed_record(
            ReputationQuery(query.archive_sha256, "p256:" + "c" * 64),
            bytes.fromhex(golden["response"]),
            bytes.fromhex(golden["envelope"]),
            golden["authority_key_id"],
            1_700_043_200_000,
        )


def _sign_response(
    response: ReputationResponse,
    private_key: ec.EllipticCurvePrivateKey,
    public_key: bytes,
) -> bytes:
    message = canonical_message(response, 1, public_key)
    signature = private_key.sign(message, ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(signature)
    if s > P256_ORDER // 2:
        s = P256_ORDER - s

    return SignatureEnvelope(1, public_key, encode_dss_signature(r, s)).encode()
