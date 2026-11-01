import pytest
from cryptography.hazmat.primitives.asymmetric.utils import (
    decode_dss_signature,
    encode_dss_signature,
)

from fluxgram_backend.reputation.signing import (
    AuthoritySignature,
    AuthoritySigningError,
    SignatureEncoding,
    normalize_p256_signature,
)
from fluxgram_backend.reputation.verification import P256_ORDER


@pytest.mark.parametrize("encoding", [SignatureEncoding.DER, SignatureEncoding.P1363])
def test_signature_normalization_accepts_provider_formats_and_enforces_low_s(
    encoding: SignatureEncoding,
) -> None:
    r = 7
    high_s = P256_ORDER - 11
    if encoding is SignatureEncoding.DER:
        value = encode_dss_signature(r, high_s)
    else:
        value = r.to_bytes(32, "big") + high_s.to_bytes(32, "big")

    normalized = normalize_p256_signature(AuthoritySignature(value, encoding))

    assert decode_dss_signature(normalized) == (r, 11)


@pytest.mark.parametrize(
    ("value", "encoding", "message"),
    [
        (b"", SignatureEncoding.DER, "malformed DER"),
        (b"\x00" * 129, SignatureEncoding.DER, "malformed DER"),
        (b"\x00" * 63, SignatureEncoding.P1363, "malformed P1363"),
    ],
)
def test_authority_signature_rejects_malformed_lengths(
    value: bytes,
    encoding: SignatureEncoding,
    message: str,
) -> None:
    with pytest.raises(AuthoritySigningError, match=message):
        AuthoritySignature(value, encoding)


def test_signature_normalization_rejects_out_of_range_values() -> None:
    signature = AuthoritySignature(encode_dss_signature(0, 1), SignatureEncoding.DER)

    with pytest.raises(AuthoritySigningError, match="out-of-range"):
        normalize_p256_signature(signature)
