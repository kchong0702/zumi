# Coding guidelines

These rules apply to all code in the channel service. For how the service is put together, read [the architecture overview](../architecture/overview.md).

## Follow the library's convention

Before you build a mechanism, check whether the standard library or a library the service already uses has one: `net/http`, `log/slog`, `coder/websocket`, `go-oidc`, `x/oauth2`, `pgx`, `goose`, `go-sse`, `backoff`, `koanf` or OpenTelemetry. If it does, use it the way its documentation shows. If none fits, look for a maintained library before writing your own, and name the options you checked in the PR.

## Simplicity

- Keep it simple. Add an interface, layer or package only when the task needs it now. Define an interface where it is used, not where it is implemented, as `channel.Runs` is.
- Write something that is used in only one place inline. Don't extract it into a variable, function or type.
- Make the smallest change that does the task.
- Remove packages, files and code that nothing uses anymore.

## Layout

- `cmd/channel-service` holds the binary: it loads the config, wires the packages together, serves `/health` and runs migrations.
- Everything else lives in a package under `internal/`, named for what it does, in one lowercase word.
- Define a type, function or helper above the code that uses it, as `run` sits above `main`.
- Start a file with `package` and its imports: no comment above them.
- Put a test next to the code it tests, in the `_test` package, so it uses the package as callers do.
- Take settings such as URLs, limits and timeouts from config. Never hardcode them.

## Types and errors

- Use the real type. Avoid `any` except for JSON whose shape you don't own.
- Give a kind a named string type and constants, such as `store.RunStatus`. The database stores it as text, with no `CHECK`.
- Parse outside data, such as a socket message, a token or an SSE event, at the boundary, and trust the type after that.
- Return errors wrapped with `fmt.Errorf("doing x: %w", err)`. Check them with `errors.Is` and `errors.As`.
- Pass a `context.Context` as the first argument of anything that waits, and stop when it is done.

## Names

- A name says exactly what the thing is or does. Rename a misleading name.
- Follow Go's naming: `MixedCaps`, short receivers, initialisms in one case (`runID`, `JWKSURL`).

## Comments

- Give every exported identifier, struct fields included, a doc comment of at most two lines that starts with its name, as `go doc` expects. Say what it does, not who calls it.
- Write no other comments: no package comments, no comments on unexported code and no comments inside functions. If code needs one, rename or restructure it.
- Log with `log/slog`, with the request's context, never `fmt.Print`.

## Tests

- Assert what a user of the code can observe: messages on the socket, requests the harness got and what the store recorded. Don't assert how it works inside.
- Write table-driven tests where cases differ only in their input.
- Never call the real Keycloak or harness. Use `internal/fakekeycloak` and an `httptest` fake harness.
- Tests that need Postgres use `storetest.New`, which skips them unless `ZUMI_CHANNEL_TEST_DATABASE_URL` is set and drops its scratch schema afterwards.

## Formatting and lint

`gofmt` formats the code. `go vet` and `golangci-lint` (pinned in `project.json`, run with `go run`) must report nothing. `.golangci.yml` only stops `errcheck` from asking for the error of a `Close`.

## Git

- Use Conventional Commits: `type(scope): summary`, with `feat`, `fix`, `refactor` or `chore`, and `channel-service` as the scope.
- Rebase onto the base branch. Never merge it in. Force-push only after a rebase, with `--force-with-lease`.
- Show evidence in the PR that the change works, and keep the PR small enough to review.
