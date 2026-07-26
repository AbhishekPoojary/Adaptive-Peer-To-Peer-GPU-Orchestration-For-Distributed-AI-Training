"""Pure unit tests for the trainer stdout metric/final line parser (M4).

No GPU, Docker, or network needed — ``agent.runtime.metrics`` only parses
strings. Covers the real shape ``trainer/train.py`` emits, plus the negative
cases: ordinary log lines, malformed JSON, wrong types, and booleans
masquerading as numbers must never be coerced into a fabricated metric.
"""

from __future__ import annotations

import json

from agent.runtime.metrics import parse_final_line, parse_metric_line, parse_resume_line


def test_parses_a_real_metric_line() -> None:
    line = json.dumps(
        {"type": "metric", "epoch": 3, "loss": 0.4123, "test_accuracy": 0.552, "step": 780}
    )
    parsed = parse_metric_line(line)
    assert parsed == {"epoch": 3, "loss": 0.4123, "test_accuracy": 0.552, "step": 780}


def test_metric_line_without_step_defaults_to_none() -> None:
    # A pre-M6 metric line (no step) is still valid; step is None.
    line = json.dumps({"type": "metric", "epoch": 3, "loss": 0.4123, "test_accuracy": 0.552})
    parsed = parse_metric_line(line)
    assert parsed == {"epoch": 3, "loss": 0.4123, "test_accuracy": 0.552, "step": None}


def test_metric_line_test_accuracy_may_be_null() -> None:
    line = json.dumps({"type": "metric", "epoch": 1, "loss": 1.5, "test_accuracy": None})
    parsed = parse_metric_line(line)
    assert parsed == {"epoch": 1, "loss": 1.5, "test_accuracy": None, "step": None}


def test_metric_step_must_be_a_real_int() -> None:
    assert (
        parse_metric_line(
            json.dumps({"type": "metric", "epoch": 1, "loss": 0.5, "step": "780"})
        )
        is None
    )
    assert (
        parse_metric_line(
            json.dumps({"type": "metric", "epoch": 1, "loss": 0.5, "step": True})
        )
        is None
    )


def test_parses_a_real_resume_line() -> None:
    line = json.dumps(
        {
            "type": "resume",
            "from_step": 1840,
            "from_epoch": 3,
            "checkpoint_key": "checkpoints/job-1/e003-s00001840-abc.pt",
        }
    )
    parsed = parse_resume_line(line)
    assert parsed == {
        "from_step": 1840,
        "from_epoch": 3,
        "checkpoint_key": "checkpoints/job-1/e003-s00001840-abc.pt",
    }


def test_resume_line_rejects_non_resume_and_bad_types() -> None:
    assert parse_resume_line(json.dumps({"type": "metric", "epoch": 1, "loss": 0.5})) is None
    assert (
        parse_resume_line(json.dumps({"type": "resume", "from_step": "x", "from_epoch": 3}))
        is None
    )
    # A metric line must never be mistaken for a resume line and vice-versa.
    metric = json.dumps({"type": "metric", "epoch": 1, "loss": 0.5, "step": 10})
    assert parse_resume_line(metric) is None
    resume = json.dumps({"type": "resume", "from_step": 5, "from_epoch": 1, "checkpoint_key": "k"})
    assert parse_metric_line(resume) is None


def test_parses_a_real_final_line() -> None:
    line = json.dumps(
        {
            "type": "final",
            "epochs_completed": 5,
            "final_loss": 0.312,
            "final_test_accuracy": 0.601,
            "device": "cuda",
        }
    )
    parsed = parse_final_line(line)
    assert parsed == {
        "epochs_completed": 5,
        "final_loss": 0.312,
        "final_test_accuracy": 0.601,
        "device": "cuda",
    }


def test_final_line_accepts_cpu_device() -> None:
    line = json.dumps(
        {
            "type": "final",
            "epochs_completed": 1,
            "final_loss": 2.0,
            "final_test_accuracy": 0.1,
            "device": "cpu",
        }
    )
    parsed = parse_final_line(line)
    assert parsed is not None
    assert parsed["device"] == "cpu"


def test_ordinary_log_line_is_not_a_metric() -> None:
    assert parse_metric_line("[train] epoch 1/5 done in 7.6s: train_loss=0.37") is None
    assert parse_final_line("[train] training complete") is None


def test_progress_bar_noise_is_not_a_metric() -> None:
    assert parse_metric_line("Downloading http://yann.lecun.com/... 45%|####|") is None


def test_malformed_json_is_not_a_metric() -> None:
    assert parse_metric_line('{"type": "metric", "epoch": 1,') is None
    assert parse_final_line("not json at all") is None


def test_valid_json_but_wrong_type_key_is_ignored() -> None:
    line = json.dumps({"type": "log", "epoch": 1, "loss": 0.5, "test_accuracy": 0.5})
    assert parse_metric_line(line) is None


def test_metric_missing_required_field_is_rejected() -> None:
    line = json.dumps({"type": "metric", "epoch": 1, "test_accuracy": 0.5})
    assert parse_metric_line(line) is None


def test_metric_with_wrong_types_is_rejected() -> None:
    assert parse_metric_line(json.dumps({"type": "metric", "epoch": "1", "loss": 0.5})) is None
    assert parse_metric_line(json.dumps({"type": "metric", "epoch": 1, "loss": "bad"})) is None


def test_booleans_are_never_coerced_into_numbers() -> None:
    """bool is an int subclass in Python; True/False must never pass as a
    real epoch/loss/accuracy value."""
    line = json.dumps({"type": "metric", "epoch": True, "loss": 0.5, "test_accuracy": 0.5})
    assert parse_metric_line(line) is None

    line2 = json.dumps({"type": "metric", "epoch": 1, "loss": True, "test_accuracy": 0.5})
    assert parse_metric_line(line2) is None


def test_final_line_rejects_unknown_device() -> None:
    line = json.dumps(
        {
            "type": "final",
            "epochs_completed": 1,
            "final_loss": 0.1,
            "final_test_accuracy": 0.9,
            "device": "tpu",
        }
    )
    assert parse_final_line(line) is None


def test_final_line_missing_field_is_rejected() -> None:
    line = json.dumps({"type": "final", "epochs_completed": 1, "device": "cpu"})
    assert parse_final_line(line) is None


def test_json_that_is_not_an_object_is_rejected() -> None:
    assert parse_metric_line("[1, 2, 3]") is None
    assert parse_metric_line("42") is None
    assert parse_final_line('"just a string"') is None
