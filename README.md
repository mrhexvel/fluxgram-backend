# FluxGram Backend

Backend services and public read API for FluxGram profiles, badges and plugin reputation.

The backend provides separate public API and Telegram webhook processes, PostgreSQL-backed appearance operations, audit history and explicit operational commands. Public profile responses contain renderer-ready badge definitions and resolve migrated group aliases to their canonical peer keys.

The dedicated reputation API serves the strict binary plugin reputation read contract when
`REPUTATION_API_AUTHORITY_KEY_ID` is configured. It reads exact pre-signed records from
PostgreSQL and verifies their identity, pinned P-256 authority, canonical signature and validity
before returning them. The API process has no Vault, publisher or administrative queue capability.

## Development

The project uses Python 3.12 and `uv`.

```sh
uv sync --frozen
uv run pytest
uv run ruff check .
uv run mypy src tests
```

Runtime configuration is read from environment variables. `DATABASE_URL` must use the `postgresql+asyncpg` driver. Production containers can instead use an absolute `DATABASE_URL_FILE` owned by the process user with mode `0400`; the two sources are mutually exclusive. The bot process additionally requires `TELEGRAM_BOT_TOKEN`, `TELEGRAM_WEBHOOK_SECRET` and a comma-separated `TELEGRAM_ADMIN_IDS` allowlist; webhook registration also requires an HTTPS `TELEGRAM_WEBHOOK_BASE_URL`.

Public API cache and rate-limit settings are configured with `API_CACHE_TTL_SECONDS`, `API_NEGATIVE_CACHE_TTL_SECONDS`, `API_RATE_LIMIT_REQUESTS`, `API_RATE_LIMIT_WINDOW_SECONDS` and `API_RATE_LIMIT_MAX_CLIENTS`.

Compose uses local `fluxgram` database credentials by default. `DATABASE_URL` addresses PostgreSQL from the host, while `COMPOSE_DATABASE_URL` addresses the `postgres` service from containers. When PostgreSQL credentials are overridden, update both percent-encoded URLs.

```sh
uv run python -m fluxgram_backend migrate
uv run python -m fluxgram_backend api
uv run python -m fluxgram_backend reputation-api
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

The complete local plugin reputation stack includes PostgreSQL, Vault, Vault Agent, publisher and
the read-only public API. Bootstrap configures Vault and passes only the public authority key ID to
the API automatically:

```sh
docker compose -f deploy/plugin-reputation/compose.local.yml up --build -d
curl --fail http://127.0.0.1:18080/health/ready
```

No host Vault installation, role creation or manual unseal/token command is required for this local
stack. The regular `api` command serves profile routes; `reputation-api` serves only the binary
reputation route and health probes.

The OpenAPI source of truth is `openapi/appearance-v1.yaml`. Example payloads are stored under `fixtures/appearance/v1`.
