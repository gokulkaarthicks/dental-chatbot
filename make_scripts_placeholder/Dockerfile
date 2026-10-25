FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY server.py ./

# SQLite lives on a mounted volume so ordinary restarts preserve records.
RUN mkdir -p /app/data
VOLUME ["/app/data"]

EXPOSE 8000

# Startup creates missing tables and seeds an empty database (see app.main).
CMD ["python", "server.py"]
