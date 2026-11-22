# Zumi

Zumi is a personal assistant that remembers you. This repository is an Nx monorepo that holds every service behind it.

## Layout

| Path                   | Holds                                                          |
| ---------------------- | -------------------------------------------------------------- |
| `apps/`                | The services, one Nx project each                              |
| `infra/otel-collector` | The OpenTelemetry Collector config the services send traces to |
| `scripts/`             | Bootstrap and commit-policy scripts                            |
| `docker-compose.yaml`  | Shared infrastructure: the collector and Postgres              |

## Prerequisites

- Node 24 and pnpm 11 (`corepack enable`)
- [uv](https://docs.astral.sh/uv/) for the Python services
- Docker

## Getting started

```bash
pnpm install
```

```bash
pnpm run bootstrap
```

```bash
pnpm run infra:up
```

```bash
pnpm run dev
```

- `pnpm install` installs Nx, Prettier and the Git hooks.
- `pnpm run bootstrap` copies `.env.example` to `.env`, installs Python into `.python/` if needed, syncs the uv workspace and runs each project's `bootstrap` target.
- `pnpm run infra:up` starts the OpenTelemetry Collector and Postgres. Postgres listens on `127.0.0.1:5433`, set by `POSTGRES_HOST_PORT`.
- `pnpm run dev` starts every service's `dev` target.

## Checks

`pnpm run lint`, `pnpm run test` and `pnpm run format:check` run each target across every project with Nx. CI runs the same targets on every push and pull request.

## Tracing

Services send OpenTelemetry traces and metrics over OTLP to the collector on `127.0.0.1:4317` (gRPC) and `127.0.0.1:4318` (HTTP). The collector adds `deployment.environment` and prints what it receives with its debug exporter. `infra/otel-collector/config.yaml` reads every setting from the `COLLECTOR_*` variables in `.env`.

## Commits

The Git hooks run Prettier and Ruff on staged files and check every commit message: Conventional Commits with the Nx project as the scope, and no AI attribution. `scripts/workflows/check-ai-attribution.mjs` holds that policy, and CI runs it on every push and pull request.
