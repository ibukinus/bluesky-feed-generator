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
COPY scripts/build_user_dict.py scripts/
RUN uv run python scripts/build_user_dict.py

FROM python:3.11-slim AS runner

ENV PYTHONUNBUFFERED=1
WORKDIR /app

COPY --from=builder /app/.venv/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages/
COPY . .

EXPOSE 8000
CMD ["python", "-m", "gunicorn", "-b", "0.0.0.0", "server.app:app"]
