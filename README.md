# FluxGram Backend

Backend services and API contracts for FluxGram profiles and badges.

Stage 1 established canonical peer identifiers, the Appearance API contract and versioned fixtures. FastAPI, PostgreSQL and the Telegram webhook are introduced by later stages from `docs/fluxgram-appearance-backend.md`.

## Development

The project uses Python 3.12 and `uv`.

```sh
uv sync --frozen
uv run pytest
uv run ruff check .
uv run mypy src tests
```

The OpenAPI source of truth is `openapi/appearance-v1.yaml`. Example payloads are stored under `fixtures/appearance/v1`.
