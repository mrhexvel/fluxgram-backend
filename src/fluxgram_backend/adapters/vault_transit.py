import base64
import json
import re
import unicodedata
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlsplit

import httpx
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from fluxgram_backend.reputation.signing import AuthoritySignature, SignatureEncoding

MAX_VAULT_RESPONSE_BYTES = 64 * 1024
MAX_VAULT_TOKEN_LENGTH = 8192
NAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
NAMESPACE_PATTERN = re.compile(r"^[A-Za-z0-9_./-]{1,256}$")


class VaultTransitError(RuntimeError):
    pass


class VaultTransitAuthoritySigner:
    def __init__(
        self,
        client: httpx.AsyncClient,
        address: str,
        mount: str,
        key_name: str,
        key_version: int,
        token_provider: Callable[[], Awaitable[str]],
        namespace: str | None = None,
    ) -> None:
        if client.follow_redirects:
            raise ValueError("Vault HTTP client must not follow redirects")
        if not NAME_PATTERN.fullmatch(mount):
            raise ValueError("Vault Transit mount is invalid")
        if not NAME_PATTERN.fullmatch(key_name):
            raise ValueError("Vault Transit key name is invalid")
        if key_version <= 0 or key_version > 2_147_483_647:
            raise ValueError("Vault Transit key version is invalid")
        if namespace is not None and (
            not NAMESPACE_PATTERN.fullmatch(namespace)
            or ".." in namespace.split("/")
        ):
            raise ValueError("Vault namespace is invalid")

        self._client = client
        self._address = _validate_address(address)
        self._mount = mount
        self._key_name = key_name
        self._key_version = key_version
        self._token_provider = token_provider
        self._namespace = namespace

    async def get_public_key(self) -> bytes:
        payload = await self._request("GET", f"keys/{self._key_name}")
        data = _require_object(payload, "data")
        if data.get("type") != "ecdsa-p256" or data.get("supports_signing") is not True:
            raise VaultTransitError("Vault Transit key is not an ECDSA P-256 signing key")

        keys = _require_object(data, "keys")
        version = _require_object(keys, str(self._key_version))
        public_key_pem = version.get("public_key")
        if not isinstance(public_key_pem, str) or not 1 <= len(public_key_pem) <= 4096:
            raise VaultTransitError("Vault Transit public key is missing")

        try:
            public_key = serialization.load_pem_public_key(public_key_pem.encode("ascii"))
        except (UnicodeEncodeError, TypeError, ValueError, UnsupportedAlgorithm) as error:
            raise VaultTransitError("Vault Transit public key is malformed") from error
        if not isinstance(public_key, ec.EllipticCurvePublicKey) or not isinstance(
            public_key.curve,
            ec.SECP256R1,
        ):
            raise VaultTransitError("Vault Transit public key is not P-256")

        return public_key.public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )

    async def sign_sha256(self, digest: bytes) -> AuthoritySignature:
        if not isinstance(digest, bytes) or len(digest) != 32:
            raise VaultTransitError("Vault Transit requires a 32-byte SHA-256 digest")

        payload = await self._request(
            "POST",
            f"sign/{self._key_name}/sha2-256",
            {
                "input": base64.b64encode(digest).decode("ascii"),
                "prehashed": True,
                "key_version": self._key_version,
                "marshaling_algorithm": "asn1",
            },
        )
        data = _require_object(payload, "data")
        encoded_signature = data.get("signature")
        if not isinstance(encoded_signature, str) or len(encoded_signature) > 512:
            raise VaultTransitError("Vault Transit signature is missing")

        parts = encoded_signature.split(":")
        expected_version = f"v{self._key_version}"
        if len(parts) != 3 or parts[0] != "vault" or parts[1] != expected_version:
            raise VaultTransitError("Vault Transit signature version is invalid")

        try:
            signature = base64.b64decode(parts[2], validate=True)
        except ValueError as error:
            raise VaultTransitError("Vault Transit signature is malformed") from error

        return AuthoritySignature(signature, SignatureEncoding.DER)

    async def _request(
        self,
        method: str,
        endpoint: str,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        token = await self._token_provider()
        if (
            not isinstance(token, str)
            or not 1 <= len(token) <= MAX_VAULT_TOKEN_LENGTH
            or any(character in token for character in "\r\n\x00")
        ):
            raise VaultTransitError("Vault token is invalid")

        headers = {
            "X-Vault-Token": token,
            "Accept": "application/json",
            "Accept-Encoding": "identity",
        }
        if self._namespace is not None:
            headers["X-Vault-Namespace"] = self._namespace

        url = f"{self._address}/v1/{self._mount}/{endpoint}"
        try:
            async with self._client.stream(
                method,
                url,
                headers=headers,
                json=body,
                follow_redirects=False,
            ) as response:
                if response.status_code != 200:
                    raise VaultTransitError(
                        f"Vault Transit request failed with status {response.status_code}"
                    )
                content_type = response.headers.get("content-type", "").split(";", 1)[0]
                if content_type.strip().lower() != "application/json":
                    raise VaultTransitError("Vault Transit response is not JSON")
                content_encoding = response.headers.get("content-encoding", "identity").lower()
                if content_encoding != "identity":
                    raise VaultTransitError("Vault Transit response encoding is not supported")
                content_length = response.headers.get("content-length")
                if content_length is not None:
                    try:
                        declared_length = int(content_length)
                    except ValueError as error:
                        raise VaultTransitError(
                            "Vault Transit response length is invalid"
                        ) from error
                    if declared_length < 0 or declared_length > MAX_VAULT_RESPONSE_BYTES:
                        raise VaultTransitError("Vault Transit response exceeds size limit")

                encoded = bytearray()
                async for chunk in response.aiter_raw():
                    if len(encoded) + len(chunk) > MAX_VAULT_RESPONSE_BYTES:
                        raise VaultTransitError("Vault Transit response exceeds size limit")
                    encoded.extend(chunk)
        except httpx.HTTPError as error:
            raise VaultTransitError("Vault Transit request failed") from error

        try:
            payload = json.loads(encoded)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise VaultTransitError("Vault Transit response is malformed") from error
        if not isinstance(payload, dict):
            raise VaultTransitError("Vault Transit response must be an object")

        return payload


def _validate_address(address: str) -> str:
    if address != address.strip() or any(
        unicodedata.category(character) == "Cc" for character in address
    ):
        raise ValueError("Vault address must be an origin")

    parsed = urlsplit(address)
    try:
        _ = parsed.port
    except ValueError as error:
        raise ValueError("Vault address port is invalid") from error
    if (
        parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in ("", "/")
        or parsed.hostname is None
    ):
        raise ValueError("Vault address must be an origin")

    loopback = parsed.hostname in ("127.0.0.1", "::1", "localhost")
    if parsed.scheme != "https" and not (parsed.scheme == "http" and loopback):
        raise ValueError("Vault address must use HTTPS")

    return address.rstrip("/")


def _require_object(value: dict[str, Any], field: str) -> dict[str, Any]:
    nested = value.get(field)
    if not isinstance(nested, dict):
        raise VaultTransitError(f"Vault Transit response field {field} is invalid")

    return nested
