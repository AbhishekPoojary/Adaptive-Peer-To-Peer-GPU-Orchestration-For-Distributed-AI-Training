"""The adaptive scheduler's ablation variants switch off exactly one term."""

from __future__ import annotations

from orchestrator.core.config import get_settings
from orchestrator.schedulers.adaptive import ABLATIONS, adaptive_weights
from orchestrator.schedulers.registry import is_registered


def test_each_variant_zeroes_only_its_own_term() -> None:
    settings = get_settings()
    full = adaptive_weights("adaptive", settings)
    assert full == (
        settings.scheduler_alpha_load,
        settings.scheduler_beta_reliability,
        settings.scheduler_gamma_latency,
    )
    assert all(w > 0 for w in full)
    for index, name in enumerate(
        ("adaptive_no_load", "adaptive_no_reliability", "adaptive_no_latency")
    ):
        weights = adaptive_weights(name, settings)
        assert weights[index] == 0.0
        assert [w for i, w in enumerate(weights) if i != index] == [
            w for i, w in enumerate(full) if i != index
        ]


def test_every_variant_can_be_requested_by_a_job() -> None:
    assert all(is_registered(name) for name in ABLATIONS)
