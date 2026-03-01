FROM python:3.11-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

RUN apt-get update && \
    apt-get install -y --no-install-recommends gcc libc6-dev curl && \
    curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y && \
    rm -rf /var/lib/apt/lists/*
ENV PATH="/root/.cargo/bin:$PATH"

ENV UV_SYSTEM_PYTHON=1
ENV PYTHONUNBUFFERED=1
WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY sudachi.json user.csv ./
RUN DICT_DIR=$(uv run python -c "import sudachidict_core; import os; print(os.path.join(os.path.dirname(sudachidict_core.__file__), 'resources', 'system.dic'))") && \
    SUDACHI_DIR=$(uv run python -c "import sudachipy; import os; print(os.path.join(os.path.dirname(sudachipy.__file__), 'resources'))") && \
    uv run sudachipy ubuild -o "$SUDACHI_DIR/user.dic" -s "$DICT_DIR" user.csv && \
    cp sudachi.json "$SUDACHI_DIR/"

FROM python:3.11-slim AS runner

ENV PYTHONUNBUFFERED=1
WORKDIR /app

COPY --from=builder /app/.venv/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages/
COPY . .

EXPOSE 8000
CMD ["python", "-m", "gunicorn", "-b", "0.0.0.0", "server.app:app"]
