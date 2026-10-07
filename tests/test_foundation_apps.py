from aiogram import Bot, Dispatcher
from fastapi.testclient import TestClient

from fluxgram_backend.api.app import create_api_app
from fluxgram_backend.bot.app import create_bot_app
from fluxgram_backend.settings import Settings


class FakeDatabase:
    def __init__(self, ready: bool) -> None:
        self.ready = ready
        self.closed = False

    async def is_ready(self) -> bool:
        return self.ready

    async def close(self) -> None:
        self.closed = True


def test_api_health_endpoints_report_database_state() -> None:
    database = FakeDatabase(ready=True)

    with TestClient(create_api_app(Settings(), database)) as client:
        assert client.get("/health/live").json() == {
            "status": "ok",
            "service": "api",
        }
        assert client.get("/health/ready").json() == {
            "status": "ready",
            "service": "api",
        }

        database.ready = False
        response = client.get("/health/ready")

        assert response.status_code == 503
        assert response.json() == {"status": "unavailable", "service": "api"}

    assert database.closed


def test_bot_webhook_requires_secret_and_accepts_valid_update() -> None:
    settings = Settings.model_validate({"telegram_webhook_secret": "webhook-secret"})
    database = FakeDatabase(ready=True)
    bot = Bot("123456:abcdefghijklmnopqrstuvwxyzABCDEFGHI")
    dispatcher = Dispatcher()
    lifecycle_events: list[str] = []

    async def on_startup() -> None:
        lifecycle_events.append("startup")

    async def on_shutdown() -> None:
        lifecycle_events.append("shutdown")

    dispatcher.startup.register(on_startup)
    dispatcher.shutdown.register(on_shutdown)
    app = create_bot_app(settings, database, bot, dispatcher)

    with TestClient(app) as client:
        assert client.post("/webhooks/telegram", json={"update_id": 1}).status_code == 401

        response = client.post(
            "/webhooks/telegram",
            headers={"X-Telegram-Bot-Api-Secret-Token": "webhook-secret"},
            json={
                "update_id": 1,
                "message": {
                    "message_id": 1,
                    "date": 0,
                    "chat": {"id": 1, "type": "private"},
                    "from": {
                        "id": 1,
                        "is_bot": False,
                        "first_name": "Admin",
                    },
                    "text": "/start",
                },
            },
        )

        assert response.status_code == 200
        assert response.json() == {"ok": True}

    assert database.closed
    assert lifecycle_events == ["startup", "shutdown"]


def test_bot_webhook_rejects_invalid_update() -> None:
    settings = Settings.model_validate({"telegram_webhook_secret": "webhook-secret"})
    database = FakeDatabase(ready=True)
    bot = Bot("123456:abcdefghijklmnopqrstuvwxyzABCDEFGHI")
    app = create_bot_app(settings, database, bot, Dispatcher())

    with TestClient(app) as client:
        wrong_secret = client.post(
            "/webhooks/telegram",
            headers={"X-Telegram-Bot-Api-Secret-Token": "wrong-secret"},
            json={"update_id": 1},
        )
        response = client.post(
            "/webhooks/telegram",
            headers={"X-Telegram-Bot-Api-Secret-Token": "webhook-secret"},
            json={},
        )

        assert wrong_secret.status_code == 401
        assert response.status_code == 400
