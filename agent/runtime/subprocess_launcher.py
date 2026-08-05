"""Run the trainer as a child process instead of a container (ADR-007 addendum).

For a peer that has no Docker — and no NVIDIA Container Toolkit, and on Windows
no WSL2 — this is the difference between contributing a GPU and not. It is
**opt-in**: the agent requires ``--allow-unsandboxed`` and prints exactly what
is being given up first. Docker, when present, remains the default and is
unchanged.

The design is deliberately a second *backend*, not a second subsystem.
:class:`TrainerProcess` implements the same ``SupportsLogs`` protocol
``docker_launcher`` defines, so ``stream_container_logs``, ``wait_for_exit``,
``stop_container`` and the whole of ``runtime.execution`` — metric parsing,
WebSocket streaming, lease renewal, epoch fencing, cancellation, abandonment —
work against it verbatim. One execution contract, two ways to start a process.

The trainer's own interface makes this cheap: it is configured entirely by
environment variables and reports entirely on stdout. The container was always
the *isolation*, never the interface.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psutil

logger = logging.getLogger("agent.runtime.subprocess_launcher")

#: Where the agent bundle puts trainer/train.py relative to the installed
#: package root. The installer extracts agent/ and trainer/ side by side.
_PACKAGE_ROOT = Path(__file__).resolve().parents[2]


class TrainerSourceNotFoundError(Exception):
    """``trainer/train.py`` is not on this machine, so nothing can be run.

    Raised loudly rather than falling back to anything: a peer that cannot find
    the trainer must fail its lease honestly, not appear to run and report
    nothing.
    """


def find_trainer_script(explicit: str | None = None) -> Path:
    """Locate ``train.py``. Raises :class:`TrainerSourceNotFoundError` if absent.

    Searched in order: an explicit path, ``TRAINER_SCRIPT``, ``trainer/`` beside
    the installed agent package (where pip puts it — see the ``trainer*`` entry
    in pyproject's package list), then the extracted agent bundle's source
    directory, which is where the installer leaves a copy.

    The last location is a genuine fallback, not belt-and-braces: the first real
    peer failed every lease because ``trainer`` was not in the installed package
    set, so only the extracted copy existed. Looking in both means a peer whose
    install is half-right still works, and the error below names every path
    tried so the next failure is diagnosable from the message alone.
    """
    home = Path.home()
    candidates = [
        Path(explicit) if explicit else None,
        Path(os.environ["TRAINER_SCRIPT"]) if os.environ.get("TRAINER_SCRIPT") else None,
        _PACKAGE_ROOT / "trainer" / "train.py",
        home / ".gpu-orchestrator-agent-src" / "trainer" / "train.py",
    ]
    tried = [str(c) for c in candidates if c is not None]
    for candidate in candidates:
        if candidate is not None and candidate.is_file():
            return candidate
    raise TrainerSourceNotFoundError(
        "could not find trainer/train.py on this machine. Looked in: "
        + "; ".join(tried)
        + ". Re-run the installer, or set TRAINER_SCRIPT to a checkout's "
        "trainer/train.py."
    )


class TrainerProcess:
    """A running trainer child process, wearing ``docker_launcher``'s interface.

    Implements ``logs()``, ``wait()``, ``stop()`` and ``id`` so every consumer
    written against a container works against this unchanged. ``logs()``
    returns byte chunks from one already-separate pipe, which is simpler than
    the container case — subprocess gives us stdout and stderr as distinct
    handles, so nothing has to be demultiplexed.
    """

    def __init__(self, process: subprocess.Popen[bytes], *, memory_limit_bytes: int | None):
        self._process = process
        self._memory_limit_bytes = memory_limit_bytes
        self._killed_for_memory = False
        self._guard: threading.Thread | None = None
        if memory_limit_bytes:
            self._guard = threading.Thread(target=self._watch_memory, daemon=True)
            self._guard.start()

    @property
    def id(self) -> str:
        return f"pid-{self._process.pid}"

    @property
    def killed_for_memory(self) -> bool:
        """True if the guard killed this process for exceeding the limit.

        Callers use it to report a truthful reason rather than a bare exit code
        the peer cannot interpret.
        """
        return self._killed_for_memory

    def logs(self, **kwargs: Any) -> Iterator[bytes]:
        """Yield raw chunks from stdout or stderr, whichever was requested."""
        want_stdout = bool(kwargs.get("stdout", False))
        pipe = self._process.stdout if want_stdout else self._process.stderr
        if pipe is None:
            return iter(())
        return iter(pipe.readline, b"")

    def wait(self) -> dict[str, Any]:
        """Block until the child exits; report its real exit code."""
        return {"StatusCode": self._process.wait()}

    def stop(self, *, timeout: int = 10, **_kwargs: Any) -> None:
        """Terminate the child, escalating to a kill if it will not go.

        Kills the whole process tree: torch's DataLoader workers are children,
        and terminating only the parent would leave them holding the GPU — the
        exact orphan problem M7.1b fixed on the container path.
        """
        self._terminate_tree(force=False)
        try:
            self._process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            logger.warning("trainer %s ignored terminate; killing", self.id)
            self._terminate_tree(force=True)

    def _terminate_tree(self, *, force: bool) -> None:
        try:
            parent = psutil.Process(self._process.pid)
        except psutil.NoSuchProcess:
            return
        procs = parent.children(recursive=True) + [parent]
        for proc in procs:
            try:
                proc.kill() if force else proc.terminate()
            except psutil.NoSuchProcess:
                continue
            except psutil.Error as exc:  # noqa: PERF203 - best effort per process
                logger.warning("could not signal %s: %s", proc, exc)

    def _tree_rss_bytes(self) -> int:
        """Resident memory of the child and everything it spawned."""
        try:
            parent = psutil.Process(self._process.pid)
            total = parent.memory_info().rss
            for child in parent.children(recursive=True):
                try:
                    total += child.memory_info().rss
                except psutil.Error:
                    continue
            return int(total)
        except psutil.Error:
            return 0

    def _watch_memory(self) -> None:
        """Poll the process tree's RSS and kill it if it exceeds the limit.

        A *mitigation*, not the cgroup limit a container gets (ADR-007
        addendum): this is sampled, so a fast allocation spike can overshoot
        between polls, and a process that escapes the tree is not seen. It
        exists because the alternative on this path is no limit at all, and
        catching a runaway training loop within a second is worth having on
        somebody else's laptop.
        """
        assert self._memory_limit_bytes is not None
        while self._process.poll() is None:
            rss = self._tree_rss_bytes()
            if rss > self._memory_limit_bytes:
                logger.error(
                    "trainer %s exceeded the memory limit (%.2f GB > %.2f GB); killing it",
                    self.id,
                    rss / 1024**3,
                    self._memory_limit_bytes / 1024**3,
                )
                self._killed_for_memory = True
                self._terminate_tree(force=True)
                return
            time.sleep(1.0)


def parse_memory_limit(limit: str) -> int | None:
    """Parse a Docker-style size ('6g', '512m') into bytes. None disables.

    Shares the launch config's format so the same ``--trainer-memory-limit``
    value means the same thing on both execution paths.
    """
    text = limit.strip().lower()
    if not text or text in {"0", "none"}:
        return None
    units = {"k": 1024, "m": 1024**2, "g": 1024**3}
    if text[-1] in units:
        return int(float(text[:-1]) * units[text[-1]])
    return int(text)


def launch_trainer_process(
    *,
    environment: dict[str, str],
    data_cache_dir: Path,
    memory_limit_bytes: int | None,
    trainer_script: Path | None = None,
) -> TrainerProcess:
    """Start ``python train.py`` with the trainer's real environment contract.

    ``environment`` is the exact dict the container path builds
    (``docker_launcher.build_run_kwargs``), so the two backends cannot drift on
    how the trainer is configured — there is one source of truth for that.
    Only ``TORCH_DATA_CACHE`` is overridden, because the container's
    ``/data-cache`` mount does not exist here.
    """
    script = trainer_script or find_trainer_script()
    data_cache_dir.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    env.update(environment)
    env["TORCH_DATA_CACHE"] = str(data_cache_dir)
    # The trainer's stdout is a machine-readable protocol; a buffered pipe would
    # hold metric lines back until the process ended, turning live progress into
    # a single dump at the end.
    env["PYTHONUNBUFFERED"] = "1"

    logger.info("launching trainer subprocess: %s %s", sys.executable, script)
    process = subprocess.Popen(
        [sys.executable, str(script)],
        cwd=str(script.parent),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
    )
    return TrainerProcess(process, memory_limit_bytes=memory_limit_bytes)
