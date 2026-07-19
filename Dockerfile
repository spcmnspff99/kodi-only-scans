FROM python:3.11-slim

ENV TZ=America/Boise

# Install system dependencies required by smbprotocol (Kerberos / GSSAPI headers)
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc \
        libkrb5-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies first (layer-cached)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source
COPY main.py nfo_parser.py smb_walker.py db_ops.py ./
COPY templates/ templates/

# Persistent volume for SQLite scan history
VOLUME ["/data"]

EXPOSE 8080

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
