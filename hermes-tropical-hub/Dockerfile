FROM python:3.11-slim

WORKDIR /app

# Install dependencies first (layer cache)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code and configuration
COPY main.py .
COPY SOUL.md .
COPY config.yaml .
COPY skills/ ./skills/

# Create persistent data directory
RUN mkdir -p /app/data/hermes_memory

CMD ["python", "main.py"]
