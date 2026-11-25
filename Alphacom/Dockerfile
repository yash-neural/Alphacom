FROM python:3.13-slim

COPY --from=ghcr.io/astral-sh/uv:0.13.7 /uv /usr/local/bin/uv
ENV UV_PROJECT_ENVIRONMENT=/opt/venv UV_COMPILE_BYTECODE=1 PATH="/opt/venv/bin:$PATH"

WORKDIR /srv
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY openapi.yaml ./
COPY src ./src
COPY mocks ./mocks
COPY examples ./examples
COPY alembic.ini ./
COPY migrations ./migrations

RUN useradd --uid 10001 --no-create-home app
USER app
EXPOSE 8080
CMD ["uvicorn", "app.main:app", "--app-dir", "src", "--host", "0.0.0.0", "--port", "8080"]
