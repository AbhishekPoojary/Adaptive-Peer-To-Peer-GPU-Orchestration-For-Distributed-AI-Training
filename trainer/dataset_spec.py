"""Facts about custom datasets that both ends of the system need to agree on.

Deliberately free of torch. ``trainer/train.py`` owns the training loop and
imports torch to do it, but the orchestrator has to know one thing from it --
the resolution images are reduced to -- and cannot pay for torch to find out.

The constant lives here rather than in ``train.py`` so there is exactly one of
it. Two copies that quietly disagree would not fail: the dashboard would shrink
images to one size, the trainer would resize them again to another, and the only
symptom would be a model that trains on upscaled mush and reports a slightly
disappointing accuracy nobody could explain.
"""

from __future__ import annotations

#: Side length, in pixels, that every custom-dataset image is resized to before
#: the model sees it. 64px is a compromise -- larger than CIFAR's 32 so real
#: photographs keep some detail, small enough to train on a 4 GB laptop card.
#:
#: The dashboard uses this to shrink an archive *before* uploading it, which is
#: sound only because the trainer would discard the extra detail anyway. Raising
#: it makes previously-uploaded datasets the smaller size: they are not wrong,
#: but they cannot supply detail they no longer hold.
CUSTOM_IMAGE_SIZE = 64
