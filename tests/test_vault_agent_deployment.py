import json
from pathlib import Path

import yaml

DEPLOY_ROOT = Path(__file__).parents[1] / "deploy" / "plugin-reputation"


def test_publisher_policy_is_scoped_to_one_transit_key() -> None:
    policy = (DEPLOY_ROOT / "vault" / "publisher-policy.hcl").read_text(encoding="ascii")

    assert 'path "transit/keys/plugin-reputation"' in policy
    assert 'path "transit/sign/plugin-reputation/sha2-256"' in policy
    assert '"prehashed" = [true, "true"]' in policy
    assert '"key_version",' in policy
    assert '"marshaling_algorithm",' in policy
    assert '"*"         = []' in policy
    assert 'path "auth/token/lookup-self"' in policy
    assert 'path "auth/token/renew-self"' in policy
    assert "transit/rotate" not in policy
    assert "transit/backup" not in policy
    assert "transit/export" not in policy
    assert "denied_parameters" not in policy
    assert '"create"' not in policy
    assert '"delete"' not in policy
    assert '"list"' not in policy


def test_vault_agent_uses_wrapped_approle_and_private_sink() -> None:
    config = (DEPLOY_ROOT / "vault" / "agent.hcl").read_text(encoding="ascii")

    assert 'address = "https://vault:8200"' in config
    assert 'ca_cert = "/vault/tls/vault-ca.pem"' in config
    assert 'method "approle"' in config
    assert 'mount_path = "auth/approle"' in config
    assert 'role_id_file_path                   = "/vault/bootstrap/role-id"' in config
    assert 'secret_id_file_path                 = "/vault/bootstrap/wrapped-secret-id"' in config
    assert (
        'secret_id_response_wrapping_path    = '
        '"auth/approle/role/plugin-reputation-publisher/secret-id"'
    ) in config
    assert "remove_secret_id_file_after_reading = true" in config
    assert 'path  = "/vault/secrets/token"' in config
    assert "mode  = 0600" in config
    assert "owner = 65534" in config
    assert "group = 65534" in config
    assert "listener" not in config
    assert "tls_skip_verify" not in config
    assert "token" not in config.replace("/vault/secrets/token", "")


def test_publisher_approle_and_secret_id_issuer_are_bounded() -> None:
    role = json.loads(
        (DEPLOY_ROOT / "vault" / "publisher-role.json").read_text(encoding="ascii")
    )
    issuer_policy = (
        DEPLOY_ROOT / "vault" / "secret-id-issuer-policy.hcl"
    ).read_text(encoding="ascii")

    assert role == {
        "bind_secret_id": True,
        "secret_id_num_uses": 1,
        "secret_id_ttl": "10m",
        "token_no_default_policy": True,
        "token_period": "15m",
        "token_policies": ["plugin-reputation-publisher"],
        "token_type": "service",
    }
    assert 'path "auth/approle/role/plugin-reputation-publisher/role-id"' in issuer_policy
    assert 'path "auth/approle/role/plugin-reputation-publisher/secret-id"' in issuer_policy
    assert 'min_wrapping_ttl = "1s"' in issuer_policy
    assert 'max_wrapping_ttl = "5m"' in issuer_policy
    assert "*" not in issuer_policy
    assert '"create"' not in issuer_policy
    assert '"delete"' not in issuer_policy
    assert '"list"' not in issuer_policy
    assert "transit" not in issuer_policy


def test_docker_context_excludes_local_environment() -> None:
    dockerignore = (DEPLOY_ROOT.parents[1] / ".dockerignore").read_text(encoding="ascii")

    assert ".env" in dockerignore.splitlines()
    assert "**/role-id" in dockerignore.splitlines()
    assert "**/wrapped-secret-id" in dockerignore.splitlines()
    assert "**/token" in dockerignore.splitlines()
    assert "**/vault-key.pem" in dockerignore.splitlines()


def test_vault_agent_compose_hardens_container_and_mounts() -> None:
    compose = yaml.safe_load(
        (DEPLOY_ROOT / "compose.vault-agent.yml").read_text(encoding="ascii")
    )
    service = compose["services"]["vault-agent"]

    assert service["image"] == (
        "hashicorp/vault:1.21.0@"
        "sha256:62dd55c9ccbdc0af0a9269e87481a64650258907434d5ddb5e795e2eb2ac5780"
    )
    assert service["entrypoint"] == ["/bin/sh", "-ec"]
    assert "65534:65534:700" in service["command"][0]
    assert "exec /bin/vault agent" in service["command"][0]
    assert service["user"] == "65534:65534"
    assert service["read_only"] is True
    assert service["cap_drop"] == ["ALL"]
    assert service["security_opt"] == ["no-new-privileges:true"]
    assert service["cpus"] == 0.5
    assert service["mem_limit"] == "128m"
    assert service["pids_limit"] == 64
    bind_mounts = [
        volume for volume in service["volumes"] if volume["type"] == "bind"
    ]
    assert {mount["target"] for mount in bind_mounts} == {
        "/vault/config/agent.hcl",
        "/vault/tls/vault-ca.pem",
        "/vault/bootstrap",
        "/vault/secrets",
    }
    assert all(mount["bind"]["create_host_path"] is False for mount in bind_mounts)
    read_only_targets = {
        mount["target"] for mount in bind_mounts if mount.get("read_only") is True
    }
    assert read_only_targets == {"/vault/config/agent.hcl", "/vault/tls/vault-ca.pem"}
    tmpfs_mounts = [
        volume for volume in service["volumes"] if volume["type"] == "tmpfs"
    ]
    assert {mount["target"] for mount in tmpfs_mounts} == {"/vault/file", "/vault/logs"}
    assert all(mount["read_only"] is True for mount in tmpfs_mounts)
    assert all(mount["tmpfs"]["mode"] == 0o555 for mount in tmpfs_mounts)
    assert "vault token lookup" in service["healthcheck"]["test"][1]


