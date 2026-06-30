# Sunday API — a single tiny image, same shape as the relay's. The hosted
# backbone is deliberately small: slim Python base, three pip deps, copy two
# source files, run.
FROM python:3.12-slim

# Don't buffer stdout/stderr — we want logs in the platform stream immediately,
# and we never write .pyc (read-only-friendly image).
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Install deps first so the layer caches across source-only changes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# The whole service is two files.
COPY server.py db.py ./

# The account store is a SQLite file; mount a volume here in production so
# accounts/usage survive restarts (SUNDAY_DB points at it). Without the volume
# every restart would orphan every account — the opposite of "stable identity."
ENV SUNDAY_DB=/data/sunday.db
VOLUME ["/data"]

# PaaS platforms inject PORT; default to 8080 for local `docker run`.
ENV PORT=8080
EXPOSE 8080

CMD ["python", "server.py"]
