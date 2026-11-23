# Running the channel service

The channel service needs Postgres for `agent_runs`, Keycloak to check and exchange tokens, and the Zumi harness to run agents. Read [the architecture overview](../architecture/overview.md) for what it does. It needs Go 1.27.

## Locally

1. From the repository root, run `pnpm run bootstrap` once. It copies `apps/channel-service/.env.example` to `apps/channel-service/.env`, which holds the development secret of the `zumi-channel` client.
2. Run `pnpm run infra:up`. It starts the collector, Postgres on `127.0.0.1:5433` (`POSTGRES_HOST_PORT`) and Keycloak on `http://localhost:8180`. Postgres creates the `channel` schema the first time its volume is made. When `POSTGRES_HOST_PORT` differs, set `ZUMI_CHANNEL_DATABASE__URL` in `.env`.
3. Run `pnpm exec nx run channel-service:migrate`. It runs `go run ./cmd/channel-service migrate`, which creates the `channel` schema when missing and applies the goose migrations, keeping their version in `channel.goose_db_version`. Run it again after you pull a new migration. `go run ./cmd/channel-service migrate down` rolls every migration back.
4. Start the harness on `http://127.0.0.1:8300` (`[harness] url`).
5. Run `pnpm exec nx run channel-service:dev`. The server listens on `127.0.0.1:8400`. `GET /health` answers `{"status":"ok","version":...}`, and the app connects to `ws://127.0.0.1:8400/ws`.

To try the socket without the app, get a `zumi-app` token the way `infra/keycloak/verify.py` signs in, then connect with any WebSocket client, send `Authorization: Bearer <token>`, and send `{"type":"message","id":"<uuid>","text":"Hi"}`.

## In Docker Compose

`docker-compose.yaml` runs the service under the `apps` profile, and the root `docker-compose.yaml` includes it. It points `ZUMI_CHANNEL_DATABASE__URL` at `postgres:5432` with the root `POSTGRES_*` variables, fetches the realm's keys and exchanges tokens at `keycloak:8080` while the issuer stays `http://localhost:8180/realms/zumi`, takes the client secret from the root `KEYCLOAK_CHANNEL_CLIENT_SECRET` and sends telemetry to `otel-collector:4318` when it is on. It listens on `127.0.0.1:8400` (`CHANNEL_SERVICE_HOST_PORT`). The image is a static binary on distroless, running as `nonroot`. Build it with `pnpm exec nx run channel-service:build`.

The one-shot `channel-service-migrate` service runs the same image with `migrate` once Postgres is healthy. `channel-service` starts only after it exits successfully and Keycloak is healthy, so `docker compose --profile apps up` applies new migrations before every start.

The service calls the harness at `CHANNEL_HARNESS_URL`, `http://harness:8000` by default, the name of the harness app's compose service. This repository's compose project doesn't run the harness yet, so set `CHANNEL_HARNESS_URL` in the root `.env`, such as `http://host.docker.internal:8300` for a harness started on the host.

## Migrations

To change a table, add the next numbered file to `internal/store/migrations`, such as `00002_add_channels.sql`, with `-- +goose Up` and `-- +goose Down` sections. Name tables without a schema: the connection's `search_path` picks it. The binary embeds the files, so a rebuilt image carries them.

The `Migrations` GitHub workflow applies the migrations to a fresh Postgres, rolls them back, applies them again and runs the tests against it.

## Checks

From the root, `pnpm exec nx run-many -t lint format:check test -p channel-service` runs:

- `lint`: `go vet ./...` and `golangci-lint run` (v2.15.0, built with `go run`, so the first run takes a minute).
- `format:check`: `gofmt -l`, which fails when it lists a file. `format` runs `gofmt -w .`.
- `test`: `go test -race ./...`.

Tests need no Keycloak or harness. They sign tokens with an RSA key of their own, serve its JWKS and the token endpoint from `internal/fakekeycloak`, and run a fake harness with `httptest`. Tests that need Postgres run only when `ZUMI_CHANNEL_TEST_DATABASE_URL` points at one, for example:

```bash
ZUMI_CHANNEL_TEST_DATABASE_URL=postgresql://zumi:zumi@127.0.0.1:5433/zumi go test -race ./...
```

Each of those tests migrates a scratch schema of its own and drops it.
