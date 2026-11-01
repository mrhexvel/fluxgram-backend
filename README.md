# FluxGram Backend

Backend services and public read API for FluxGram profiles, badges and plugin reputation.

The backend provides separate public API and Telegram webhook processes, PostgreSQL-backed appearance operations, audit history and explicit operational commands. Public profile responses contain renderer-ready badge definitions and resolve migrated group aliases to their canonical peer keys.

The plugin reputation package currently provides a strict binary read contract and signature
verification for pre-signed records. Its route is registered only when an explicit read service is
supplied; production persistence, publishing and authority-key ownership are not part of the API
process yet.

## Development

The project uses Python 3.12 and `uv`.

```sh
uv sync --frozen
uv run pytest
uv run ruff check .
uv run mypy src tests
```

Runtime configuration is read from environment variables. `DATABASE_URL` must use the `postgresql+asyncpg` driver. The bot process additionally requires `TELEGRAM_BOT_TOKEN`, `TELEGRAM_WEBHOOK_SECRET` and a comma-separated `TELEGRAM_ADMIN_IDS` allowlist; webhook registration also requires an HTTPS `TELEGRAM_WEBHOOK_BASE_URL`.

Public API cache and rate-limit settings are configured with `API_CACHE_TTL_SECONDS`, `API_NEGATIVE_CACHE_TTL_SECONDS`, `API_RATE_LIMIT_REQUESTS`, `API_RATE_LIMIT_WINDOW_SECONDS` and `API_RATE_LIMIT_MAX_CLIENTS`.

Compose uses local `fluxgram` database credentials by default. `DATABASE_URL` addresses PostgreSQL from the host, while `COMPOSE_DATABASE_URL` addresses the `postgres` service from containers. When PostgreSQL credentials are overridden, update both percent-encoded URLs.

```sh
uv run python -m fluxgram_backend migrate
uv run python -m fluxgram_backend api
uv run python -m fluxgram_backend bot
uv run python -m fluxgram_backend webhook set
uv run python -m fluxgram_backend webhook info
```

Migrations and webhook registration are explicit commands. They are not run while API or bot replicas start.

The appearance database keeps a monotonic revision per canonical peer. Administrative mutations, their operation IDs and append-only audit snapshots commit in one transaction. Changes to a badge definition increment the revisions of profiles which use that badge. Reusing an operation ID with the same intent returns its stored result; reusing it for another intent is rejected. Rollback restores a previous appearance as a new revision and never rewrites audit history.

Bot commands are accepted only from allowlisted users in private chats. Run `/admin` to open the inline control panel. The panel asks only for a numeric Telegram user ID; actions, badges, positions, expiry and confirmations are selected with inline buttons. The initial migration adds the built-in `developer` badge definition.

```text
/admin
```

For local PostgreSQL 18:

```sh
docker compose up -d postgres
docker compose --profile tools run --rm migrate
docker compose --profile api up api
```

The OpenAPI source of truth is `openapi/appearance-v1.yaml`. Example payloads are stored under `fixtures/appearance/v1`.