def test_production_publisher_uses_digest_image_private_database_file_and_bounded_health() -> None:
    compose = yaml.safe_load(
        (DEPLOY_ROOT / "compose.vault-agent.yml").read_text(encoding="ascii")
    )
    service = compose["services"]["publisher-worker"]

    assert service["image"].startswith("${PLUGIN_REPUTATION_PUBLISHER_IMAGE:?")
    assert service["command"] == ["python", "-m", "fluxgram_backend.publisher"]
    assert service["user"] == "65534:65534"
    assert service["init"] is True
    assert service["read_only"] is True
    assert service["cap_drop"] == ["ALL"]
    assert service["security_opt"] == ["no-new-privileges:true"]
    assert service["restart"] == "unless-stopped"
    assert service["stop_grace_period"] == "40s"
    assert "ports" not in service
    assert service["expose"] == ["8090"]
    assert service["environment"]["DATABASE_URL_FILE"] == "/run/secrets/database-url"
    assert "DATABASE_URL" not in service["environment"]
    assert "VAULT_TOKEN" not in service["environment"]
    assert service["environment"]["PLUGIN_REPUTATION_OPERATIONS_HOST"] == "0.0.0.0"
    assert service["depends_on"]["vault-agent"]["condition"] == "service_healthy"
    assert service["networks"] == ["database", "monitoring", "vault"]
    assert "PLUGIN_REPUTATION_OPERATIONS_ALLOWED_NETWORK" in service["environment"]
    assert service["healthcheck"]["test"][-1] == "/health/ready"
    assert service["logging"]["options"] == {"max-size": "10m", "max-file": "3"}

    mounts = service["volumes"]
    assert {mount["target"] for mount in mounts} == {
        "/run/secrets/database-url",
        "/vault/secrets",
        "/vault/tls/vault-ca.pem",
    }
    assert all(mount["read_only"] is True for mount in mounts)
    assert all(mount["bind"]["create_host_path"] is False for mount in mounts)
    assert not any("bootstrap" in mount["target"] for mount in mounts)


def test_production_public_api_has_database_and_ingress_only() -> None:
    compose = yaml.safe_load(
        (DEPLOY_ROOT / "compose.vault-agent.yml").read_text(encoding="ascii")
    )
    service = compose["services"]["public-api"]

    assert service["image"].startswith("${PLUGIN_REPUTATION_API_IMAGE:?")
    assert service["command"] == [
        "python",
        "-m",
        "fluxgram_backend",
        "reputation-api",
    ]
    assert service["user"] == "65534:65534"
    assert service["init"] is True
    assert service["read_only"] is True
    assert service["cap_drop"] == ["ALL"]
    assert service["security_opt"] == ["no-new-privileges:true"]
    assert service["restart"] == "unless-stopped"
    assert "ports" not in service
    assert service["expose"] == ["8080"]
    assert service["networks"] == ["database", "public"]
    assert "depends_on" not in service
    assert service["environment"]["DATABASE_URL_FILE"] == "/run/secrets/database-url"
    assert "DATABASE_URL" not in service["environment"]
    assert "VAULT_ADDRESS" not in service["environment"]
    assert "VAULT_TOKEN" not in service["environment"]
    assert "PLUGIN_REPUTATION_WORKER_ID" not in service["environment"]
    assert "REPUTATION_API_TRUSTED_PROXY_NETWORK" in service["environment"]

    mounts = service["volumes"]
    assert len(mounts) == 1
    assert mounts[0]["target"] == "/run/secrets/database-url"
    assert mounts[0]["read_only"] is True
    assert mounts[0]["bind"]["create_host_path"] is False


