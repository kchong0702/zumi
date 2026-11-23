# Channel service

The channel service is the only service Zumi's apps talk to. It is written in Go. It holds one WebSocket per app, checks each app's Keycloak token, starts runs on the Zumi harness with a token exchanged for the harness, and relays every AG-UI event of a user's runs to every socket that user has open. When the harness stream drops, it reconnects and carries on from the last event it relayed.

- [Architecture](docs/architecture/overview.md): what it does, the socket protocol, the tables and the reconnect loop.
- [Running it](docs/development/running.md): locally, in Docker Compose, migrations and checks.
- [Coding guidelines](docs/development/coding-guidelines.md): how the Go code is written.

```bash
pnpm exec nx run channel-service:migrate
```

```bash
pnpm exec nx run channel-service:dev
```

In Docker Compose, `channel-service-migrate` applies the migrations before the service starts:

```bash
docker compose --profile apps up channel-service
```
