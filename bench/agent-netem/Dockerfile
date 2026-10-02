# A real agent in a Linux container, so the latency_placement benchmark can add
# real network delay to one peer with `tc netem` (Linux-only). Two of these run
# side by side: identical image, identical VM, so their load is the same by
# construction and the injected delay is the only difference between them.
#
# Not a peer image: it never trains. The scenario runs it with --release-after,
# the agent's test-only hold-and-release path, which claims leases without
# Docker. Built from the repo root:
#   docker build -t gpu-orchestrator-agent-netem -f bench/agent-netem/Dockerfile .
FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends iproute2 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /src
COPY pyproject.toml README.md ./
COPY agent ./agent
COPY trainer ./trainer
RUN pip install --no-cache-dir ".[agent]"

# The scenario passes one shell command: add the delay (if any), then exec the
# agent so it is PID 1's direct child and stops cleanly.
ENTRYPOINT ["/bin/sh", "-c"]
