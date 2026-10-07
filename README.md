# FluxGram Backend

Backend services and API contracts for FluxGram profiles and badges.

Stage 1 established canonical peer identifiers, the Appearance API contract and versioned fixtures. The backend foundation provides separate API and Telegram webhook processes, PostgreSQL readiness checks, Loguru output and explicit operational commands. Database entities and appearance operations are introduced by the next stage from `docs/fluxgram-appearance-backend.md`.

## Development

The project uses Python 3.12 and `uv`.

```sh
uv sync --frozen
uv run pytest
uv run ruff check .
uv run mypy src tests
```

Runtime configuration is read from environment variables. `DATABASE_URL` must use the `postgresql+asyncpg` driver. The bot process additionally requires `TELEGRAM_BOT_TOKEN` and `TELEGRAM_WEBHOOK_SECRET`; webhook registration also requires an HTTPS `TELEGRAM_WEBHOOK_BASE_URL`.

Compose uses local `fluxgram` database credentials by default. `DATABASE_URL` addresses PostgreSQL from the host, while `COMPOSE_DATABASE_URL` addresses the `postgres` service from containers. When PostgreSQL credentials are overridden, update both percent-encoded URLs.

```sh
uv run python -m fluxgram_backend migrate
uv run python -m fluxgram_backend api
uv run python -m fluxgram_backend bot
uv run python -m fluxgram_backend webhook set
uv run python -m fluxgram_backend webhook info
```

Migrations and webhook registration are explicit commands. They are not run while API or bot replicas start.

For local PostgreSQL 18:

```sh
docker compose up -d postgres
docker compose --profile tools run --rm migrate
docker compose --profile api up api
```

The OpenAPI source of truth is `openapi/appearance-v1.yaml`. Example payloads are stored under `fixtures/appearance/v1`.
