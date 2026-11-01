import re
import unicodedata
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from fluxgram_backend.adapters.vault_transit import NAME_PATTERN, NAMESPACE_PATTERN

AUTHORITY_KEY_ID_PATTERN = re.compile(r"^p256:[0-9a-f]{64}$")


class PluginReputationPublisherSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        extra="ignore",
    )

    plugin_reputation_authority_key_id: str
    vault_address: str
    vault_transit_mount: str
    vault_transit_key_name: str
    vault_transit_key_version: int = Field(ge=1, le=2_147_483_647)
    vault_agent_token_file: Path
    vault_ca_file: Path
    vault_namespace: str | None = None
    vault_signer_timeout_seconds: float = Field(default=10.0, gt=0, le=60)

    @field_validator("plugin_reputation_authority_key_id")
    @classmethod
    def validate_authority_key_id(cls, value: str) -> str:
        if AUTHORITY_KEY_ID_PATTERN.fullmatch(value) is None:
            raise ValueError("plugin reputation authority key ID is invalid")

        return value

    @field_validator("vault_address")
    @classmethod
    def validate_vault_address(cls, value: str) -> str:
        if value != value.strip() or any(
            unicodedata.category(character) == "Cc" for character in value
        ):
            raise ValueError("Vault address must be an HTTPS origin")

        parsed = urlsplit(value)
        try:
            _ = parsed.port
        except ValueError as error:
            raise ValueError("Vault address port is invalid") from error
        if (
            parsed.scheme != "https"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.hostname is None
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Vault address must be an HTTPS origin")

        return value.rstrip("/")

    @field_validator("vault_transit_mount", "vault_transit_key_name")
    @classmethod
    def validate_vault_name(cls, value: str) -> str:
        if NAME_PATTERN.fullmatch(value) is None:
            raise ValueError("Vault Transit name is invalid")

        return value

    @field_validator("vault_namespace")
    @classmethod
    def validate_vault_namespace(cls, value: str | None) -> str | None:
        if value is not None and (
            NAMESPACE_PATTERN.fullmatch(value) is None or ".." in value.split("/")
        ):
            raise ValueError("Vault namespace is invalid")

        return value

    @field_validator("vault_agent_token_file", "vault_ca_file")
    @classmethod
    def validate_absolute_path(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError("Vault file paths must be absolute")

        return value
