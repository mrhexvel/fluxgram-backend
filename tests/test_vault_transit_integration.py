import asyncio
import os
from uuid import uuid4

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import Prehashed

from fluxgram_backend.adapters.vault_transit import VaultTransitAuthoritySigner


@pytest.mark.integration
def test_vault_transit_signs_prehashed_digest_with_fixed_p256_key_version() -> None:
    address = os.environ.get("TEST_VAULT_ADDR")
    token = os.environ.get("TEST_VAULT_TOKEN")
    if address is None or token is None:
        pytest.skip("TEST_VAULT_ADDR and TEST_VAULT_TOKEN are not configured")

    mount = f"fluxgram-{uuid4().hex}"
    key_name = "reputation"
    digest = bytes(range(32))

    async def check() -> None:
        async with httpx.AsyncClient(timeout=5, follow_redirects=False) as client:
            headers = {"X-Vault-Token": token}
            mount_response = await client.post(
                f"{address}/v1/sys/mounts/{mount}",
                headers=headers,
                json={"type": "transit"},
            )
            assert mount_response.status_code == 204

            try:
                key_response = await client.post(
                    f"{address}/v1/{mount}/keys/{key_name}",
                    headers=headers,
                    json={"type": "ecdsa-p256"},
                )
                assert key_response.status_code == 200
                rotate_response = await client.post(
                    f"{address}/v1/{mount}/keys/{key_name}/rotate",
                    headers=headers,
                )
                assert rotate_response.status_code == 200

                async def token_provider() -> str:
                    return token

                signer = VaultTransitAuthoritySigner(
                    client,
                    address,
                    mount,
                    key_name,
                    1,
                    token_provider,
                )
                public_key_bytes = await signer.get_public_key()
                signature = await signer.sign_sha256(digest)

                key_details = await client.get(
                    f"{address}/v1/{mount}/keys/{key_name}",
                    headers=headers,
                )
                assert key_details.status_code == 200
                assert key_details.json()["data"]["latest_version"] == 2

                public_key = serialization.load_der_public_key(public_key_bytes)
                assert isinstance(public_key, ec.EllipticCurvePublicKey)
                public_key.verify(
                    signature.value,
                    digest,
                    ec.ECDSA(Prehashed(hashes.SHA256())),
                )
                assert signature.value
            finally:
                delete_response = await client.delete(
                    f"{address}/v1/sys/mounts/{mount}",
                    headers=headers,
                )
                assert delete_response.status_code == 204

    asyncio.run(check())
