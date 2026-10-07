from pathlib import Path

import pytest
from pydantic import ValidationError

from fluxgram_backend.settings import Settings


def test_settings_load_runtime_values_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://user:secret@db/fluxgram")
    monkeypatch.setenv("API_PORT", "9080")

    settings = Settings()

    assert settings.require_database_url() == (
        "postgresql+asyncpg://user:secret@db/fluxgram"
    )
    assert settings.api_port == 9080
    assert "postgresql+asyncpg://user:secret@db/fluxgram" not in str(settings)


def test_settings_require_explicit_runtime_secrets(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    settings = Settings()

    with pytest.raises(RuntimeError, match="DATABASE_URL"):
        settings.require_database_url()

    with pytest.raises(RuntimeError, match="TELEGRAM_BOT_TOKEN"):
        settings.require_bot_token()

    with pytest.raises(RuntimeError, match="TELEGRAM_WEBHOOK_SECRET"):
        settings.require_webhook_secret()


def test_settings_ignore_empty_optional_environment_values(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "")

    settings = Settings()

    with pytest.raises(RuntimeError, match="TELEGRAM_BOT_TOKEN"):
        settings.require_bot_token()

    with pytest.raises(RuntimeError, match="TELEGRAM_WEBHOOK_SECRET"):
        settings.require_webhook_secret()


@pytest.mark.parametrize(
    "database_url",
    [
        "not-a-url",
        "postgresql://user:secret@db/fluxgram",
        "sqlite+aiosqlite:///fluxgram.db",
    ],
)
def test_settings_reject_unsupported_database_url(database_url: str) -> None:
    with pytest.raises(ValidationError):
        Settings.model_validate({"database_url": database_url})


def test_settings_require_https_webhook_url() -> None:
    with pytest.raises(ValidationError):
        Settings.model_validate({"telegram_webhook_base_url": "http://localhost:8081"})


def test_settings_build_webhook_url() -> None:
    settings = Settings.model_validate(
        {"telegram_webhook_base_url": "https://example.ngrok.app/base/"}
    )

    assert settings.webhook_url() == "https://example.ngrok.app/base/webhooks/telegram"


@pytest.mark.parametrize("secret", ["", "contains space", "contains.dot", "a" * 257])
def test_settings_reject_invalid_webhook_secret(secret: str) -> None:
    with pytest.raises(ValidationError):
        Settings.model_validate({"telegram_webhook_secret": secret})
