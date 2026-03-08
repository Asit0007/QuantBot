# ── QuantBot Dockerfile ──────────────────────────────────────────
# Multi-stage: one base image, three runnable services.
# Build:  docker-compose build
# Run:    docker-compose up -d

FROM python:3.11-slim AS base
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends gcc curl \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN mkdir -p /app/data

FROM base AS bot
ENV DATA_DIR=/app/data
CMD ["python", "bot.py"]

FROM base AS notifier
ENV DATA_DIR=/app/data
CMD ["python", "notifier.py"]

FROM base AS dashboard
ENV DATA_DIR=/app/data
ENV DASHBOARD_HOST=0.0.0.0
ENV DASHBOARD_PORT=8050
EXPOSE 8050
CMD ["python", "dashboard.py"]
