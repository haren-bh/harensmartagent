FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    GOOGLE_API_USE_CLIENT_CERTIFICATE=false \
    PORT=8080

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY . .

EXPOSE 8080

# Single worker + 8 threads avoids duplicate in-process schedulers while serving concurrent admin/cron requests
CMD exec gunicorn --bind :$PORT --workers 1 --threads 8 --timeout 300 app:app
