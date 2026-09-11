FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
RUN groupadd --gid 10001 calendar && useradd --uid 10001 --gid calendar --no-create-home calendar
COPY requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock
COPY pyproject.toml README.md LICENSE ./
COPY calendar_sync ./calendar_sync
RUN pip install --no-cache-dir --no-deps . && mkdir -p /app/data && chown calendar:calendar /app/data
USER calendar
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import os,urllib.request; from urllib.parse import urlsplit; req=urllib.request.Request('http://127.0.0.1:8000/healthz',headers={'Host':urlsplit(os.environ['BASE_URL']).netloc}); urllib.request.urlopen(req,timeout=4)"
CMD ["uvicorn", "calendar_sync.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--no-access-log", "--no-proxy-headers"]
