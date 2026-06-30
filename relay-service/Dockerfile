# Sunday Relay — a single tiny image. The relay is the only hosted component in
# Sunday and deliberately the least powerful one, so its image is correspondingly
# minimal: slim Python base, two pip deps, copy two source files, run.
FROM python:3.12-slim

# Don't buffer stdout/stderr — we want logs to show up immediately in the
# platform's log stream, and we never write .pyc (read-only-friendly image).
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Install deps first so the layer caches across source-only changes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# The whole service is two files.
COPY server.py registry.py ./

# The registry persists to a JSON file; mount a volume here in production so
# self-registered agents survive restarts (RELAY_AGENTS_FILE points at it).
ENV RELAY_AGENTS_FILE=/data/agents.json
VOLUME ["/data"]

# PaaS platforms inject PORT; default to 8787 for local `docker run`.
ENV PORT=8787
EXPOSE 8787

CMD ["python", "server.py"]
