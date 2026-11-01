import os
import re
import unicodedata
from ipaddress import IPv4Network
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from fluxgram_backend.adapters.secure_file import SecureFileError, read_bounded_regular_file
from fluxgram_backend.adapters.vault_transit import NAME_PATTERN, NAMESPACE_PATTERN

AUTHORITY_KEY_ID_PATTERN = re.compile(r"^p256:[0-9a-f]{64}$")
DATABASE_URL_FILE_MODES = frozenset((0o400,))
MAX_DATABASE_URL_BYTES = 4096


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


class PluginReputationWorkerSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        extra="ignore",
    )

    plugin_reputation_worker_id: str = Field(pattern=r"^[A-Za-z0-9._:-]{1,128}$")
    plugin_reputation_worker_lease_seconds: float = Field(default=30, gt=0, le=900)
    plugin_reputation_worker_renewal_seconds: float = Field(default=10, gt=0, le=899)
    plugin_reputation_worker_poll_seconds: float = Field(default=1, gt=0, le=60)
    plugin_reputation_worker_retry_initial_seconds: float = Field(default=1, gt=0, le=86400)
    plugin_reputation_worker_retry_max_seconds: float = Field(default=60, gt=0, le=86400)
    plugin_reputation_worker_max_attempts: int = Field(default=10, ge=1, le=100)
    plugin_reputation_worker_shutdown_grace_seconds: float = Field(default=30, gt=0, le=300)
    plugin_reputation_operations_host: str = "127.0.0.1"
    plugin_reputation_operations_port: int = Field(default=8090, ge=1024, le=65535)
    plugin_reputation_operations_allowed_network: IPv4Network = IPv4Network("127.0.0.0/8")
    plugin_reputation_operations_max_connections: int = Field(default=32, ge=1, le=128)
    plugin_reputation_readiness_stale_seconds: float = Field(default=30, gt=0, le=300)
    plugin_reputation_signer_probe_seconds: float = Field(default=30, gt=0, le=300)

    @field_validator("plugin_reputation_operations_host")
    @classmethod
    def validate_operations_host(cls, value: str) -> str:
        if value not in ("0.0.0.0", "127.0.0.1"):
            raise ValueError("operations host must be an explicit IPv4 bind address")
        return value

    @model_validator(mode="after")
    def validate_intervals(self) -> "PluginReputationWorkerSettings":
        if (
            self.plugin_reputation_worker_renewal_seconds
            * 2
            > self.plugin_reputation_worker_lease_seconds
        ):
            raise ValueError("worker renewal interval must not exceed half the lease")
        if (
            self.plugin_reputation_worker_retry_initial_seconds
            > self.plugin_reputation_worker_retry_max_seconds
        ):
            raise ValueError("worker initial retry must not exceed maximum retry")
        if self.plugin_reputation_readiness_stale_seconds <= max(
            self.plugin_reputation_worker_poll_seconds * 2,
            self.plugin_reputation_worker_renewal_seconds * 2,
        ):
            raise ValueError("readiness stale interval is too short")

        return self


class PluginReputationProcessSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        extra="ignore",
    )

    database_url: SecretStr | None = None
    database_url_file: Path | None = None
    log_level: str = "INFO"

    @field_validator("database_url")
    @classmethod
    def validate_database_url(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None:
            return None
        if not value.get_secret_value().startswith("postgresql+asyncpg://"):
            raise ValueError("database_url must use postgresql+asyncpg")
        return value

    @field_validator("database_url_file")
    @classmethod
    def validate_database_url_file(cls, value: Path | None) -> Path | None:
        if value is not None and not value.is_absolute():
            raise ValueError("database_url_file must be absolute")
        return value

    @model_validator(mode="after")
    def validate_database_source(self) -> "PluginReputationProcessSettings":
        if (self.database_url is None) == (self.database_url_file is None):
            raise ValueError("exactly one database URL source is required")
        return self

    def resolve_database_url(self) -> str:
        if self.database_url is not None:
            return self.database_url.get_secret_value()
        if self.database_url_file is None:
            raise RuntimeError("database URL source is unavailable")

        try:
            encoded = read_bounded_regular_file(
                self.database_url_file,
                MAX_DATABASE_URL_BYTES,
                DATABASE_URL_FILE_MODES,
                frozenset((os.geteuid(),)),
            )
        except SecureFileError as error:
            raise RuntimeError("database URL file is invalid") from error

        if encoded.endswith(b"\n"):
            encoded = encoded[:-1]
        try:
            value = encoded.decode("utf-8")
        except UnicodeDecodeError as error:
            raise RuntimeError("database URL file is invalid") from error
        if (
            not value.startswith("postgresql+asyncpg://")
            or not value
            or value != value.strip()
            or any(unicodedata.category(character) == "Cc" for character in value)
        ):
            raise RuntimeError("database URL file is invalid")
        return value
