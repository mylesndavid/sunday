# Adapted from hermes/run_agent.py — MIT — (c) 2025 Nous Research
# Ported verbatim from the IterationBudget class in Hermes's run_agent.py
# (around line 283) with attribution. The class is small, well-tested in
# production, and exactly what we need — no point rewriting it.

"""Thread-safe iteration counter shared across an agent + its sub-agents."""

from __future__ import annotations

import threading


class IterationBudget:
    """Thread-safe iteration counter for an agent.

    Each agent (parent or sub-agent) gets its own ``IterationBudget``.
    The parent's budget is capped at ``max_iterations`` (Sunday default 30,
    Hermes default 90). Sub-agents get independent budgets — total iterations
    across parent + sub-agents can exceed the parent's cap by design.

    Lock-protected because tool execution can be parallelized across threads.
    """

    def __init__(self, max_total: int) -> None:
        self.max_total = max_total
        self._used = 0
        self._lock = threading.Lock()

    def consume(self) -> bool:
        """Try to consume one iteration. Returns True if allowed."""
        with self._lock:
            if self._used >= self.max_total:
                return False
            self._used += 1
            return True

    def refund(self) -> None:
        """Give back one iteration — e.g. for programmatic tool calls that
        shouldn't eat from the user-visible budget."""
        with self._lock:
            if self._used > 0:
                self._used -= 1

    @property
    def used(self) -> int:
        with self._lock:
            return self._used

    @property
    def remaining(self) -> int:
        with self._lock:
            return max(0, self.max_total - self._used)
