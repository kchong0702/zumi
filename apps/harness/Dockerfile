FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim AS harness-build

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0

WORKDIR /app

COPY pyproject.toml uv.lock ./

COPY apps/harness/pyproject.toml ./apps/harness/

RUN uv sync --frozen --no-dev --package harness

COPY apps/harness ./apps/harness

FROM python:3.13-slim-bookworm AS harness

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH" \
    ZUMI_HARNESS_APP__HOST=0.0.0.0 \
    ZUMI_HARNESS_APP__PORT=8000

COPY --from=harness-build /app/.venv /app/.venv

COPY --from=harness-build /app/apps/harness /app/apps/harness

RUN useradd --create-home --uid 10001 zumi

USER zumi
WORKDIR /app/apps/harness

EXPOSE 8000

CMD ["python", "-m", "src.main"]
