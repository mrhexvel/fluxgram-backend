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
