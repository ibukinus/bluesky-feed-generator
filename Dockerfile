FROM python:3.11-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

RUN apt-get update && \
    apt-get install -y --no-install-recommends gcc curl && \
    curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y && \
    rm -rf /var/lib/apt/lists/*
ENV PATH="/root/.cargo/bin:$PATH"

ENV UV_SYSTEM_PYTHON=1
ENV PYTHONUNBUFFERED=1
WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY sudachi.json user.csv ./
RUN DICT_DIR=$(python -c "import sudachidict_core; import os; print(os.path.join(sudachidict_core.DICT_DIR, 'system.dic'))") && \
    SUDACHI_DIR=$(python -c "import sudachipy; import os; print(os.path.join(os.path.dirname(sudachipy.__file__), 'resources'))") && \
    sudachipy ubuild -o "$SUDACHI_DIR/user.dic" -s "$DICT_DIR" user.csv && \
    cp sudachi.json "$SUDACHI_DIR/"

FROM python:3.11-slim AS runner

ENV PYTHONUNBUFFERED=1
WORKDIR /app

COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages/
COPY --from=builder /usr/local/bin/gunicorn /usr/local/bin/gunicorn
COPY . .

EXPOSE 8000
CMD ["gunicorn", "-b", "0.0.0.0", "server.app:app"]
