"""Trainer package.

The training entrypoint (``train.py``) and the checkpoint-to-MinIO helper
(``checkpoint.py``) live here. Inside the trainer container these are imported
by their bare module names (the image copies them next to each other and runs
``python train.py``); from the repo they are importable as ``trainer.train`` /
``trainer.checkpoint`` for unit testing the pure, torch-free checkpoint logic.
"""
