from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from fluxgram_backend.publisher.settings import (
    PluginReputationProcessSettings,
    PluginReputationPublisherSettings,
    PluginReputationWorkerSettings,
)

AUTHORITY_KEY_ID = "p256:" + "a" * 64


def _valid_settings() -> dict[str, object]:
    return {
        "plugin_reputation_authority_key_id": AUTHORITY_KEY_ID,
        "vault_address": "https://vault.example:8200",
        "vault_transit_mount": "transit",
        "vault_transit_key_name": "plugin-reputation",
        "vault_transit_key_version": 3,
        "vault_agent_token_file": "/run/vault/token",
        "vault_ca_file": "/run/vault/ca.pem",
    }


def test_publisher_settings_load_vault_agent_configuration() -> None:
    settings = PluginReputationPublisherSettings.model_validate(
        {**_valid_settings(), "vault_namespace": "fluxgram/production/"}
    )

    assert settings.vault_address == "https://vault.example:8200"
    assert settings.vault_agent_token_file == Path("/run/vault/token")
    assert settings.vault_namespace == "fluxgram/production/"
    assert "vault_token" not in type(settings).model_fields


def test_publisher_settings_load_environment_without_static_token(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    for field, value in _valid_settings().items():
        monkeypatch.setenv(field.upper(), str(value))
    monkeypatch.setenv("VAULT_TOKEN", "must-not-be-loaded")

    settings_type: Any = PluginReputationPublisherSettings
    settings = settings_type()

    assert settings.plugin_reputation_authority_key_id == AUTHORITY_KEY_ID
    assert settings.vault_agent_token_file == Path("/run/vault/token")
    assert "must-not-be-loaded" not in repr(settings)


@pytest.mark.parametrize(
    "address",
    [
        "http://vault.example",
        "http://127.0.0.1:8200",
        "https://user@vault.example",
        "https://vault.example/path",
        "https://vault.example?query=1",
        "https://vault.example#fragment",
        "https://vault.example:70000",
        "https://vault.example\x7f",
    ],
)
def test_publisher_settings_reject_non_production_vault_address(address: str) -> None:
    with pytest.raises(ValidationError):
        PluginReputationPublisherSettings.model_validate(
            {**_valid_settings(), "vault_address": address}
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("plugin_reputation_authority_key_id", "p256:ABC"),
        ("vault_transit_mount", "nested/path"),
        ("vault_transit_key_name", "contains space"),
        ("vault_transit_key_version", 0),
        ("vault_transit_key_version", 2_147_483_648),
        ("vault_namespace", "parent/../child"),
        ("vault_agent_token_file", "relative/token"),
        ("vault_ca_file", "relative/ca.pem"),
        ("vault_signer_timeout_seconds", 0),
        ("vault_signer_timeout_seconds", 61),
    ],
)
def test_publisher_settings_reject_invalid_configuration(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        PluginReputationPublisherSettings.model_validate({**_valid_settings(), field: value})


def test_worker_settings_validate_bounded_intervals() -> None:
    settings = PluginReputationWorkerSettings.model_validate(
        {"plugin_reputation_worker_id": "publisher-1"}
    )

    assert settings.plugin_reputation_worker_lease_seconds == 30
    assert settings.plugin_reputation_worker_max_attempts == 10

    with pytest.raises(ValidationError, match="half the lease"):
        PluginReputationWorkerSettings.model_validate(
            {
                "plugin_reputation_worker_id": "publisher-1",
                "plugin_reputation_worker_lease_seconds": 10,
                "plugin_reputation_worker_renewal_seconds": 10,
            }
        )


def test_process_settings_read_database_url_from_private_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL_FILE", raising=False)
    database_file = tmp_path / "database-url"
    database_file.write_text("postgresql+asyncpg://user:secret@db/fluxgram\n", encoding="utf-8")
    database_file.chmod(0o400)
    settings = PluginReputationProcessSettings.model_validate(
        {"database_url_file": database_file}
    )

    assert settings.resolve_database_url() == "postgresql+asyncpg://user:secret@db/fluxgram"
    assert "secret" not in repr(settings)


def test_process_settings_require_exactly_one_database_url_source(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL_FILE", raising=False)
    with pytest.raises(ValidationError, match="exactly one"):
        PluginReputationProcessSettings.model_validate({})
    with pytest.raises(ValidationError, match="exactly one"):
        PluginReputationProcessSettings.model_validate(
            {
                "database_url": "postgresql+asyncpg://user:secret@db/fluxgram",
                "database_url_file": tmp_path / "database-url",
            }
        )


@pytest.mark.parametrize("mode", (0o440, 0o600, 0o644))
def test_process_settings_reject_unsafe_database_url_file_mode(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    mode: int,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL_FILE", raising=False)
    database_file = tmp_path / "database-url"
    database_file.write_text("postgresql+asyncpg://user:secret@db/fluxgram", encoding="utf-8")
    database_file.chmod(mode)
    settings = PluginReputationProcessSettings.model_validate(
        {"database_url_file": database_file}
    )

    with pytest.raises(RuntimeError, match="database URL file is invalid"):
        settings.resolve_database_url()
    with pytest.raises(ValidationError, match="initial retry"):
        PluginReputationWorkerSettings.model_validate(
            {
                "plugin_reputation_worker_id": "publisher-1",
                "plugin_reputation_worker_retry_initial_seconds": 10,
                "plugin_reputation_worker_retry_max_seconds": 1,
            }
        )
