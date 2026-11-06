"""Crash/restart conformance: run a subject, then re-run it crashing at every
side-effect boundary, and check the invariants that matter:

* ``state_lost``       - after restart, the subject's state is empty/gone while
                         the baseline run had state
* ``effect_replayed``  - an idempotency key was issued more often than in the
                         baseline (a baseline that itself repeats a key gets a warning)
* ``effect_lost``      - a keyed effect from the baseline was issued fewer times
                         than in the baseline (usually: never)
* ``effect_diverged``  - after a crash, an effect was issued under a baseline key
                         with a different kind or payload (idempotent APIs reject or
                         mis-apply this); opt in with ``check_payloads=True``
* ``resume_diverged``  - the resumed run finished in a different state than the
                         baseline run
* ``resume_crashed``   - the resume itself hit an injected crash it was not given
* ``subject_error``    - the subject raised an ordinary exception after a crash
* ``crash_swallowed``  - the subject caught the injected crash (``except BaseException``)
                         and kept running, which a real process death never allows

The subject is any object with a ``step(input)`` method. The harness owns the
side-effect channel: subjects record effects through ``ctx.effect(...)``, which
is where crashes get injected. Persistence across restart is the subject's job -
the factory is called again to model a fresh process. ``ctx.store`` is a
harness-owned dict that is durable within one scenario and fresh for the next;
each new process life gets a deep copy, so live objects cannot survive a crash.
"""
from __future__ import annotations

import copy
import json
import re
import reprlib
from collections import Counter
from dataclasses import asdict, dataclass, field, replace
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

AFTER = "after"
BEFORE = "before"

_short = reprlib.Repr()
_short.maxstring = 80
_short.maxother = 80
_short.maxlist = _short.maxtuple = _short.maxset = _short.maxdict = 8
_short.maxlevel = 3


def _r(value: Any) -> str:
    """Bounded repr for messages: huge or recursive payloads cannot blow up a report."""
    try:
        return _short.repr(value)
    except Exception:  # a hostile __repr__ must not break the harness
        return f"<unrepresentable {type(value).__name__}>"


def _sig(value: Any) -> str:
    """Comparable payload signature (full repr, falling back safely)."""
    try:
        return repr(value)
    except Exception:
        return f"<unrepresentable {type(value).__name__}>"  # no id(): avoid false "diverged"


_ADDR = re.compile(r" at 0x[0-9a-fA-F]+")


def _norm_sig(value: Any) -> str:
    """Repr signature with memory addresses removed (``<P object at 0x..>``)."""
    return _ADDR.sub("", _sig(value))


def _same_payload(a: Any, b: Any) -> bool:
    """``==`` first (dict order, custom ``__eq__``); repr only as a fallback for
    types without their own equality or whose ``==`` raises or is not a bool."""
    try:
        eq = a == b
        if isinstance(eq, bool):
            if eq:
                return True
            if type(a).__eq__ is not object.__eq__:
                return False          # the type defines equality: trust it
    except Exception:
        pass
    return _norm_sig(a) == _norm_sig(b)


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

    def __len__(self) -> int:
        return len(self._effects)


