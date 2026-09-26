FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies first (layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY app/ ./app/

# Create a non-root user for security
RUN useradd -r -s /bin/false botuser && \
    mkdir -p /app/data && \
    chown -R botuser:botuser /app

USER botuser

# Environment defaults (overridden via docker-compose or -e flags)
ENV LOG_LEVEL=INFO
ENV TEST_MODE=false
ENV DATABASE_URL=sqlite+aiosqlite:///./data/public_mint_bot.db

# Health check — verifies the Python process is alive
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
    CMD python -c "import sys; sys.exit(0)"

# Entry point
CMD ["python", "-m", "app.main"]
