# Coding guidelines

These rules apply to all code in the harness. For how the harness is put together, read [the architecture overview](../architecture/overview.md).

## Follow the library's convention

Before you build a mechanism, check whether LangChain, LangGraph, deepagents, AG-UI or another library the harness uses already has one. If it does, use it the way its documentation shows. Don't invent a parallel version. If the library's convention doesn't fit, say why in the PR description before you write your own.

## Simplicity

- Keep it simple. Add an abstraction, layer, base class or registry only when the task needs it now.
- Write something that is used in only one place inline. Don't extract it into a variable, function or type.
- Prefer a library over custom logic.
- Make the smallest change that does the task.
- Don't nest a function inside another function when a module-level function works.
- Remove folders, files and code that nothing uses anymore.

## Layout

- Name folders and files in snake_case.
- Group related files into a folder.
- Put a helper above the function that uses it. Put the file's main item at the bottom.
- Start a file with its imports: no module docstring and no comment at the top.
- Let `__init__.py` only re-export.
- Put tests under `tests/`, mirroring `src/`.
- Take settings such as URLs, limits and model names from config. Never hardcode them.

## Types

- Use the real type. Don't use `Any` or a bare `dict`.
- Put `model_config = ConfigDict(extra="forbid", frozen=True, use_attribute_docstrings=True)` inside each pydantic class. Don't add a base class only to share config.
- Mark an overriding method with `@override`.
- Prefer being explicit over silencing a linter or type checker.
- Parse outside data into a model at the boundary, and trust the type after that.

## Names

- A name says exactly what the thing is or does. Rename a misleading name.

## Docstrings and comments

- Don't put a docstring on a class itself.
- Give every function, and every field in a class, a docstring of at most two lines. Say what it does, not what happens around it, and never name its callers or other modules.
- A tool's schema carries its description, and the model sees each description once.
- Don't write code comments. Log with structlog, not `print`.

## Tests

- Assert what a user of the code can observe, not how it works inside.

## Git

- Use Conventional Commits: `type(scope): summary`, with `feat`, `fix`, `refactor` or `chore`, and `harness` or the agent's name as the scope.
- Rebase onto the base branch. Never merge it in. Force-push only after a rebase, with `--force-with-lease`.
- Show evidence in the PR that the change works, and keep the PR small enough to review.
