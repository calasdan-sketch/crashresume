"""Crash/restart conformance: run a subject, then re-run it crashing at every
side-effect boundary, and check the three invariants that matter:

* ``state_lost``       - after restart, the subject's persisted state is empty/gone
                         while the baseline run had state
* ``effect_replayed``  - the same idempotency key was issued twice, or the final
                         effect log contains effects the baseline never produced
* ``resume_diverged``  - the resumed run finished in a different state than the
                         baseline run

The subject is any object with a ``step(input)`` method. The harness owns the
side-effect channel: subjects record effects through ``ctx.effect(...)``, which
is where crashes get injected. Persistence across restart is the subject's job -
the factory is called again to model a fresh process.
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, List, Optional, Sequence


class InjectedCrash(BaseException):
    """Raised inside the subject to simulate a process death at a side effect.

    Derives from ``BaseException`` (like ``KeyboardInterrupt``) so a subject's
    broad ``except Exception`` cannot swallow the simulated death.
    """


@dataclass(frozen=True)
class Effect:
    kind: str
    payload: Any = None
    key: Optional[str] = None
    seq: int = 0


class EffectLog:
    """Durable record of side effects, shared across restarts of one run."""

    def __init__(self) -> None:
        self._effects: List[Effect] = []

    def record(self, kind: str, payload: Any = None, key: Optional[str] = None) -> Effect:
        e = Effect(kind=kind, payload=payload, key=key, seq=len(self._effects))
        self._effects.append(e)
        return e

    @property
    def effects(self) -> List[Effect]:
        return list(self._effects)

    def keys(self) -> List[str]:
        return [e.key for e in self._effects if e.key is not None]


class Context:
    """Handed to the factory on every (re)start; subjects record effects via it.

    ``run_id`` is the scenario namespace: the baseline run uses ``"run"``;
    crash point *k* and its resume share ``"run-<k>"``. State that must survive
    a restart but must NOT leak from one scenario into the next should be keyed
    by ``ctx.run_id`` (e.g. an in-memory ``stores.setdefault(ctx.run_id, ...)``).
    """

    def __init__(self, run_id: str, log: EffectLog, crash_at: Optional[int] = None) -> None:
        self.run_id = run_id
        self.effects = log
        self._log = log
        self._crash_at = crash_at
        self._count = 0

    def effect(self, kind: str, payload: Any = None, key: Optional[str] = None) -> Effect:
        """Record a side effect. The crash is injected right after this call,
        modelling: side effect committed, process died before the checkpoint."""
        e = self._log.record(kind, payload, key)
        self._count += 1
        if self._crash_at is not None and self._count >= self._crash_at:
            raise InjectedCrash(f"crash injected after effect #{self._count} ({kind})")
        return e


@dataclass(frozen=True)
class Finding:
    kind: str          # state_lost | effect_replayed | effect_lost | resume_diverged | resume_crashed
    crash_after: int   # which effect boundary the crash was injected at
    detail: str


@dataclass
class ConformanceReport:
    crash_points: int = 0
    findings: List[Finding] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.findings

    def __bool__(self) -> bool:
        return self.passed

    def __str__(self) -> str:
        head = (
            f"conformance: {self.crash_points} crash point(s) tested, "
            f"{len(self.findings)} finding(s)"
        )
        warn = "".join(f"\n  warning: {w}" for w in self.warnings)
        if not self.findings:
            return head + " - PASS" + warn
        body = "\n".join(
            f"  [{f.kind}] crash after effect #{f.crash_after}: {f.detail}"
            for f in self.findings
        )
        return head + "\n" + body + warn


def _is_empty(value: Any) -> bool:
    """Deep emptiness: None, empty containers, or containers whose contents are
    all empty. Numbers are never empty (0 is data, not absence)."""
    if value is None or value is False:
        return True
    if isinstance(value, str):
        return value == ""
    if isinstance(value, dict):
        return all(_is_empty(v) for v in value.values())
    if isinstance(value, (list, tuple, set)):
        return len(value) == 0 or all(_is_empty(v) for v in value)
    return False


def _snapshot(agent: Any) -> Any:
    """Best-effort observable state of a subject."""
    state = getattr(agent, "state", None)
    if callable(state):
        return state()
    if hasattr(agent, "__dict__"):
        return {k: v for k, v in vars(agent).items() if not k.startswith("_") and k != "ctx"}
    return None


def _run(factory: Callable[[Context], Any], inputs: Sequence[Any], log: EffectLog,
         run_id: str, crash_at: Optional[int] = None) -> Any:
    agent = factory(Context(run_id, log, crash_at=crash_at))
    for item in inputs:
        agent.step(item)
    return agent


def conform(
    factory: Callable[[Context], Any],
    inputs: Iterable[Any],
    run_id: str = "run",
) -> ConformanceReport:
    """Run ``factory(ctx) -> subject`` to completion, then re-run it crashing at
    every effect boundary and compare against the baseline.

    Each crash point is an independent scenario: the factory is called again
    from scratch (baseline state included). Key per-scenario durable state by
    ``ctx.run_id`` so scenarios don't contaminate each other - see Context.
    """
    inputs = list(inputs)

    baseline_log = EffectLog()
    baseline_agent = _run(factory, inputs, baseline_log, run_id)
    base_state = _snapshot(baseline_agent)
    base_effects = baseline_log.effects
    base_keys = [e.key for e in base_effects if e.key]

    report = ConformanceReport(crash_points=len(base_effects))
    if not base_effects:
        # Nothing to crash against: subject records no side effects through ctx.
        report.warnings.append(
            "subject recorded no effects through ctx.effect(); nothing was tested"
        )
        return report

    counter = itertools.count(1)
    for k in range(1, len(base_effects) + 1):
        run_id_k = f"{run_id}-{next(counter)}"
        log = EffectLog()
        try:
            _run(factory, inputs, log, run_id_k, crash_at=k)
        except InjectedCrash:
            pass  # expected

        # restart: fresh process model, same durable log
        try:
            agent = factory(Context(run_id_k, log))
            for item in inputs:
                agent.step(item)
            final_state = _snapshot(agent)
        except InjectedCrash:
            report.findings.append(Finding("resume_crashed", k, "resume run raised InjectedCrash again"))
            continue

        final_effects = log.effects
        final_keys = [e.key for e in final_effects if e.key]

        # 1. duplicate idempotency keys in the final log
        seen: set = set()
        flagged: set = set()
        for key in final_keys:
            if key in seen and key not in flagged:
                report.findings.append(Finding("effect_replayed", k, f"key {key!r} issued more than once"))
                flagged.add(key)
            seen.add(key)

        # 2. keyed effects the baseline never issued (beyond one occurrence)
        extra = [key for key in final_keys if final_keys.count(key) > base_keys.count(key)]
        for key in sorted(set(extra)):
            if key not in flagged:
                report.findings.append(Finding("effect_replayed", k, f"key {key!r} appears more often than in baseline"))

        # 2b. keyless effects: compare (kind, payload) counts against baseline
        base_sig = [(e.kind, repr(e.payload)) for e in base_effects if not e.key]
        final_sig = [(e.kind, repr(e.payload)) for e in final_effects if not e.key]
        for sig in sorted(set(final_sig)):
            if final_sig.count(sig) > base_sig.count(sig):
                msg = (
                    f"keyless effect {sig[0]!r} payload {sig[1]} repeated after a crash "
                    "(no idempotency key, so not counted as a finding; pass key= to enforce)"
                )
                if msg not in report.warnings:
                    report.warnings.append(msg)

        # 3. keyed effects that vanished
        for key in sorted(set(base_keys) - set(final_keys)):
            report.findings.append(Finding("effect_lost", k, f"key {key!r} never re-issued after resume"))

        # 4. state invariants (only when the subject exposes comparable state).
        #    A subject that reconciles against the committed effect log may start
        #    from an empty checkpoint and still converge - that is fine. We flag
        #    state only when the loss survives the whole resume: final state is
        #    empty while the baseline had state (state_lost), or non-empty but
        #    different (resume_diverged).
        if base_state is not None and final_state is not None:
            if _is_empty(final_state) and not _is_empty(base_state):
                report.findings.append(
                    Finding("state_lost", k, f"final state {final_state!r} is empty but baseline state was {base_state!r}")
                )
            elif final_state != base_state:
                report.findings.append(
                    Finding(
                        "resume_diverged",
                        k,
                        f"final state {final_state!r} != baseline {base_state!r}",
                    )
                )

    return report
