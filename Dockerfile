FROM python:3.12-slim

# WeasyPrint runtime deps (rendering email HTML to PDF)
RUN apt-get update && apt-get install -y --no-install-recommends \
      libpango-1.0-0 libpangoft2-1.0-0 fonts-dejavu-core zip \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install deps first for layer caching
COPY pyproject.toml README.md ./
RUN pip install --no-cache-dir -U pip && \
    pip install --no-cache-dir \
      httpx>=0.27 anthropic>=0.40 fastapi>=0.115 "uvicorn[standard]>=0.32" \
      asyncpg>=0.29 redis>=5.0 pydantic>=2.9 python-telegram-bot>=21.6 \
      weasyprint>=63 boto3>=1.35 stripe>=10 pynacl>=1.5 structlog>=24 \
      claude-agent-sdk>=0.1

COPY omnihr_client ./omnihr_client
COPY bot ./bot
COPY tenants ./tenants
COPY ops ./ops
COPY extension ./extension

ENV PYTHONUNBUFFERED=1 \
    PORT=8000

EXPOSE 8000

CMD ["python", "-m", "bot.server"]
