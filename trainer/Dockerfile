# Real trainer image (M4). Based on an official PyTorch CUDA *runtime* image
# (not -devel: no compiler toolchain needed, smaller image) whose bundled
# CUDA 12.4 userspace is backward-compatible with the host's driver 610.62 —
# confirmed working with `docker run --gpus all ... nvidia-smi` on the target
# RTX 3050 (see the M4 verification log). torchvision is pinned to the release
# matching the image's bundled torch==2.5.1.
FROM pytorch/pytorch:2.5.1-cuda12.4-cudnn9-runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TORCH_DATA_CACHE=/data-cache

WORKDIR /app

# torchvision matches the image's bundled torch==2.5.1; boto3 (M6, ADR-006) is
# the S3 client used to write/read checkpoints to MinIO.
RUN pip install --no-cache-dir torchvision==0.20.1 boto3==1.35.99

# Everything train.py imports flatly must sit next to it: the container runs
# `python train.py`, not a package import, so `import checkpoint` and
# `from dataset_spec import ...` resolve by filename in /app and nothing else.
#
# A module missing here does not fail the build. It fails at the first line of
# every training run, on someone else's machine, as ModuleNotFoundError — which
# is why tests/test_trainer_image.py checks this list against train.py's own
# imports rather than trusting whoever edits one to remember the other.
COPY trainer/train.py ./train.py
COPY trainer/checkpoint.py ./checkpoint.py
COPY trainer/dataset_spec.py ./dataset_spec.py

# /data-cache is a mounted named volume at run time (see ADR-007 launch flags
# in agent/runtime/docker_launcher.py) — not baked into the image — so dataset
# downloads are reused across job runs on the same node.
RUN mkdir -p /data-cache

CMD ["python", "train.py"]
