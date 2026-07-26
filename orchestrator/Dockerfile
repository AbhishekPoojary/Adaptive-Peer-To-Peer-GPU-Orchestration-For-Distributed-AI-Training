FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY orchestrator ./orchestrator
# agent/ and installer/ are not imported by the running server; they are
# served as static/bundled artifacts by orchestrator/api/installer.py so a new
# peer can `curl .../install.sh | bash` and download this exact agent build
# without the repo needing a public git remote (M7).
COPY agent ./agent
COPY installer ./installer

RUN pip install --no-cache-dir ".[server]"

COPY orchestrator/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

EXPOSE 8000

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["uvicorn", "orchestrator.main:app", "--host", "0.0.0.0", "--port", "8000"]
