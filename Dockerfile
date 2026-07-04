FROM python:3.12-slim

WORKDIR /app

# Install system dependencies for pyodbc (SQL tools support)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    g++ \
    unixodbc-dev \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY server.py .

# Run the server
CMD ["python", "server.py"]
