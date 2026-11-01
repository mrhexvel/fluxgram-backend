import asyncio
import base64
import os
import ssl
import stat
import subprocess
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import Prehashed

from fluxgram_backend.adapters.vault_agent_token import VaultAgentTokenFileProvider
from fluxgram_backend.adapters.vault_transit import VaultTransitAuthoritySigner


@pytest.mark.integration
def test_vault_agent_policy_allows_only_fixed_key_signing() -> None:
    address = os.environ.get("TEST_VAULT_AGENT_ADDR")
    token_path = os.environ.get("TEST_VAULT_AGENT_TOKEN_FILE")
    ca_path = os.environ.get("TEST_VAULT_AGENT_CA_FILE")
    wrapped_secret_path = os.environ.get("TEST_VAULT_AGENT_WRAPPED_SECRET_FILE")
    agent_container = os.environ.get("TEST_VAULT_AGENT_CONTAINER")
    issuer_token = os.environ.get("TEST_VAULT_SECRET_ID_ISSUER_TOKEN")
    admin_token = os.environ.get("TEST_VAULT_TOKEN")
    deployment_values = (
        address,
        token_path,
        ca_path,
        wrapped_secret_path,
        agent_container,
        issuer_token,
        admin_token,
    )
    if all(value is None for value in deployment_values):
        pytest.skip("Vault Agent deployment is not configured")
    if any(value is None for value in deployment_values):
        pytest.fail("Vault Agent deployment configuration is incomplete")

    assert address is not None
    assert token_path is not None
    assert ca_path is not None
    assert wrapped_secret_path is not None
    assert agent_container is not None
    assert issuer_token is not None
    assert admin_token is not None

    token_file = Path(token_path)
    metadata = token_file.stat()
    assert stat.S_ISREG(metadata.st_mode)
    assert stat.S_IMODE(metadata.st_mode) == 0o600
    assert metadata.st_nlink == 1
    assert not Path(wrapped_secret_path).exists()
    file_state = subprocess.run(
        [
            "docker",
            "exec",
            agent_container,
            "stat",
            "-c",
            "%u:%g:%a:%h",
            "/vault/secrets/token",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert file_state.stdout.strip() == "65534:65534:600:1"

    async def check() -> None:
        token_provider = VaultAgentTokenFileProvider(token_file)
        tls = ssl.create_default_context(cafile=ca_path)
        async with httpx.AsyncClient(
            verify=tls,
            trust_env=False,
            follow_redirects=False,
            timeout=5,
        ) as client:
            signer = VaultTransitAuthoritySigner(
                client,
                address,
                "transit",
                "plugin-reputation",
                1,
                token_provider,
            )
            digest = bytes(range(32))
            public_key_bytes = await signer.get_public_key()
            key_details = await client.get(
                f"{address}/v1/transit/keys/plugin-reputation",
                headers={"X-Vault-Token": await token_provider()},
            )
            assert key_details.status_code == 200
            assert key_details.json()["data"]["latest_version"] == 1
            signature = await signer.sign_sha256(digest)
            public_key = serialization.load_der_public_key(public_key_bytes)
            assert isinstance(public_key, ec.EllipticCurvePublicKey)
            public_key.verify(
                signature.value,
                digest,
                ec.ECDSA(Prehashed(hashes.SHA256())),
            )

            token = await token_provider()
            headers = {"X-Vault-Token": token}
            lookup = await client.get(
                f"{address}/v1/auth/token/lookup-self",
                headers=headers,
            )
            assert lookup.status_code == 200
            token_data = lookup.json()["data"]
            assert token_data["policies"] == ["plugin-reputation-publisher"]
            assert token_data.get("identity_policies") in (None, [])
            assert token_data["renewable"] is True
            assert token_data["period"] == 900
            renewal = await client.post(
                f"{address}/v1/auth/token/renew-self",
                headers=headers,
            )
            assert renewal.status_code == 200

            denied_requests = [
                client.request("LIST", f"{address}/v1/transit/keys", headers=headers),
                client.post(
                    f"{address}/v1/transit/keys/other",
                    headers=headers,
                    json={"type": "ecdsa-p256"},
                ),
                client.post(
                    f"{address}/v1/transit/keys/plugin-reputation/rotate",
                    headers=headers,
                ),
                client.post(
                    f"{address}/v1/transit/keys/plugin-reputation/config",
                    headers=headers,
                    json={"deletion_allowed": True},
                ),
                client.get(
                    f"{address}/v1/transit/backup/plugin-reputation",
                    headers=headers,
                ),
                client.get(
                    f"{address}/v1/transit/export/signing-key/plugin-reputation",
                    headers=headers,
                ),
                client.get(f"{address}/v1/sys/mounts", headers=headers),
                client.post(
                    f"{address}/v1/transit/sign/plugin-reputation/sha2-256",
                    headers=headers,
                    json={
                        "input": base64.b64encode(digest).decode("ascii"),
                        "prehashed": False,
                        "key_version": 1,
                        "marshaling_algorithm": "asn1",
                    },
                ),
            ]
            responses = await asyncio.gather(*denied_requests)
            assert all(response.status_code == 403 for response in responses)

            issuer_headers = {"X-Vault-Token": issuer_token}
            issuer_metadata = await client.post(
                f"{address}/v1/auth/token/lookup",
                headers={"X-Vault-Token": admin_token},
                json={"token": issuer_token},
            )
            assert issuer_metadata.status_code == 200
            issuer_token_data = issuer_metadata.json()["data"]
            assert issuer_token_data["policies"] == [
                "plugin-reputation-secret-id-issuer"
            ]
            assert issuer_token_data.get("identity_policies") in (None, [])
            assert issuer_token_data["renewable"] is False
            assert 0 < issuer_token_data["ttl"] <= 600

            role_id_response = await client.get(
                f"{address}/v1/auth/approle/role/plugin-reputation-publisher/role-id",
                headers=issuer_headers,
            )
            assert role_id_response.status_code == 200
            role_id = role_id_response.json()["data"]["role_id"]

            denied_issuer_requests = [
                client.post(
                    f"{address}/v1/auth/approle/role/plugin-reputation-publisher/secret-id",
                    headers=issuer_headers,
                ),
                client.post(
                    f"{address}/v1/auth/approle/role/plugin-reputation-publisher/secret-id",
                    headers={**issuer_headers, "X-Vault-Wrap-TTL": "500ms"},
                ),
                client.post(
                    f"{address}/v1/auth/approle/role/plugin-reputation-publisher/secret-id",
                    headers={**issuer_headers, "X-Vault-Wrap-TTL": "6m"},
                ),
                client.get(
                    f"{address}/v1/transit/keys/plugin-reputation",
                    headers=issuer_headers,
                ),
            ]
            denied_issuer_responses = await asyncio.gather(*denied_issuer_requests)
            assert all(response.status_code == 403 for response in denied_issuer_responses)

            wrapped_response = await client.post(
                f"{address}/v1/auth/approle/role/plugin-reputation-publisher/secret-id",
                headers={**issuer_headers, "X-Vault-Wrap-TTL": "5m"},
            )
            assert wrapped_response.status_code == 200
            wrap_info = wrapped_response.json()["wrap_info"]
            assert wrap_info["creation_path"] == (
                "auth/approle/role/plugin-reputation-publisher/secret-id"
            )
            assert wrap_info["ttl"] == 300
            wrapping_headers = {"X-Vault-Token": wrap_info["token"]}

            unwrap_response = await client.post(
                f"{address}/v1/sys/wrapping/unwrap",
                headers=wrapping_headers,
            )
            assert unwrap_response.status_code == 200
            secret_data = unwrap_response.json()["data"]
            assert secret_data["secret_id_num_uses"] == 1
            assert secret_data["secret_id_ttl"] == 600
            repeated_unwrap = await client.post(
                f"{address}/v1/sys/wrapping/unwrap",
                headers=wrapping_headers,
            )
            assert repeated_unwrap.status_code == 400

            login_payload = {
                "role_id": role_id,
                "secret_id": secret_data["secret_id"],
            }
            login_response = await client.post(
                f"{address}/v1/auth/approle/login",
                json=login_payload,
            )
            assert login_response.status_code == 200
            issued_auth = login_response.json()["auth"]
            try:
                assert issued_auth["policies"] == ["plugin-reputation-publisher"]
                repeated_login = await client.post(
                    f"{address}/v1/auth/approle/login",
                    json=login_payload,
                )
                assert repeated_login.status_code == 400
            finally:
                revoke_response = await client.post(
                    f"{address}/v1/auth/token/revoke",
                    headers={"X-Vault-Token": admin_token},
                    json={"token": issued_auth["client_token"]},
                )
                assert revoke_response.status_code == 204

    asyncio.run(check())