def test_local_compose_separates_worker_from_vault_admin_credentials() -> None:
    compose = yaml.safe_load(
        (DEPLOY_ROOT / "compose.local.yml").read_text(encoding="ascii")
    )
    services = compose["services"]

    assert set(services) == {
        "api-role-init",
        "migrate",
        "postgres",
        "public-api",
        "vault",
        "vault-agent",
        "vault-bootstrap",
        "vault-tls-init",
        "verify",
        "worker",
    }
    assert services["postgres"]["ports"] == [
        "127.0.0.1:${POSTGRES_PORT:-55432}:5432"
    ]
    assert services["vault"]["ports"] == [
        "127.0.0.1:${VAULT_PORT:-18200}:8200"
    ]
    assert services["vault"]["entrypoint"] == ["/bin/vault"]
    assert services["vault"]["user"] == "100:1000"
    assert services["vault"]["read_only"] is True
    assert services["vault"]["cap_drop"] == ["ALL"]
    for service_name in (
        "postgres",
        "public-api",
        "vault",
        "vault-agent",
        "vault-bootstrap",
        "worker",
    ):
        assert services[service_name]["restart"] == "unless-stopped"
    assert services["vault-agent"]["depends_on"]["vault-bootstrap"]["condition"] == (
        "service_healthy"
    )
    assert "wrapped-secret-id" in services["vault-bootstrap"]["healthcheck"]["test"][1]

    worker = services["worker"]
    assert worker["user"] == "65534:65534"
    assert worker["read_only"] is True
    assert worker["cap_drop"] == ["ALL"]
    assert worker["security_opt"] == ["no-new-privileges:true"]
    assert worker["environment"]["VAULT_AGENT_TOKEN_FILE"] == "/vault/secrets/token"
    assert worker["environment"]["VAULT_CA_FILE"] == "/vault/config/vault-ca.pem"
    assert "VAULT_TOKEN" not in worker["environment"]
    assert "authority-key-id" in worker["command"][0]
    worker_mounts = set(worker["volumes"])
    assert worker_mounts == {
        "local-vault-config:/vault/config:ro",
        "local-vault-secrets:/vault/secrets:ro",
    }
    assert not any("bootstrap" in mount for mount in worker_mounts)
    assert not any("admin" in mount for mount in worker_mounts)
    assert not any("vault-tls" in mount for mount in worker_mounts)

    public_api = services["public-api"]
    assert public_api["ports"] == ["127.0.0.1:${REPUTATION_API_PORT:-18080}:8080"]
    assert public_api["user"] == "65534:65534"
    assert public_api["read_only"] is True
    assert public_api["cap_drop"] == ["ALL"]
    assert public_api["security_opt"] == ["no-new-privileges:true"]
    assert public_api["volumes"] == ["local-vault-config:/vault/config:ro"]
    assert "VAULT_ADDRESS" not in public_api["environment"]
    assert "VAULT_TOKEN" not in public_api["environment"]
    assert "PLUGIN_REPUTATION_WORKER_ID" not in public_api["environment"]
    assert "reputation-api" in public_api["command"][0]
    assert "fluxgram_reputation_api:local-read-only" in public_api["environment"][
        "DATABASE_URL"
    ]

    role_init = services["api-role-init"]
    assert role_init["image"] == "postgres:18-bookworm"
    assert "GRANT SELECT ON TABLE plugin_reputation_records" in role_init["command"][0]
    assert "REVOKE ALL ON ALL TABLES" in role_init["command"][0]
    assert "REVOKE TEMPORARY ON DATABASE fluxgram FROM PUBLIC" in role_init["command"][0]
    assert "NOBYPASSRLS NOINHERIT" in role_init["command"][0]

    verifier = services["verify"]
    assert verifier["profiles"] == ["verify"]
    assert "depends_on" not in verifier
    assert verifier["volumes"][1] == "local-vault-config:/vault/config:ro"


def test_local_bootstrap_uses_tls_wrapped_approle_and_non_exportable_key() -> None:
    bootstrap = (DEPLOY_ROOT / "local" / "bootstrap.py").read_text(encoding="ascii")
    vault_config = (DEPLOY_ROOT / "local" / "vault.hcl").read_text(encoding="ascii")

    assert 'VAULT_ADDRESS = "https://vault:8200"' in bootstrap
    assert '"type": "ecdsa-p256"' in bootstrap
    assert '"exportable": False' in bootstrap
    assert '"allow_plaintext_backup": False' in bootstrap
    assert 'wrap_ttl="5m"' in bootstrap
    assert 'CONFIG_DIRECTORY / "vault-ca.pem"' in bootstrap
    assert 'CONFIG_DIRECTORY / "authority-key-id"' in bootstrap
    assert '(SECRETS_DIRECTORY, 65534, 65534)' in bootstrap
    assert 'BOOTSTRAP_DIRECTORY / "wrapped-secret-id"' in bootstrap
    assert "supervise_vault(root_token)" in bootstrap
    assert 'storage "file"' in vault_config
    assert 'tls_cert_file   = "/vault/tls/vault-cert.pem"' in vault_config
    assert 'tls_key_file    = "/vault/tls/vault-key.pem"' in vault_config
    assert "tls_disable" not in vault_config
