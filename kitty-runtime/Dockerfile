FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    KITTY_PROJECT_ROOT=/app/kitty-runtime \
    KITTY_STATE_DIR=/data/kitty

WORKDIR /app

COPY . /app
RUN pip install --no-cache-dir -r kitty-runtime/requirements.lock \
    && pip install --no-cache-dir --no-deps ./kitty-runtime \
    && useradd --create-home --uid 10001 kitty \
    && mkdir -p /data/kitty \
    && chown -R kitty:kitty /data

USER kitty
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/ready', timeout=3)"

CMD ["sh", "-c", "uvicorn kitty.server:create_app --factory --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers"]
