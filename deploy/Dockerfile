# Sunday daemon image. Build context is the repo root.
FROM python:3.11-slim

WORKDIR /app

# Build deps for the wheel; curl for the healthcheck.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl \
 && rm -rf /var/lib/apt/lists/*

# Install the package. Copy only what hatchling needs to build the wheel,
# then the source, so layer caching survives source-only edits.
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir ".[devices]"

# Per-user state (memory db, sessions, skills, credentials) lives here;
# mount a volume so it survives container rebuilds.
ENV SUNDAY_HOME=/data \
    SUNDAY_HOST=0.0.0.0 \
    SUNDAY_PORT=8765
VOLUME ["/data"]
EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD curl -fsS http://127.0.0.1:8765/v1/health || exit 1

CMD ["sunday", "start"]
