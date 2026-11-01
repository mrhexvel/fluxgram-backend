from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from cryptography.hazmat.primitives.asymmetric.utils import (
    decode_dss_signature,
    encode_dss_signature,
)

from fluxgram_backend.reputation.verification import P256_ORDER


class AuthoritySigningError(ValueError):
    pass


class AuthoritySigningUnavailable(AuthoritySigningError):
    pass


class SignatureEncoding(StrEnum):
    DER = "der"
    P1363 = "p1363"


@dataclass(frozen=True, slots=True)
class AuthoritySignature:
    value: bytes
    encoding: SignatureEncoding

    def __post_init__(self) -> None:
        if not isinstance(self.value, bytes):
            raise AuthoritySigningError("Authority signature must be bytes")
        if self.encoding is SignatureEncoding.DER and not 1 <= len(self.value) <= 128:
            raise AuthoritySigningError("Authority returned malformed DER signature")
        if self.encoding is SignatureEncoding.P1363 and len(self.value) != 64:
            raise AuthoritySigningError("Authority returned malformed P1363 signature")
        if self.encoding not in (SignatureEncoding.DER, SignatureEncoding.P1363):
            raise AuthoritySigningError("Authority returned unsupported signature encoding")


class AuthoritySigner(Protocol):
    async def get_public_key(self) -> bytes: ...

    async def sign_sha256(self, digest: bytes) -> AuthoritySignature: ...


def normalize_p256_signature(signature: AuthoritySignature) -> bytes:
    if signature.encoding is SignatureEncoding.DER:
        try:
            r, s = decode_dss_signature(signature.value)
        except ValueError as error:
            raise AuthoritySigningError("Authority returned malformed DER signature") from error
    elif signature.encoding is SignatureEncoding.P1363:
        r = int.from_bytes(signature.value[:32], "big")
        s = int.from_bytes(signature.value[32:], "big")
    else:
        raise AuthoritySigningError("Authority returned unsupported signature encoding")

    if r <= 0 or r >= P256_ORDER or s <= 0 or s >= P256_ORDER:
        raise AuthoritySigningError("Authority returned out-of-range P-256 signature")

    if s > P256_ORDER // 2:
        s = P256_ORDER - s

    return encode_dss_signature(r, s)
