FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY backend ./backend
COPY bot ./bot
COPY config ./config
COPY lang ./lang

RUN python -m pip install --upgrade pip \
    && python -m pip install -e .

CMD ["python", "-m", "backend.main"]
