from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from fluxgram_backend.publisher.settings import PluginReputationPublisherSettings

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
