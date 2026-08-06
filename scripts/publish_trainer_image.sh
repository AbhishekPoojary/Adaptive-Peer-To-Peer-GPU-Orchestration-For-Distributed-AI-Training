#!/usr/bin/env bash
# Publish the trainer image so peers can pull it instead of building it.
#
# Usage:
#   docker login                                  # once, interactively
#   bash scripts/publish_trainer_image.sh <your-dockerhub-username>
#   # or a full reference for another registry:
#   bash scripts/publish_trainer_image.sh ghcr.io/<user>
#
# Why this exists
# ---------------
# The trainer image is built locally and, until it is published, exists only on
# the machine that built it. A peer WITH Docker then fails every claimed lease
# with a registry 404 that reads like an auth error:
#
#   pull access denied for gpu-orchestrator-trainer, repository does not exist
#
# A real peer hit exactly that. The agent and installer now detect it and offer
# a local build or the unsandboxed path (ADR-007 addendum), but both are poor
# consolation: the build pulls a ~7 GB CUDA base on every peer, and the
# unsandboxed path gives up container isolation. Publishing once removes the
# choice entirely.
#
# After publishing, point the orchestrator at the published reference:
#
#   TRAINER_IMAGE=<user>/gpu-orchestrator-trainer:latest  (deploy/.env)
#
# The orchestrator substitutes that name into the installers it serves, so
# every peer picks it up automatically — nobody has to be told.

set -euo pipefail

LOCAL_IMAGE="gpu-orchestrator-trainer:latest"
NAME="gpu-orchestrator-trainer"
TAG="${TAG:-latest}"

if [ $# -lt 1 ]; then
    echo "usage: $0 <dockerhub-username | registry/namespace>" >&2
    echo "example: $0 abhishek           -> abhishek/$NAME:$TAG" >&2
    echo "example: $0 ghcr.io/abhishek   -> ghcr.io/abhishek/$NAME:$TAG" >&2
    exit 2
fi

NAMESPACE="${1%/}"
TARGET="$NAMESPACE/$NAME:$TAG"

echo "[publish] local image : $LOCAL_IMAGE"
echo "[publish] target      : $TARGET"
echo

# Refuse to publish something that does not exist rather than pushing a stale
# or half-built image.
if ! docker image inspect "$LOCAL_IMAGE" >/dev/null 2>&1; then
    echo "[publish] ERROR: $LOCAL_IMAGE is not built on this machine." >&2
    echo "[publish] Build it first:" >&2
    echo "[publish]   docker build -t $LOCAL_IMAGE -f trainer/Dockerfile ." >&2
    exit 1
fi

# A push to a registry you are not logged into fails with a confusing 401 after
# uploading nothing; check first and say so plainly.
if ! docker system info 2>/dev/null | grep -qi "username"; then
    echo "[publish] NOTE: no logged-in Docker account detected."
    echo "[publish] If the push fails with 'denied' or 'unauthorized', run:"
    echo "[publish]   docker login          # Docker Hub"
    echo "[publish]   docker login ghcr.io  # GitHub Container Registry"
    echo
fi

echo "[publish] tagging..."
docker tag "$LOCAL_IMAGE" "$TARGET"

echo "[publish] pushing (this uploads several GB the first time)..."
docker push "$TARGET"

echo
echo "[publish] done: $TARGET"
echo
echo "Next, so every peer uses it automatically:"
echo "  1. add to deploy/.env:   TRAINER_IMAGE=$TARGET"
echo "  2. docker compose -f deploy/compose.yaml up -d --force-recreate orchestrator"
echo
echo "Peers with Docker will now pull the image instead of failing or building"
echo "it. Make the repository PUBLIC in the registry UI, or peers will get a"
echo "401 and be no better off than before."
