# One image, four Cloud Run services. SERVICE_NAME picks the role at runtime.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# The package is imported as `cognikart.*`, so copy it under that name.
COPY . /app/cognikart/

# Cloud Run injects PORT (default 8080). Single worker: these services are
# I/O-bound and Cloud Run scales by adding instances, not threads.
ENV PORT=8080
EXPOSE 8080
CMD exec uvicorn cognikart.main:app --host 0.0.0.0 --port ${PORT} --workers 1 --no-access-log
