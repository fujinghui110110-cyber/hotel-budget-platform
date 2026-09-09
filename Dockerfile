FROM python:3.13-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DJANGO_SETTINGS_MODULE=config.settings \
    DATABASE_PATH=/var/lib/budget/db.sqlite3 \
    BUDGET_STORAGE_ROOT=/var/lib/budget/storage \
    SOFFICE_BIN=/usr/bin/soffice \
    HOME=/home/budget

RUN apt-get update \
    && apt-get install --no-install-recommends -y \
        ca-certificates \
        fontconfig \
        fonts-noto-cjk \
        fonts-wqy-zenhei \
        libreoffice-calc \
        tzdata \
    && fc-cache -f \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./requirements.txt
RUN python -m pip install --upgrade pip \
    && python -m pip install -r requirements.txt

COPY manage.py ./manage.py
COPY config ./config
COPY budgeting ./budgeting
COPY templates ./templates
COPY static ./static

COPY artifacts/v3/2027/平台标准预算模板_V3_2027.xlsx artifacts/v3/2027/平台标准预算模板_V3_2027.xlsx
COPY artifacts/v3/2027/template_manifest_V3_2027.json artifacts/v3/2027/template_manifest_V3_2027.json
COPY artifacts/v3/2028/平台标准预算模板_V3_2028.xlsx artifacts/v3/2028/平台标准预算模板_V3_2028.xlsx
COPY artifacts/v3/2028/template_manifest_V3_2028.json artifacts/v3/2028/template_manifest_V3_2028.json

COPY deploy/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

RUN useradd --create-home --home-dir /home/budget --uid 10001 --shell /usr/sbin/nologin budget \
    && mkdir -p /var/lib/budget/storage /app/staticfiles \
    && chown -R budget:budget /app /var/lib/budget /home/budget \
    && chmod 0755 /usr/local/bin/docker-entrypoint.sh

USER budget

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4)"]

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "1", "--threads", "1", "--timeout", "300"]
