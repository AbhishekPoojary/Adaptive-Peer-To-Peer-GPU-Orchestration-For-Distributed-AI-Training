# Contributing

## Anti-fabrication law

This project makes claims about real hardware, real network conditions, and
real training outcomes. Those claims are worthless if any number in the
pipeline is made up. The following rules are project law, not style
preference. `scripts/check_no_fake_data.sh` enforces what it mechanically
can; the rest is enforced by review.

1. **No fabricated data anywhere**: no `random.*` GPU/latency/reliability
   values, no seeded fake nodes, no mock results in source.
2. **Telemetry comes from hardware** (NVML/psutil) **or is reported as
   `null`** — never substituted with plausible numbers.
3. **Latency is measured RTT (EWMA), never a constant.** Reliability is
   derived from recorded lease history, never assigned.
4. **Training is real** (real datasets, real backprop, measured accuracy).
   No `time.sleep()` standing in for work outside tests.
5. **Fakes/stubs live only in `tests/`** and are named `Fake*` or `Stub*`.
6. **Every benchmark number is machine-written** to a timestamped artifact
   with git SHA + hardware inventory. Nothing hand-typed.

### What this means in practice

- If a value can't be measured yet (hardware absent, telemetry not wired,
  history empty), the correct representation is `null` / "unknown", never a
  plausible-looking placeholder.
- Jitter used for retry/backoff timing is exempt from rule 1's random-API
  ban, but must be annotated inline: `# allow-random: retry backoff jitter`.
  The checker only allows lines carrying that annotation — it does not
  allow-list whole files.
- `Fake*`/`Stub*` test doubles belong in `tests/`. `Mock*` names outside
  `tests/` are also rejected — if you need a real implementation, write one;
  if you need a test double, put it in `tests/`.
- Benchmarks write their own JSON artifact (see `bench/schema.json`) with
  `timestamp_utc`, `git_sha`, and a hardware inventory captured by the bench
  harness at run time. Do not edit a bench report by hand.
- The harness refuses to publish an incomplete measurement: any `null` inside
  `results` aborts the write and leaves no file. A missing artifact honestly
  says "this did not run"; a partial one invites a later reader to average over
  a hole. It also refuses a dirty worktree, because the recorded `git_sha`
  would not describe the code that ran.
- Every artifact carries a `limitations` block naming the score terms the run
  could **not** exercise, computed from the observed fleet rather than
  hardcoded (ADR-013). A reader taking only the JSON must not be able to
  mistake a partial validation for a complete one.

### When a benchmark contradicts the design

Report the result and fix the design. Do not tune parameters until the number
flatters the code.

This is not hypothetical: M9's first run showed the adaptive scheduler placing
jobs on a node with three recorded failures, no better than round-robin. The
cause was a real defect in the scoring normalization that had shipped since M3
and passed every unit test, because the fixtures used well-separated values
where the buggy and correct formulas agree. The failing artifact is committed
alongside the passing one, and `docs/adr/ADR-009-addendum.md` records what was
wrong and why. Deleting the first artifact would have been the easier story and
a worse project.

## Running the guardrail locally

```bash
bash scripts/check_no_fake_data.sh
```

This also runs in CI (`.github/workflows/ci.yml`) alongside ruff, mypy, and
pytest. A PR that fails it will not merge.

## Development setup

```bash
pip install -e ".[dev]"
ruff check .
mypy orchestrator agent bench
pytest -q
bash scripts/check_no_fake_data.sh
```

## Commits

Keep commits atomic and scoped to one logical change. Do not bundle
unrelated refactors with feature work.
