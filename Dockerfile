FROM python:3.12-slim

WORKDIR /app

# Install Python dependencies first (leverages Docker layer caching)
COPY src/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source code
COPY src/ .

# Security hardening: Run container as non-root user (least privilege)
RUN useradd -m -u 10001 appuser && chown -R appuser:appuser /app
USER appuser

# Expose port 8000 for Azure Container Apps Ingress
EXPOSE 8000

# Start FastAPI with Uvicorn
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