class Context:
    """Handed to the factory on every (re)start; subjects record effects via it.

    ``run_id`` is the scenario namespace: the baseline run uses ``"run"``; a
    single crash after effect *k* and its resume share ``"run-<k>"``; a crash
    *before* effect *k* uses ``"run-b<k>"``; a second crash *j* effects into the
    resume uses ``"run-<k>-<j>"``. ``store`` is a dict that survives restarts
    within one scenario and starts empty for every scenario - use it, or key
    your own durable state by ``run_id``.
    """

    def __init__(self, run_id: str, log: EffectLog, crash_at: Optional[int] = None,
                 store: Optional[Dict[str, Any]] = None, crash_when: str = AFTER) -> None:
        if crash_when not in (AFTER, BEFORE):
            raise ValueError(f"crash_when must be {AFTER!r} or {BEFORE!r}, got {crash_when!r}")
        self.run_id = run_id
        self.effects = log
        self.store: Dict[str, Any] = {} if store is None else store
        self._log = log
        self._crash_at = crash_at
        self._crash_when = crash_when
        self._count = 0
        self.crashed = False    # True once a crash was injected in this process life
        self.swallowed = False  # True if the subject kept running after a crash

    def effect(self, kind: str, payload: Any = None, key: Optional[str] = None) -> Effect:
        """Record a side effect.

        In the default ``after`` mode the crash is injected right after recording:
        side effect committed, process died before the checkpoint. In ``before``
        mode the process dies just before the effect is committed.
        """
        if key is not None:
            try:
                hash(key)
            except TypeError:
                raise TypeError(f"ctx.effect key must be hashable (use a str), got {type(key).__name__}") from None
        self._count += 1
        armed = self._crash_at is not None and self._count >= self._crash_at
        if armed:
            if self.crashed:
                self.swallowed = True   # already "dead" once: the first crash was caught
            self.crashed = True
        if armed and self._crash_when == BEFORE:
            raise InjectedCrash(f"crash injected before effect #{self._count} ({kind})")
        e = self._log.record(kind, payload, key)
        if armed:
            raise InjectedCrash(f"crash injected after effect #{self._count} ({kind})")
        return e


@dataclass(frozen=True)
class Finding:
    # state_lost | effect_replayed | effect_lost | effect_diverged |
    # resume_diverged | resume_crashed | subject_error | crash_swallowed
    kind: str
    crash_after: int   # effect boundary of the FIRST crash in the scenario
    detail: str
    scenario: str = ""  # run_id of the scenario, e.g. "run-2", "run-b1", "run-2-1"
    when: str = AFTER   # whether the first crash was "after" or "before" that effect


@dataclass
class ConformanceReport:
    crash_points: int = 0          # number of crash scenarios executed
    findings: List[Finding] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.findings

    def __bool__(self) -> bool:
        return self.passed

    def to_dict(self) -> Dict[str, Any]:
        return {
            "passed": self.passed,
            "crash_points": self.crash_points,
            "findings": [asdict(f) for f in self.findings],
            "warnings": list(self.warnings),
        }

    def to_json(self, **kwargs: Any) -> str:
        """JSON for CI artifacts. Extra kwargs go to ``json.dumps``."""
        return json.dumps(self.to_dict(), **kwargs)

    def __str__(self) -> str:
        head = (
            f"conformance: {self.crash_points} crash point(s) tested, "
            f"{len(self.findings)} finding(s)"
        )
        warn = "".join(f"\n  warning: {w}" for w in self.warnings)
        if not self.findings:
            return head + " - PASS" + warn
        body = "\n".join(
            f"  [{f.kind}] crash {f.when} effect #{f.crash_after}"
            + (f" ({f.scenario})" if f.scenario else "")
            + f": {f.detail}"
            for f in self.findings
        )
        return head + "\n" + body + warn


def _is_empty(value: Any, _depth: int = 0) -> bool:
    """Deep emptiness: None, empty containers, or containers whose contents are
    all empty. Numbers are never empty (0 is data, not absence). Depth-bounded
    so self-referential structures cannot recurse forever."""
    if value is None or value is False:
        return True
    if _depth > 50:
        return False
    if isinstance(value, str):
        return value == ""
    if isinstance(value, dict):
        return all(_is_empty(v, _depth + 1) for v in value.values())
    if isinstance(value, (list, tuple, set, frozenset)):
        return len(value) == 0 or all(_is_empty(v, _depth + 1) for v in value)
    return False


def _equal(a: Any, b: Any) -> bool:
    """State equality that survives self-referential containers, ``__eq__`` that
    raises or returns non-bools (e.g. arrays), and objects with no ``__eq__``
    (compared by address-free repr)."""
    return _same_payload(a, b)


