FROM python:3.11-slim

WORKDIR /app

# System dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    libpq-dev \
    curl \
    jq \
    && rm -rf /var/lib/apt/lists/*

# Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Application
COPY . .

# Create directories
RUN mkdir -p logs data/exports

# Non-root user
RUN useradd -m -s /bin/bash cryptotrader \
    && chown -R cryptotrader:cryptotrader /app
USER cryptotrader

# Expose API port
EXPOSE 8000

# Health check
HEALTHCHECK --interval=30s --timeout=10s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

# Default: run API server
CMD ["python", "main.py", "--task", "api"]
