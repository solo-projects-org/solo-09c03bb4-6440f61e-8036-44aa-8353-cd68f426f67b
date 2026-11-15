FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /srv

# Dependencies first for layer caching.
COPY requirements.txt /srv/requirements.txt
RUN pip install --no-cache-dir -r /srv/requirements.txt

# Application code and the seed data set that is imported on first start.
COPY app /srv/app
COPY seed /srv/seed
COPY examples /srv/examples

# The database lives on a volume so the imported data set, the spent budget
# and released answers survive container restarts.
RUN mkdir -p /srv/data && useradd --system --uid 10001 --home /srv svc \
    && chown -R svc:svc /srv/data
USER svc

ENV DATABASE_PATH=/srv/data/followup.db \
    CSV_PATH=/srv/seed/followup.csv \
    TOTAL_BUDGET=1 \
    IMPORT_ON_STARTUP=1

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).status == 200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