def _snapshot(agent: Any) -> Any:
    """Best-effort observable state of a subject."""
    state = getattr(agent, "state", None)
    if callable(state):
        return state()
    if hasattr(agent, "__dict__"):
        return {k: v for k, v in vars(agent).items() if not k.startswith("_") and k != "ctx"}
    return None


def _run(factory: Callable[[Context], Any], inputs: Sequence[Any], ctx: Context) -> Any:
    agent = factory(ctx)
    for item in inputs:
        agent.step(item)
    return agent


Crash = Tuple[int, str]  # (effect index within that process life, AFTER|BEFORE)


def _scenario_id(run_id: str, crashes: Sequence[Crash]) -> str:
    # single after-crash keeps the 0.1.x name "run-<k>" for compatibility
    return run_id + "".join(f"-{k}" if when == AFTER else f"-b{k}" for k, when in crashes)


@dataclass
class _Outcome:
    log: EffectLog
    final_state: Any = None
    last_life_effects: int = 0
    first_life_effects: int = 0
    error: Optional[Finding] = None
    unreached: Optional[str] = None


def _survive(store: Dict[str, Any], isolate: bool) -> Dict[str, Any]:
    """What durable storage hands the next process life."""
    if not isolate:
        return store
    try:
        return copy.deepcopy(store)
    except Exception as exc:
        raise TypeError(
            "ctx.store holds an object that cannot outlive a process "
            f"({type(exc).__name__}: {exc}); store plain data, or pass isolate_store=False"
        ) from exc


_SWALLOWED = "caught InjectedCrash and kept running; a real crash cannot be caught"


def _play(factory: Callable[[Context], Any], inputs: Sequence[Any], run_id: str,
          crashes: Sequence[Crash], isolate: bool = True) -> _Outcome:
    """Run one scenario: crash once per entry in ``crashes``, then resume clean."""
    sid = _scenario_id(run_id, crashes)
    first = crashes[0][0]
    log = EffectLog()
    store: Dict[str, Any] = {}
    out = _Outcome(log=log)
    for life, (k, when) in enumerate(crashes, 1):
        if life > 1:
            store = _survive(store, isolate)
        ctx = Context(sid, log, crash_at=k, store=store, crash_when=when)
        try:
            _run(factory, inputs, ctx)
        except InjectedCrash:
            if life == 1:
                out.first_life_effects = len(log)
            if ctx.swallowed:
                out.error = Finding("crash_swallowed", first, f"process life {life} {_SWALLOWED}", sid)
                return out
            continue
        except Exception as exc:  # noqa: BLE001 - reported, not hidden
            out.error = Finding("subject_error", first, f"process life {life} raised {type(exc).__name__}: {_r(exc)}", sid)
            return out
        if ctx.crashed:
            out.error = Finding("crash_swallowed", first, f"process life {life} {_SWALLOWED}", sid)
            return out
        out.unreached = (
            f"{sid}: crash {when} effect #{k} never reached in process life {life} "
            "(subject issued fewer effects than the baseline - nondeterministic?); resumed without it"
        )
        if life == 1:
            out.first_life_effects = len(log)
        break
    before = len(log)
    store = _survive(store, isolate)
    try:
        agent = _run(factory, inputs, Context(sid, log, store=store))
        out.final_state = _snapshot(agent)
    except InjectedCrash:
        out.error = Finding("resume_crashed", first, "resume run raised InjectedCrash again", sid)
    except Exception as exc:  # noqa: BLE001
        out.error = Finding("subject_error", first, f"resume raised {type(exc).__name__}: {_r(exc)}", sid)
    out.last_life_effects = len(log) - before
    return out


