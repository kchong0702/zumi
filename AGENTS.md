<!-- nx configuration start-->
<!-- Leave the start & end comments to automatically receive updates. -->

# General Guidelines for working with Nx

- For navigating/exploring the workspace, invoke the `nx-workspace` skill first - it has patterns for querying projects, targets, and dependencies
- When running tasks (for example build, lint, test, e2e, etc.), always prefer running the task through `nx` (i.e. `nx run`, `nx run-many`, `nx affected`) instead of using the underlying tooling directly
- Prefix nx commands with the workspace's package manager (e.g., `pnpm nx build`, `npm exec nx test`) - avoids using globally installed CLI
- You have access to the Nx MCP server and its tools, use them to help the user
- For Nx plugin best practices, check `node_modules/@nx/<plugin>/PLUGIN.md`. Not all plugins have this file - proceed without it if unavailable.
- NEVER guess CLI flags - always check nx_docs or `--help` first when unsure

## Scaffolding & Generators

- For scaffolding tasks (creating apps, libs, project structure, setup), ALWAYS invoke the `nx-generate` skill FIRST before exploring or calling MCP tools

## When to use nx_docs

- USE for: advanced config options, unfamiliar flags, migration guides, plugin configuration, edge cases
- DON'T USE for: basic generator syntax (`nx g @nx/react:app`), standard commands, things you already know
- The `nx-generate` skill handles generator discovery internally - don't call nx_docs just to look up generator syntax


<!-- nx configuration end-->

# Zumi

- Before changing an app, read its `docs/architecture/` and `docs/development/`, and update them in the same change.
- Before writing code for a feature, look for a maintained open-source library, MCP server or framework feature that already does it, and use it. Write our own code only for what none of them covers, and name the options you checked in the PR.
- Back every pull request with evidence, not claims. Include screenshots, or output from a real run when there is no UI, and say which commit and data they came from. When the change affects how parts fit together, add a small Mermaid architecture diagram. When it changes a flow across parts, such as a run, a tool call or a reconnect, add a Mermaid sequence diagram.
- Start every pull request title with the service it changes in brackets, such as `[REPO]`, `[KEYCLOAK]`, `[ZUMI]` or `[CHANNEL]`, followed by its Conventional Commit summary.
- Never add Claude, Anthropic, or any AI assistant as an author or co-author: no `Co-Authored-By` trailers and no AI attribution in commits, PRs or comments.
- Commit as the configured git user. Never change `user.name` or `user.email`.
- Use Conventional Commits: `type(scope): summary`, with `feat`, `fix`, `refactor` or `chore`, and the Nx project (`harness`, `channel-service`, `keycloak`) or `repo` as the scope.
- Never push to `main` or merge into it. Changes reach `main` only through a pull request the user merges.
- Never `git merge main` into a branch. Rebase onto it instead.
- Never force push, except to update a branch after a rebase, with `--force-with-lease`.
