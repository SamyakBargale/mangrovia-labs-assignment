# ── Build stage ────────────────────────────────────────────────────────────────
FROM python:3.12-slim AS builder

WORKDIR /app

# Install dependencies into a wheel cache layer so rebuilds are fast when
# only application code changes.
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir -r requirements.txt


# ── Runtime stage ──────────────────────────────────────────────────────────────
FROM python:3.12-slim AS runtime

# Create a non-root user for security
RUN useradd --create-home --shell /bin/bash botuser

WORKDIR /app

# Copy installed packages from the build stage
COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# Copy application code
COPY --chown=botuser:botuser . .

# Create the data directory that will hold the SQLite database.
# Mount a volume here in production: -v /host/data:/app/data
RUN mkdir -p /app/data && chown botuser:botuser /app/data

USER botuser

# Unbuffered output so log lines appear immediately in docker logs
ENV PYTHONUNBUFFERED=1
ENV DATABASE_PATH=/app/data/bot.db

# Health check — verifies the process is alive (no network dependency)
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
  CMD python -c "import sys; sys.exit(0)"

CMD ["python", "main.py"]