def _check(report: ConformanceReport, sid: str, first: int, out: _Outcome,
           base_effects: List[Effect], base_state: Any,
           check_payloads: bool = True, payload_key: Optional[Callable[[Any], Any]] = None) -> None:
    final_effects = out.log.effects
    base_keys = Counter(e.key for e in base_effects if e.key is not None)
    final_keys = Counter(e.key for e in final_effects if e.key is not None)

    # 1. keyed effects issued more than once, or more often than in the baseline
    for key, n in final_keys.items():
        if n > 1 and n > base_keys.get(key, 0):
            report.findings.append(Finding("effect_replayed", first, f"key {_r(key)} issued {n} times", sid))
        elif n > base_keys.get(key, 0):
            report.findings.append(Finding("effect_replayed", first, f"key {_r(key)} appears more often than in baseline", sid))

    # 1b. same key, different payload - only for effects issued AFTER the first
    #     crash (the first life is just a prefix of the baseline), and only for
    #     keys the baseline issued exactly once.
    if check_payloads:
        norm = payload_key or (lambda p: p)
        base_payload = {e.key: (e.kind, norm(e.payload)) for e in base_effects
                        if e.key is not None and base_keys[e.key] == 1}
        reported = set()
        for e in final_effects[out.first_life_effects:]:
            want = base_payload.get(e.key) if e.key is not None else None
            if want is None or e.key in reported:
                continue
            if e.kind != want[0] or not _same_payload(norm(e.payload), want[1]):
                reported.add(e.key)
                report.findings.append(Finding(
                    "effect_diverged", first,
                    f"key {_r(e.key)} re-issued as {e.kind!r} {_r(e.payload)}; baseline was {want[0]!r} {_r(want[1])}",
                    sid,
                ))

    # 2. keyless effects: compare (kind, payload) counts against baseline (warning only)
    base_sig = Counter((e.kind, _norm_sig(e.payload)) for e in base_effects if e.key is None)
    final_sig = Counter((e.kind, _norm_sig(e.payload)) for e in final_effects if e.key is None)
    for sig, n in sorted(final_sig.items()):
        if n > base_sig.get(sig, 0):
            msg = (
                f"keyless effect {sig[0]!r} payload {_r(sig[1])} repeated after a crash "
                "(no idempotency key, so not counted as a finding; pass key= to enforce)"
            )
            if msg not in report.warnings:
                report.warnings.append(msg)

    # 3. keyed effects that vanished, or were issued fewer times than in the baseline
    for key in sorted(base_keys, key=_sig):
        n, want = final_keys.get(key, 0), base_keys[key]
        if n == 0:
            report.findings.append(Finding("effect_lost", first, f"key {_r(key)} never issued after resume", sid))
        elif n < want:
            report.findings.append(Finding("effect_lost", first, f"key {_r(key)} issued {n} time(s), baseline {want}", sid))

    # 4. state invariants (only when the subject exposes comparable state).
    #    A subject that reconciles against the committed effect log may start
    #    from an empty checkpoint and still converge - that is fine. We flag
    #    state only when the loss survives the whole resume.
    final_state = out.final_state
    if base_state is not None and final_state is not None:
        if _is_empty(final_state) and not _is_empty(base_state):
            report.findings.append(
                Finding("state_lost", first,
                        f"final state {_r(final_state)} is empty but baseline state was {_r(base_state)}", sid)
            )
        elif not _equal(final_state, base_state):
            report.findings.append(
                Finding("resume_diverged", first, f"final state {_r(final_state)} != baseline {_r(base_state)}", sid)
            )


