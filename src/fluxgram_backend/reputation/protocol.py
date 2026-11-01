import struct
from dataclasses import dataclass
from enum import IntEnum

QUERY_MAGIC = 0x46475251
RESPONSE_MAGIC = 0x46475252
ENVELOPE_MAGIC = 0x46475345
FRAME_MAGIC = 0x46475254
FORMAT_VERSION = 1
ALGORITHM_ECDSA_P256_SHA256 = 1
P256_KEY_PREFIX = "p256:"
MAX_QUERY_BYTES = 76
MAX_RESPONSE_BYTES = 128
MAX_ENVELOPE_BYTES = 512
MAX_FRAME_BYTES = 660
MAX_VALIDITY_MILLIS = 30 * 24 * 60 * 60 * 1000
MAX_SIGNED_LONG = (1 << 63) - 1


class ProtocolError(ValueError):
    pass


class ArchiveStatus(IntEnum):
    CLEAR = 1
    MALICIOUS = 2


class KeyStatus(IntEnum):
    NOT_APPLICABLE = 0
    CLEAR = 1
    REVOKED = 2


@dataclass(frozen=True, slots=True)
class ReputationQuery:
    archive_sha256: str
    developer_key_id: str | None = None

    def __post_init__(self) -> None:
        _require_sha256(self.archive_sha256)

        if self.developer_key_id is not None:
            _require_key_id(self.developer_key_id)

    def encode(self) -> bytes:
        flags = 1 if self.developer_key_id is not None else 0
        encoded = struct.pack(
            ">III32s",
            QUERY_MAGIC,
            FORMAT_VERSION,
            flags,
            bytes.fromhex(self.archive_sha256),
        )

        if self.developer_key_id is not None:
            encoded += bytes.fromhex(self.developer_key_id.removeprefix(P256_KEY_PREFIX))

        return encoded

    @classmethod
    def decode(cls, encoded: bytes) -> "ReputationQuery":
        if len(encoded) not in (44, MAX_QUERY_BYTES):
            raise ProtocolError("Invalid reputation query size")

        magic, version, flags = struct.unpack_from(">III", encoded)
        if magic != QUERY_MAGIC or version != FORMAT_VERSION or flags not in (0, 1):
            raise ProtocolError("Invalid reputation query header")

        expected_size = MAX_QUERY_BYTES if flags == 1 else 44
        if len(encoded) != expected_size:
            raise ProtocolError("Invalid reputation query fields")

        archive_sha256 = encoded[12:44].hex()
        developer_key_id = None
        if flags == 1:
            developer_key_id = P256_KEY_PREFIX + encoded[44:76].hex()

        return cls(archive_sha256, developer_key_id)


@dataclass(frozen=True, slots=True)
class ReputationResponse:
    archive_sha256: str
    developer_key_id: str | None
    revision: int
    issued_at_millis: int
    expires_at_millis: int
    archive_status: ArchiveStatus
    key_status: KeyStatus

    def __post_init__(self) -> None:
        _require_sha256(self.archive_sha256)

        if self.developer_key_id is None:
            if self.key_status is not KeyStatus.NOT_APPLICABLE:
                raise ProtocolError("Key status requires a developer key")
        else:
            _require_key_id(self.developer_key_id)
            if self.key_status is KeyStatus.NOT_APPLICABLE:
                raise ProtocolError("Developer key requires a key status")

        for value in (self.revision, self.issued_at_millis, self.expires_at_millis):
            if value < 0 or value > MAX_SIGNED_LONG:
                raise ProtocolError("Reputation response integer is out of range")

        validity = self.expires_at_millis - self.issued_at_millis
        if validity <= 0 or validity > MAX_VALIDITY_MILLIS:
            raise ProtocolError("Invalid reputation response validity")

    def encode(self) -> bytes:
        key_kind = 0 if self.developer_key_id is None else 1
        encoded = struct.pack(
            ">II32sI",
            RESPONSE_MAGIC,
            FORMAT_VERSION,
            bytes.fromhex(self.archive_sha256),
            key_kind,
        )

        if self.developer_key_id is not None:
            encoded += bytes.fromhex(self.developer_key_id.removeprefix(P256_KEY_PREFIX))

        encoded += struct.pack(
            ">qqqII",
            self.revision,
            self.issued_at_millis,
            self.expires_at_millis,
            int(self.archive_status),
            int(self.key_status),
        )
        if len(encoded) > MAX_RESPONSE_BYTES:
            raise ProtocolError("Reputation response exceeds size limit")

        return encoded

    @classmethod
    def decode(cls, encoded: bytes) -> "ReputationResponse":
        if len(encoded) not in (76, 108) or len(encoded) > MAX_RESPONSE_BYTES:
            raise ProtocolError("Invalid reputation response size")

        magic, version = struct.unpack_from(">II", encoded)
        if magic != RESPONSE_MAGIC or version != FORMAT_VERSION:
            raise ProtocolError("Invalid reputation response header")

        archive_sha256 = encoded[8:40].hex()
        key_kind = struct.unpack_from(">I", encoded, 40)[0]
        offset = 44
        developer_key_id = None

        if key_kind == 1:
            developer_key_id = P256_KEY_PREFIX + encoded[offset : offset + 32].hex()
            offset += 32
        elif key_kind != 0:
            raise ProtocolError("Unknown reputation key kind")

        if len(encoded) != offset + 32:
            raise ProtocolError("Invalid reputation response fields")

        revision, issued_at, expires_at, archive_code, key_code = struct.unpack_from(
            ">qqqII",
            encoded,
            offset,
        )

        try:
            archive_status = ArchiveStatus(archive_code)
            key_status = KeyStatus(key_code)
        except ValueError as error:
            raise ProtocolError("Unknown reputation status") from error

        return cls(
            archive_sha256,
            developer_key_id,
            revision,
            issued_at,
            expires_at,
            archive_status,
            key_status,
        )


