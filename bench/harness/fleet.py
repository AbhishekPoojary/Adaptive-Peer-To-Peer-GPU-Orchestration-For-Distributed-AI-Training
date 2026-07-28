"""Real agent processes for a benchmark run.

These are genuine ``python -m agent`` subprocesses, each with its own state
directory and therefore its own enrolled identity, keypair, and lease history.
They are not simulated nodes: they heartbeat with real psutil/NVML telemetry,
claim real leases, and launch real trainer containers.

What they are *not* is independent machines — see ADR-013. Co-located agents
observe the same physical CPU and GPU, so their reported load is identical by
construction. That is why the benchmark measures the reliability term and
records ``load_term_differentiable: false`` in every artifact.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from bench.harness.client import BenchClient

logger = logging.getLogger("bench.fleet")

_REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclass
class AgentProcess:
    """One running agent subprocess and the node identity it enrolled as."""

    label: str
    state_dir: Path
    process: subprocess.Popen[bytes]
    log_path: Path
    node_id: str | None = None
    node_name: str | None = None
    #: The hostname this agent really reported at enrollment. Distinct agents on
    #: one laptop all report the same one, which is exactly how the harness
    #: detects a single-host fleet and marks the load/latency terms untested.
    hostname: str | None = None

    @property
    def alive(self) -> bool:
        return self.process.poll() is None

    def stop(self, *, timeout: float = 10.0) -> None:
        """Stop this agent gracefully, escalating to a kill if it will not go."""
        if not self.alive:
            return
        self.process.terminate()
        try:
            self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            logger.warning("agent %s ignored terminate; killing", self.label)
            self.process.kill()
            with contextlib.suppress(subprocess.TimeoutExpired):
                self.process.wait(timeout=timeout)


@dataclass
class Fleet:
    """A set of real agent processes started for one benchmark run."""

    client: BenchClient
    workdir: Path
    orchestrator_url: str
    agents: list[AgentProcess] = field(default_factory=list)

    async def start_agent(self, label: str) -> AgentProcess:
        """Enroll and start one real agent, waiting until it is ONLINE.

        Each agent gets a fresh state directory so it enrolls as a genuinely
        new node rather than reusing an identity from a previous run — which
        would carry that run's lease history into this one's measurements.
        """
        before = {n["id"] for n in await self.client.nodes()}

        state_dir = self.workdir / f"agent-{label}"
        if state_dir.exists():
            shutil.rmtree(state_dir)
        state_dir.mkdir(parents=True)

        token = await self.client.mint_enrollment_token(created_by=f"bench-{label}")
        log_path = self.workdir / f"agent-{label}.log"

        process = self._spawn(state_dir, log_path, enrollment_token=token)
        agent = AgentProcess(
            label=label, state_dir=state_dir, process=process, log_path=log_path
        )
        self.agents.append(agent)

        node = await self._await_enrollment(agent, before=before)
        agent.node_id = str(node["id"])
        agent.node_name = str(node["name"])
        agent.hostname = str(node.get("hardware", {}).get("hostname", ""))
        logger.info(
            "agent %s enrolled as %s (%s)", label, agent.node_name, agent.node_id[:8]
        )
        return agent

    async def _await_enrollment(
        self, agent: AgentProcess, *, before: set[str], timeout_seconds: float = 90.0
    ) -> dict[str, Any]:
        """Wait for this agent's own node row to appear ONLINE.

        Identified by diffing against the node ids that existed before it
        started, so a concurrently-enrolling agent cannot be mistaken for this
        one.
        """
        started = time.monotonic()
        while True:
            if not agent.alive:
                tail = agent.log_path.read_text(encoding="utf-8", errors="replace")[-2000:]
                raise RuntimeError(
                    f"agent {agent.label} exited during enrollment "
                    f"(code {agent.process.returncode}). Log tail:\n{tail}"
                )
            for node in await self.client.nodes():
                if node["id"] not in before and node["status"] == "ONLINE":
                    return node
            if time.monotonic() - started > timeout_seconds:
                tail = agent.log_path.read_text(encoding="utf-8", errors="replace")[-2000:]
                raise TimeoutError(
                    f"agent {agent.label} did not come ONLINE within "
                    f"{timeout_seconds:.0f}s. Log tail:\n{tail}"
                )
            await asyncio.sleep(1.0)

    def _spawn(
        self, state_dir: Path, log_path: Path, *, enrollment_token: str | None = None
    ) -> subprocess.Popen[bytes]:
        """Launch an agent process against ``state_dir``.

        ``enrollment_token`` is passed only on a node's very first start; a
        restart deliberately omits it so the agent re-authenticates with the
        keypair already in its state directory and comes back as the same node,
        carrying its lease history with it.
        """
        argv = [
            sys.executable,
            "-m",
            "agent",
            "--orchestrator",
            self.orchestrator_url,
            "--state-dir",
            str(state_dir),
        ]
        if enrollment_token is not None:
            argv += ["--enrollment-token", enrollment_token]
        return subprocess.Popen(
            argv,
            cwd=str(_REPO_ROOT),
            stdout=log_path.open("ab"),
            stderr=subprocess.STDOUT,
        )

    async def restart_agent(
        self, agent: AgentProcess, *, timeout_seconds: float = 90.0
    ) -> None:
        """Restart a stopped agent **as the same node**.

        No enrollment token: the identity lives in the state directory, so the
        node keeps its id and — the point of this — its accumulated lease
        history. The benchmark relies on that to give one node a real record of
        failures and another a real record of successes, by running each alone
        while its history is built.
        """
        if agent.alive:
            return
        agent.process = self._spawn(agent.state_dir, agent.log_path)

        started = time.monotonic()
        while True:
            if not agent.alive:
                tail = agent.log_path.read_text(encoding="utf-8", errors="replace")[-2000:]
                raise RuntimeError(
                    f"agent {agent.label} exited on restart "
                    f"(code {agent.process.returncode}). Log tail:\n{tail}"
                )
            for node in await self.client.nodes():
                if node["id"] == agent.node_id and node["status"] == "ONLINE":
                    logger.info("agent %s back online as %s", agent.label, agent.node_name)
                    return
            if time.monotonic() - started > timeout_seconds:
                raise TimeoutError(
                    f"agent {agent.label} did not come back ONLINE within "
                    f"{timeout_seconds:.0f}s"
                )
            await asyncio.sleep(1.0)

    async def stop_agent(
        self, agent: AgentProcess, *, await_offline: bool = False, timeout_seconds: float = 60.0
    ) -> None:
        """Stop one agent, optionally waiting until the orchestrator agrees.

        ``await_offline`` waits for the real φ-accrual detector to declare the
        node OFFLINE (ADR-004), rather than assuming the orchestrator noticed
        the moment the process died — it has not, and a scenario that assumed
        so would place jobs on a node that is gone.
        """
        agent.stop()
        if not await_offline:
            return
        started = time.monotonic()
        while True:
            for node in await self.client.nodes():
                if node["id"] == agent.node_id and node["status"] != "ONLINE":
                    return
            if time.monotonic() - started > timeout_seconds:
                raise TimeoutError(
                    f"node {agent.node_name} was still ONLINE {timeout_seconds:.0f}s "
                    f"after its agent was stopped"
                )
            await asyncio.sleep(1.0)

    def kill_agent(self, agent: AgentProcess) -> None:
        """SIGKILL an agent with no chance to release its lease.

        Distinct from :meth:`stop_agent`, which terminates gracefully. This is
        the honest simulation of a peer *disappearing* — laptop lid closed,
        wifi dropped, process OOM-killed — because the agent gets no
        opportunity to tell the orchestrator anything. Recovery therefore has
        to come from the orchestrator noticing silence on its own (ADR-004's
        φ-accrual detector), which is exactly the path under test.
        """
        if agent.alive:
            agent.process.kill()
            agent.process.wait(timeout=15)

    def stop_all(self) -> None:
        for agent in self.agents:
            agent.stop()

    def hostnames(self) -> list[str]:
        """Reported hostnames, used to decide which score terms were testable.

        Read from what each agent actually reported at enrollment, so this
        reflects the real fleet rather than an assumption about it — the same
        code yields ``load_term_differentiable: true`` unchanged once agents run
        on genuinely separate laptops.
        """
        return [a.hostname or a.label for a in self.agents]


def running_trainer_containers(*, job_id: str | None = None) -> list[str]:
    """Container ids of running trainer containers, optionally for one job.

    Reads real ``docker ps`` output, filtered on the labels
    ``agent/runtime/docker_launcher.py`` stamps at launch. Scenarios that
    induce failures pass ``job_id`` so the kill targets exactly the container
    under test — killing by "whatever trainer is running" would, on a host that
    also runs the operator's own jobs, silently corrupt someone else's run and
    this benchmark's data at the same time.
    """
    filters = ["--filter", "label=gpu-orchestrator.role=trainer"]
    if job_id is not None:
        filters += ["--filter", f"label=gpu-orchestrator.job_id={job_id}"]
    result = subprocess.run(
        ["docker", "ps", *filters, "-q"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        logger.warning("docker ps failed: %s", result.stderr.strip())
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


async def await_trainer_container(
    *, job_id: str, timeout_seconds: float = 180.0, poll_seconds: float = 0.5
) -> str:
    """Wait for this job's trainer container to actually be running.

    Returns its container id. Raises on timeout rather than returning ``None``:
    a failure-injection scenario that "killed" nothing would go on to measure a
    recovery that never needed to happen.
    """
    started = time.monotonic()
    while True:
        containers = running_trainer_containers(job_id=job_id)
        if containers:
            return containers[0]
        if time.monotonic() - started > timeout_seconds:
            raise TimeoutError(
                f"no trainer container appeared for job {job_id[:8]} within "
                f"{timeout_seconds:.0f}s"
            )
        await asyncio.sleep(poll_seconds)


def kill_container(container_id: str) -> bool:
    """``docker kill`` a real container. Returns whether it was killed.

    This is the failure-injection primitive for the whole benchmark suite: a
    real SIGKILL to a real training process, which the agent observes as a real
    nonzero exit and reports as a real lease failure.
    """
    result = subprocess.run(
        ["docker", "kill", container_id],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        logger.warning("docker kill %s failed: %s", container_id[:12], result.stderr.strip())
        return False
    return True
