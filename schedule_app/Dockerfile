# Employee Scheduler container image.
#
# Runs the Flask app behind gunicorn (a production WSGI server) rather than the
# development server. The SQLite database lives at /data/schedule.db so it can
# be mounted as a volume and survive container restarts.

FROM python:3.12-slim

# Don't buffer stdout/stderr, don't write .pyc files.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    SCHEDULE_DB=/data/schedule.db

WORKDIR /app

# Install dependencies first so this layer caches across code changes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Application code.
COPY . .

# Persisted data (SQLite file) lives here; declared as a volume.
RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 5000

# 2 workers is plenty for a single-manager tool; SQLite handles the low
# write concurrency fine. Adjust --workers if you scale up.
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "2", "--timeout", "60", "app:create_app()"]
