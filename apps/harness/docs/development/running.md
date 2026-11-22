# Running the harness

The harness needs Postgres for its threads, runs, events, transcript, usage and LangGraph checkpoints, and Keycloak for the tokens every request carries. Read [the architecture overview](../architecture/overview.md#what-postgres-keeps) for what each table holds.

## Locally

1. From the repository root, run `pnpm run bootstrap` once. It syncs the uv workspace, which holds the harness, and copies `apps/harness/.env.example` to `apps/harness/.env`. Fill in the model and search keys you have.
2. Run `pnpm run infra:up`. It starts the collector, Postgres on `127.0.0.1:5433` (`POSTGRES_HOST_PORT`), Keycloak on `http://localhost:8180` and SearXNG on `127.0.0.1:8310`. Postgres creates the `harness` schema the first time its volume is made, and `[database] url` in `config.toml` points at it.
3. Run `pnpm exec nx run harness:migrate`. It runs the harness's Alembic migrations (`uv run alembic upgrade head`), which create the `harness` schema when missing and the harness's tables, and keep their version in `harness.alembic_version`. Run it again after you pull a new migration.
4. Run `pnpm exec nx run harness:dev`. The server listens on `127.0.0.1:8300` and creates LangGraph's tables when it starts. `GET /health` reports that it is up.

Sothoth's browser needs Node 22 or newer for `npx`.

To call an agent, get a token for `zumi-harness` from the realm, the way the channel service will: sign in as `zumi-app` and exchange the token as `zumi-channel` for the `zumi-harness` audience, as `infra/keycloak/verify.py` does. Then:

```bash
curl -N http://127.0.0.1:8300/sothoth \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"threadId": "ignored", "runId": "'"$(uuidgen)"'", "messages": [{"id": "1", "role": "user", "content": "Hi"}]}'
```

The harness runs only the last user message in `messages`. A body without one gets 422 and nothing is recorded.

## In Docker Compose

`docker-compose.yaml` runs the harness under the `apps` profile, and the root `docker-compose.yaml` includes it. It waits for Postgres and Keycloak, points `ZUMI_HARNESS_DATABASE__URL` at `postgres:5432` with the root `POSTGRES_*` variables, fetches the realm's keys from `keycloak:8080`, asks SearXNG at `searxng:8080` and sends traces to `otel-collector:4318` when tracing is on. Build the image with `pnpm exec nx run harness:build`, then apply its migrations with:

```bash
docker compose run --rm --no-deps harness alembic upgrade head
```

The image has no Node, so Sothoth can't start Playwright MCP inside it, and `[browser]` can't be switched to a running Playwright MCP server from the environment yet. Run the harness locally to use the browser.

## Migrations

To change a table, edit it in `src/infrastructure/persistence/tables.py`, then run `uv run alembic revision --autogenerate -m "<what changed>"` in `apps/harness` and check the new file in `migrations/versions/`. `uv run alembic check` says whether the tables and the migrations still match. Alembic manages only the tables in that module's `metadata`; LangGraph creates its own.

The `Migrations` GitHub workflow runs `alembic upgrade head`, `alembic check`, `alembic downgrade base` and `alembic upgrade head` against a fresh Postgres, then the `postgres` tests. To do the same locally without touching the `harness` schema, point `ZUMI_HARNESS_DATABASE__SCHEMA_NAME` at a scratch schema and drop it afterwards.

## Checks

From `apps/harness`: `uv run ruff check .`, `uv run ruff format --check .`, `uv run pyright` and `uv run pytest`, or `pnpm exec nx run-many -t lint format:check typecheck test` from the root.

Tests need no database or Keycloak. They make their own RSA key and serve its JWKS from a local HTTP server. Tests marked `postgres` run only when `ZUMI_HARNESS_TEST_DATABASE_URL` points at a Postgres, for example:

```bash
ZUMI_HARNESS_TEST_DATABASE_URL=postgresql+psycopg://zumi:zumi@127.0.0.1:5433/zumi uv run pytest -m postgres
```

Each of those tests works in a schema of its own and drops it.
