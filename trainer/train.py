"""Training entrypoint placeholder (torchrun target, see ADR-005).

M0 ships this as a placeholder only. When implemented, this script must:
  - load a real dataset (no synthetic tensors standing in for data),
  - run real forward/backward passes under torch.distributed DDP,
  - report measured accuracy/loss — never a hand-typed or randomly
    generated number,
  - never stand in a sleep call for real compute (see CONTRIBUTING.md
    rule 4 and scripts/check_no_fake_data.sh, which bans that pattern
    under this directory).
"""

from __future__ import annotations


def main() -> None:
    raise NotImplementedError(
        "real training loop lands in the milestone that implements "
        "distributed training (ADR-005); M0 is a placeholder only"
    )


if __name__ == "__main__":
    main()
