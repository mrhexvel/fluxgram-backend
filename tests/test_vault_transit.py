import asyncio
import base64
import json

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import Prehashed

from fluxgram_backend.adapters.vault_transit import (
    MAX_VAULT_RESPONSE_BYTES,
    VaultTransitAuthoritySigner,
    VaultTransitError,
)
from fluxgram_backend.reputation.signing import SignatureEncoding


def test_vault_transit_signer_uses_fixed_key_version_and_prehashed_digest() -> None:
    private_key = ec.generate_private_key(ec.SECP256R1())
    public_key_pem = private_key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("ascii")
    digest = bytes(range(32))
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.headers["X-Vault-Token"] == "test-token"
        assert request.headers["X-Vault-Namespace"] == "team/fluxgram"

        if request.method == "GET":
            return _json_response(
                {
                    "data": {
                        "type": "ecdsa-p256",
                        "supports_signing": True,
                        "keys": {"3": {"public_key": public_key_pem}},
                    }
                }
            )

        body = json.loads(request.content)
        assert body == {
            "input": base64.b64encode(digest).decode("ascii"),
            "prehashed": True,
            "key_version": 3,
            "marshaling_algorithm": "asn1",
        }
        signature = private_key.sign(digest, ec.ECDSA(Prehashed(hashes.SHA256())))
        return _json_response(
            {"data": {"signature": f"vault:v3:{base64.b64encode(signature).decode()}"}}
        )

    async def check() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            signer = VaultTransitAuthoritySigner(
                client,
                "https://vault.example",
                "transit",
                "reputation",
                3,
                _test_token,
                namespace="team/fluxgram",
            )
            public_key = await signer.get_public_key()
            signature = await signer.sign_sha256(digest)

        loaded = serialization.load_der_public_key(public_key)
        assert isinstance(loaded, ec.EllipticCurvePublicKey)
        loaded.verify(signature.value, digest, ec.ECDSA(Prehashed(hashes.SHA256())))
        assert signature.encoding is SignatureEncoding.DER
        assert [request.url.path for request in requests] == [
            "/v1/transit/keys/reputation",
            "/v1/transit/sign/reputation/sha2-256",
        ]

    asyncio.run(check())


@pytest.mark.parametrize(
    "address",
    [
        "http://vault.example",
        "https://user@vault.example",
        "https://vault.example/path",
        "https://vault.example?query=1",
        "https://vault.example\x7f",
    ],
)
def test_vault_transit_signer_rejects_unsafe_addresses(address: str) -> None:
    async def check() -> None:
        async with httpx.AsyncClient() as client:
            with pytest.raises(ValueError, match="Vault address"):
                VaultTransitAuthoritySigner(
                    client,
                    address,
                    "transit",
                    "reputation",
                    1,
                    _test_token,
                )

    asyncio.run(check())


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (
            {"data": {"type": "rsa-2048", "supports_signing": True, "keys": {}}},
            "not an ECDSA P-256",
        ),
        (
            {
                "data": {
                    "type": "ecdsa-p256",
                    "supports_signing": True,
                    "keys": {"2": {"public_key": "invalid"}},
                }
            },
            "public key is malformed",
        ),
    ],
)
def test_vault_transit_signer_rejects_invalid_key_responses(
    response: dict[str, object],
    message: str,
) -> None:
    async def check() -> None:
        transport = httpx.MockTransport(lambda _: _json_response(response))
        async with httpx.AsyncClient(transport=transport) as client:
            signer = VaultTransitAuthoritySigner(
                client,
                "https://vault.example",
                "transit",
                "reputation",
                2,
                _test_token,
            )
            with pytest.raises(VaultTransitError, match=message):
                await signer.get_public_key()

    asyncio.run(check())


def test_vault_transit_signer_bounds_response_and_signature_version() -> None:
    responses = iter(
        (
            httpx.Response(
                200,
                headers={"content-type": "application/json"},
                stream=httpx.ByteStream(b"x" * (MAX_VAULT_RESPONSE_BYTES + 1)),
            ),
            _json_response({"data": {"signature": "vault:v2:AA=="}}),
        )
    )

    async def check() -> None:
        transport = httpx.MockTransport(lambda _: next(responses))
        async with httpx.AsyncClient(transport=transport) as client:
            signer = VaultTransitAuthoritySigner(
                client,
                "https://vault.example",
                "transit",
                "reputation",
                1,
                _test_token,
            )
            with pytest.raises(VaultTransitError, match="size limit"):
                await signer.get_public_key()
            with pytest.raises(VaultTransitError, match="version"):
                await signer.sign_sha256(b"d" * 32)

    asyncio.run(check())


def test_vault_transit_signer_does_not_follow_redirects() -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(307, headers={"location": "https://attacker.example/steal"})

    async def check() -> None:
        transport = httpx.MockTransport(handle)
        async with httpx.AsyncClient(transport=transport) as client:
            signer = VaultTransitAuthoritySigner(
                client,
                "https://vault.example",
                "transit",
                "reputation",
                1,
                _test_token,
            )
            client.follow_redirects = True

            with pytest.raises(VaultTransitError, match="status 307"):
                await signer.get_public_key()

        assert [request.url.host for request in requests] == ["vault.example"]

    asyncio.run(check())


def test_vault_transit_signer_rejects_encoded_response_before_decompression() -> None:
    async def check() -> None:
        transport = httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                headers={
                    "content-type": "application/json",
                    "content-encoding": "gzip",
                },
                stream=httpx.ByteStream(b"encoded"),
            )
        )
        async with httpx.AsyncClient(transport=transport) as client:
            signer = VaultTransitAuthoritySigner(
                client,
                "https://vault.example",
                "transit",
                "reputation",
                1,
                _test_token,
            )
            with pytest.raises(VaultTransitError, match="encoding"):
                await signer.get_public_key()

    asyncio.run(check())


async def _test_token() -> str:
    return "test-token"


def _json_response(payload: object) -> httpx.Response:
    return httpx.Response(
        200,
        headers={"content-type": "application/json"},
        stream=httpx.ByteStream(json.dumps(payload, separators=(",", ":")).encode()),
    )