@dataclass(frozen=True, slots=True)
class SignatureEnvelope:
    algorithm: int
    public_key: bytes
    signature: bytes

    def __post_init__(self) -> None:
        if self.algorithm <= 0:
            raise ProtocolError("Invalid signature algorithm")
        if not 1 <= len(self.public_key) <= 256:
            raise ProtocolError("Invalid signature public key")
        if not 1 <= len(self.signature) <= 128:
            raise ProtocolError("Invalid signature")

    def encode(self) -> bytes:
        encoded = struct.pack(
            ">IIIII",
            ENVELOPE_MAGIC,
            FORMAT_VERSION,
            self.algorithm,
            len(self.public_key),
            len(self.signature),
        )
        encoded = encoded[:16] + self.public_key + encoded[16:] + self.signature
        if len(encoded) > MAX_ENVELOPE_BYTES:
            raise ProtocolError("Signature envelope exceeds size limit")

        return encoded

    @classmethod
    def decode(cls, encoded: bytes) -> "SignatureEnvelope":
        if not encoded or len(encoded) > MAX_ENVELOPE_BYTES or len(encoded) < 20:
            raise ProtocolError("Invalid signature envelope size")

        magic, version, algorithm, key_length = struct.unpack_from(">IIII", encoded)
        if magic != ENVELOPE_MAGIC or version != FORMAT_VERSION:
            raise ProtocolError("Invalid signature envelope header")
        if not 1 <= key_length <= 256:
            raise ProtocolError("Invalid signature public key length")

        key_end = 16 + key_length
        if key_end + 4 > len(encoded):
            raise ProtocolError("Truncated signature envelope")

        signature_length = struct.unpack_from(">I", encoded, key_end)[0]
        signature_start = key_end + 4
        signature_end = signature_start + signature_length
        if not 1 <= signature_length <= 128 or signature_end != len(encoded):
            raise ProtocolError("Invalid signature envelope fields")

        return cls(
            algorithm,
            encoded[16:key_end],
            encoded[signature_start:signature_end],
        )


@dataclass(frozen=True, slots=True)
class TransportFrame:
    response: bytes
    envelope: bytes

    def __post_init__(self) -> None:
        if not 1 <= len(self.response) <= MAX_RESPONSE_BYTES:
            raise ProtocolError("Invalid transport response")
        if not 1 <= len(self.envelope) <= MAX_ENVELOPE_BYTES:
            raise ProtocolError("Invalid transport envelope")

    def encode(self) -> bytes:
        encoded = struct.pack(
            ">IIIII",
            FRAME_MAGIC,
            FORMAT_VERSION,
            0,
            len(self.response),
            len(self.envelope),
        )
        encoded += self.response + self.envelope
        if len(encoded) > MAX_FRAME_BYTES:
            raise ProtocolError("Transport frame exceeds size limit")

        return encoded

    @classmethod
    def decode(cls, encoded: bytes) -> "TransportFrame":
        if len(encoded) < 22 or len(encoded) > MAX_FRAME_BYTES:
            raise ProtocolError("Invalid transport frame size")

        magic, version, flags, response_length, envelope_length = struct.unpack_from(
            ">IIIII",
            encoded,
        )
        if magic != FRAME_MAGIC or version != FORMAT_VERSION or flags != 0:
            raise ProtocolError("Invalid transport frame header")
        if response_length <= 0 or envelope_length <= 0:
            raise ProtocolError("Invalid transport frame lengths")
        if response_length + envelope_length != len(encoded) - 20:
            raise ProtocolError("Invalid transport frame fields")

        response_end = 20 + response_length
        return cls(encoded[20:response_end], encoded[response_end:])


def _require_sha256(value: str) -> None:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ProtocolError("Invalid SHA-256")


def _require_key_id(value: str) -> None:
    if not value.startswith(P256_KEY_PREFIX):
        raise ProtocolError("Invalid P-256 key ID")

    _require_sha256(value.removeprefix(P256_KEY_PREFIX))
