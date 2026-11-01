import hashlib
import struct

from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import (
    decode_dss_signature,
    encode_dss_signature,
)

from fluxgram_backend.reputation.protocol import (
    ALGORITHM_ECDSA_P256_SHA256,
    P256_KEY_PREFIX,
    ArchiveStatus,
    KeyStatus,
    ReputationQuery,
    ReputationResponse,
    SignatureEnvelope,
)

SIGNED_MESSAGE_MAGIC = 0x46475253
SIGNED_MESSAGE_VERSION = 1
SIGNED_MESSAGE_DOMAIN = b"fluxgram.plugin.reputation.response"
P256_ORDER = int("ffffffff00000000ffffffffffffffffbce6faada7179e84f3b9cac2fc632551", 16)


class VerificationError(ValueError):
    pass


def verify_signed_record(
    query: ReputationQuery,
    encoded_response: bytes,
    encoded_envelope: bytes,
    authority_key_id: str,
    now_millis: int,
) -> None:
    response = ReputationResponse.decode(encoded_response)
    envelope = SignatureEnvelope.decode(encoded_envelope)

    if envelope.algorithm != ALGORITHM_ECDSA_P256_SHA256:
        raise VerificationError("Unsupported reputation signature algorithm")

    public_key = _load_public_key(envelope.public_key)
    key_id = P256_KEY_PREFIX + hashlib.sha256(envelope.public_key).hexdigest()
    if key_id != authority_key_id:
        raise VerificationError("Unexpected reputation authority")

    _require_strict_low_s(envelope.signature)
    message = canonical_message(response, envelope.algorithm, envelope.public_key)

    try:
        public_key.verify(envelope.signature, message, ec.ECDSA(hashes.SHA256()))
    except InvalidSignature as error:
        raise VerificationError("Invalid reputation signature") from error

    if response.archive_sha256 != query.archive_sha256:
        raise VerificationError("Reputation archive identity mismatch")
    if response.developer_key_id != query.developer_key_id:
        raise VerificationError("Reputation developer identity mismatch")
    if now_millis < response.issued_at_millis:
        raise VerificationError("Reputation response is not current")
    if now_millis >= response.expires_at_millis and not _is_blocking(response):
        raise VerificationError("Clear reputation response is expired")


def canonical_message(
    response: ReputationResponse,
    algorithm: int,
    public_key: bytes,
) -> bytes:
    encoded_response = response.encode()
    return b"".join(
        (
            struct.pack(
                ">III",
                SIGNED_MESSAGE_MAGIC,
                SIGNED_MESSAGE_VERSION,
                len(SIGNED_MESSAGE_DOMAIN),
            ),
            SIGNED_MESSAGE_DOMAIN,
            struct.pack(">II", algorithm, len(public_key)),
            public_key,
            struct.pack(">I", len(encoded_response)),
            encoded_response,
        )
    )


def _load_public_key(encoded: bytes) -> ec.EllipticCurvePublicKey:
    try:
        public_key = serialization.load_der_public_key(encoded)
    except (TypeError, ValueError, UnsupportedAlgorithm) as error:
        raise VerificationError("Malformed reputation public key") from error

    if not isinstance(public_key, ec.EllipticCurvePublicKey):
        raise VerificationError("Reputation authority is not an EC key")
    if not isinstance(public_key.curve, ec.SECP256R1):
        raise VerificationError("Reputation authority is not P-256")

    canonical = public_key.public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    if canonical != encoded:
        raise VerificationError("Reputation public key is not canonical")

    return public_key


def _require_strict_low_s(signature: bytes) -> None:
    try:
        r, s = decode_dss_signature(signature)
    except ValueError as error:
        raise VerificationError("Malformed reputation signature") from error

    if encode_dss_signature(r, s) != signature:
        raise VerificationError("Reputation signature is not strict DER")
    if r <= 0 or r >= P256_ORDER or s <= 0 or s > P256_ORDER // 2:
        raise VerificationError("Reputation signature is not canonical low-S")


def _is_blocking(response: ReputationResponse) -> bool:
    return (
        response.archive_status is ArchiveStatus.MALICIOUS
        or response.key_status is KeyStatus.REVOKED
    )
