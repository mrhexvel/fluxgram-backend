import re
from typing import Annotated, Literal

from pydantic import Field, HttpUrl, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError


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
    api_host: str = "127.0.0.1"
    api_port: int = Field(default=8080, ge=1, le=65535)
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
        if self.database_url is None:
            raise RuntimeError("DATABASE_URL is required for this command")

        return self.database_url.get_secret_value()

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
