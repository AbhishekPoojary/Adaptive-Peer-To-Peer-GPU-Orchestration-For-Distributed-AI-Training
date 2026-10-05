FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# Dependencies first, in their own layer, read straight from pyproject.toml.
# Installing them after the source (as this used to) re-ran the whole install
# on every code change -- over ten minutes on a 1 GB cloud server, for an
# update that changed one file. Now this layer is reused until the dependency
# list itself changes.
COPY pyproject.toml ./
RUN python -c "import tomllib; p = tomllib.load(open('pyproject.toml', 'rb'))['project']; print('\n'.join(p['dependencies'] + p['optional-dependencies']['server']))" > /tmp/requirements.txt \
    && pip install --no-cache-dir -r /tmp/requirements.txt

COPY README.md ./
COPY orchestrator ./orchestrator
# agent/, trainer/ and installer/ are not imported by the running server; they
# are served as static/bundled artifacts by orchestrator/api/installer.py so a
# new peer can `curl .../install.sh | bash` and download this exact build
# without the repo needing a public git remote (M7).
#
# trainer/ must be here (ADR-007 addendum): a peer running unsandboxed executes
# train.py directly instead of pulling the trainer image, and the agent bundle
# is the only way it gets that file.
COPY agent ./agent
COPY trainer ./trainer
COPY installer ./installer
# The account script, so demo.ps1 can create the first admin inside this
# container on a fresh install -- no Python environment or database URL needed
# on the host.
COPY scripts/create_user.py ./scripts/create_user.py

# Only the project itself: its dependencies are already in the layer above.
RUN pip install --no-cache-dir --no-deps .

COPY orchestrator/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

EXPOSE 8000

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["uvicorn", "orchestrator.main:app", "--host", "0.0.0.0", "--port", "8000"]
