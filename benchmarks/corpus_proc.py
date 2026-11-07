"""Agents whose bug only a REAL process kill can show (process-mode corpus).

Durable state is kept in a module-level dict keyed by ``ctx.run_id`` - exactly what
the README's "your own persistence" protocol suggests for tests - so a simulated
crash (same interpreter) keeps it and the agent looks fine, while a real kill (new
interpreter) loses it. Targets for ``conform_process``: ``corpus_proc:<ClassName>``
with ``benchmarks/`` on sys.path.
"""
from __future__ import annotations

from typing import Dict, Set

_MEM: Dict[str, Set[str]] = {}   # "the database" - wrongly just process memory


class _Base:
    def __init__(self, ctx):
        self.ctx = ctx
        self.seen = _MEM.setdefault(ctx.run_id, set())

    def state(self):
        return sorted(self.ctx.effects.keys())


class ProcGood(_Base):
    """Reconciles against the effect ledger; needs no memory."""

    def step(self, item):
        if f"pay:{item}" not in self.ctx.effects.keys():
            self.ctx.effect("pay", item, key=f"pay:{item}")


class ProcMemoryCheckpoint(_Base):
    """Marks the item done in process memory, then does the work."""

    def step(self, item):
        if item in self.seen:
            return
        self.seen.add(item)
        self.ctx.effect("pay", item, key=f"pay:{item}")


class ProcFinallyCheckpoint(_Base):
    """Checkpoint in ``finally``: runs while a simulated crash unwinds, never on SIGKILL."""

    def step(self, item):
        if item in self.seen:
            return
        try:
            self.ctx.effect("pay", item, key=f"pay:{item}")
        finally:
            self.seen.add(item)
