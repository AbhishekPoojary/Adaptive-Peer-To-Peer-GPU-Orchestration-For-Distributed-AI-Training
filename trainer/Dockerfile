# Placeholder trainer image. Real dependencies (torch matching host CUDA,
# dataset mounts, torchrun elastic config) land with the milestone that
# implements distributed training (ADR-005).
FROM python:3.12-slim

WORKDIR /app
COPY trainer/train.py ./train.py

CMD ["python", "train.py"]