def conform(
    factory: Callable[[Context], Any],
    inputs: Iterable[Any],
    run_id: str = "run",
    *,
    crash_before: bool = False,
    max_crashes: int = 1,
    max_scenarios: Optional[int] = 10_000,
    check_payloads: Optional[bool] = None,
    payload_key: Optional[Callable[[Any], Any]] = None,
    isolate_store: bool = True,
) -> ConformanceReport:
    """Run ``factory(ctx) -> subject`` to completion, then re-run it crashing at
    every effect boundary and compare against the baseline.

    * ``crash_before`` - also crash just *before* each effect commits (catches
      subjects that checkpoint "done" before doing the work -> ``effect_lost``).
    * ``max_crashes`` - crash up to this many times per scenario; 2 also crashes
      the resume at each of its own effect boundaries (crash-during-recovery).
    * ``max_scenarios`` - safety cap on scenarios executed; ``None`` for no cap.
      Hitting it adds a warning, so a truncated run is never silent. Pending
      scenarios are bounded by the cap too, so memory stays O(max_scenarios).
    * ``check_payloads`` / ``payload_key`` - opt in to ``effect_diverged``: compare
      the payload of each keyed effect issued after a crash with the baseline's.
      Off by default (request ids and timestamps legitimately differ). Passing
      ``payload_key=lambda p: p["amount"]`` turns it on and compares only that.
    * ``isolate_store`` - give each new process life a deep copy of ``ctx.store``
      so live objects (iterators, locks, the old agent) cannot survive a crash.
      ``False`` shares one live dict between lives (faster, less realistic).

    Each scenario is independent: the factory is called again from scratch and
    ``ctx.store`` starts empty. Key any other durable state by ``ctx.run_id``.
    """
    if not isinstance(max_crashes, int) or max_crashes < 1:
        raise ValueError("max_crashes must be an int >= 1")
    if max_scenarios is not None and (not isinstance(max_scenarios, int) or max_scenarios < 1):
        raise ValueError("max_scenarios must be a positive int or None")
    if check_payloads is None:
        check_payloads = payload_key is not None
    inputs = list(inputs)
    modes = (AFTER, BEFORE) if crash_before else (AFTER,)

    baseline_log = EffectLog()
    baseline_agent = _run(factory, inputs, Context(run_id, baseline_log))
    base_state = _snapshot(baseline_agent)
    base_effects = baseline_log.effects

    report = ConformanceReport()
    if not base_effects:
        report.warnings.append("subject recorded no effects through ctx.effect(); nothing was tested")
        return report
    for key, n in Counter(e.key for e in base_effects if e.key is not None).items():
        if n > 1:
            report.warnings.append(
                f"baseline issued key {_r(key)} {n} times with no crash at all - a double effect? "
                f"(treated as the expected count)"
            )

    # breadth-first: all single crashes first, so a cap trims the deepest scenarios
    queue: List[Tuple[Crash, ...]] = [((k, when),) for when in modes for k in range(1, len(base_effects) + 1)]
    truncated = False
    dropped = 0   # children never enqueued because of the cap
    i = 0
    while i < len(queue):
        if max_scenarios is not None and report.crash_points >= max_scenarios:
            truncated = True
            break
        crashes = queue[i]
        i += 1
        sid = _scenario_id(run_id, crashes)
        out = _play(factory, inputs, run_id, crashes, isolate_store)
        if out.unreached and out.unreached not in report.warnings:
            report.warnings.append(out.unreached)
        report.crash_points += 1
        if out.error is not None:
            report.findings.append(replace(out.error, when=crashes[0][1]))
            continue
        start = len(report.findings)
        _check(report, sid, crashes[0][0], out, base_effects, base_state, check_payloads, payload_key)
        if crashes[0][1] != AFTER:
            report.findings[start:] = [replace(f, when=crashes[0][1]) for f in report.findings[start:]]
        if len(crashes) < max_crashes and not out.unreached:
            for when in modes:
                for j in range(1, out.last_life_effects + 1):
                    if max_scenarios is not None and len(queue) >= max_scenarios:
                        dropped += 1
                    else:
                        queue.append(crashes + ((j, when),))
    if truncated or dropped:
        report.warnings.append(
            f"stopped at max_scenarios={max_scenarios}; at least {len(queue) - i + dropped} more scenario(s) were not run"
        )
    return report
