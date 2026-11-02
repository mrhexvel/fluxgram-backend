import os
import re
import unicodedata
from ipaddress import IPv4Network
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, HttpUrl, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

from fluxgram_backend.adapters.secure_file import SecureFileError, read_bounded_regular_file

AUTHORITY_KEY_ID_PATTERN = re.compile(r"^p256:[0-9a-f]{64}$")
DATABASE_URL_FILE_MODES = frozenset((0o400,))
MAX_DATABASE_URL_BYTES = 4096


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        extra="ignore",
    )

    environment: Literal["development", "test", "production"] = "development"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    database_url: SecretStr | None = None
    database_url_file: Path | None = None
    api_host: str = "127.0.0.1"
    api_port: int = Field(default=8080, ge=1, le=65535)
    api_cache_ttl_seconds: int = Field(default=300, ge=1, le=86400)
    api_negative_cache_ttl_seconds: int = Field(default=60, ge=1, le=3600)
    api_rate_limit_requests: int = Field(default=120, ge=1, le=100000)
    api_rate_limit_window_seconds: int = Field(default=60, ge=1, le=3600)
    api_rate_limit_max_clients: int = Field(default=10000, ge=1, le=1000000)
    reputation_api_authority_key_id: str | None = None
    reputation_api_body_timeout_seconds: float = Field(default=2.0, gt=0, le=10)
    plugin_reputation_lookup_timeout_seconds: float = Field(default=2.0, gt=0, le=10)
    reputation_api_trusted_proxy_network: IPv4Network = IPv4Network("127.0.0.0/8")
    bot_host: str = "127.0.0.1"
    bot_port: int = Field(default=8081, ge=1, le=65535)
    telegram_bot_token: SecretStr | None = None
    telegram_admin_ids: Annotated[frozenset[int], NoDecode] = frozenset()
    telegram_webhook_base_url: HttpUrl | None = None
    telegram_webhook_secret: SecretStr | None = None

    @field_validator("database_url")
    @classmethod
    def validate_database_url(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None:
            return value

        try:
            url = make_url(value.get_secret_value())
        except ArgumentError as error:
            raise ValueError("database_url must be a valid SQLAlchemy URL") from error

        if url.drivername != "postgresql+asyncpg":
            raise ValueError("database_url must use the postgresql+asyncpg driver")

        return value

    @field_validator("database_url_file")
    @classmethod
    def validate_database_url_file(cls, value: Path | None) -> Path | None:
        if value is not None and not value.is_absolute():
            raise ValueError("database_url_file must be absolute")
        return value

    @field_validator("reputation_api_authority_key_id")
    @classmethod
    def validate_reputation_api_authority_key_id(cls, value: str | None) -> str | None:
        if value is not None and AUTHORITY_KEY_ID_PATTERN.fullmatch(value) is None:
            raise ValueError("plugin reputation authority key ID is invalid")
        return value

    @model_validator(mode="after")
    def validate_database_source(self) -> "Settings":
        if self.database_url is not None and self.database_url_file is not None:
            raise ValueError("database_url and database_url_file are mutually exclusive")
        return self

    @field_validator("telegram_webhook_base_url")
    @classmethod
    def validate_webhook_base_url(cls, value: HttpUrl | None) -> HttpUrl | None:
        if value is not None and value.scheme != "https":
            raise ValueError("telegram_webhook_base_url must use HTTPS")

        return value

    @field_validator("telegram_admin_ids", mode="before")
    @classmethod
    def parse_admin_ids(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]

        return value

    @field_validator("telegram_admin_ids")
    @classmethod
    def validate_admin_ids(cls, value: frozenset[int]) -> frozenset[int]:
        if any(admin_id <= 0 for admin_id in value):
            raise ValueError("telegram_admin_ids must contain positive identifiers")

        return value

    @field_validator("telegram_webhook_secret")
    @classmethod
    def validate_webhook_secret(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None:
            return value

        secret = value.get_secret_value()
        if len(secret) > 256 or re.fullmatch(r"[A-Za-z0-9_-]+", secret) is None:
            raise ValueError("telegram_webhook_secret contains unsupported characters")

        return value

    def require_database_url(self) -> str:
        if self.database_url is not None:
            return self.database_url.get_secret_value()
        if self.database_url_file is None:
            raise RuntimeError("DATABASE_URL or DATABASE_URL_FILE is required for this command")

        try:
            encoded = read_bounded_regular_file(
                self.database_url_file,
                MAX_DATABASE_URL_BYTES,
                DATABASE_URL_FILE_MODES,
                frozenset((os.geteuid(),)),
            )
        except SecureFileError as error:
            raise RuntimeError("DATABASE_URL_FILE is invalid") from error

        if encoded.endswith(b"\n"):
            encoded = encoded[:-1]
        try:
            value = encoded.decode("utf-8")
        except UnicodeDecodeError as error:
            raise RuntimeError("DATABASE_URL_FILE is invalid") from error
        if (
            not value
            or value != value.strip()
            or any(unicodedata.category(character) == "Cc" for character in value)
        ):
            raise RuntimeError("DATABASE_URL_FILE is invalid")

        try:
            url = make_url(value)
        except ArgumentError as error:
            raise RuntimeError("DATABASE_URL_FILE is invalid") from error
        if url.drivername != "postgresql+asyncpg":
            raise RuntimeError("DATABASE_URL_FILE is invalid")
        return value

    def require_reputation_api_authority_key_id(self) -> str:
        if self.reputation_api_authority_key_id is None:
            raise RuntimeError("REPUTATION_API_AUTHORITY_KEY_ID is required for this command")
        return self.reputation_api_authority_key_id

    def require_bot_token(self) -> str:
        if self.telegram_bot_token is None:
            raise RuntimeError("TELEGRAM_BOT_TOKEN is required for this command")

        return self.telegram_bot_token.get_secret_value()

    def require_webhook_secret(self) -> str:
        if self.telegram_webhook_secret is None:
            raise RuntimeError("TELEGRAM_WEBHOOK_SECRET is required for this command")

        return self.telegram_webhook_secret.get_secret_value()

    def webhook_url(self) -> str:
        if self.telegram_webhook_base_url is None:
            raise RuntimeError("TELEGRAM_WEBHOOK_BASE_URL is required for this command")

        base_url = str(self.telegram_webhook_base_url).rstrip("/")
        return f"{base_url}/webhooks/telegram"
