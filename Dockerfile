FROM python:3.12-slim

# Enterprise optimization: Lean runtime, unbuffered streaming logs for Azure Container Apps
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PORT=8000

WORKDIR /app

# Install dependencies first (leverages Docker layer cache)
COPY src/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source code
COPY src/ .

# Security hardening: Non-root user with dedicated UID/GID (least privilege)
RUN useradd -m -u 10001 -s /bin/sh appuser && chown -R appuser:appuser /app
USER appuser

# Container runtime healthcheck probe for orchestrator reliability
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

EXPOSE 8000

# Production Uvicorn invocation with reverse proxy header support
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips=*"]
