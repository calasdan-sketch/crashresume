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
* ``subject_hang``     - process mode only: a child exceeded its timeout

The subject is any object with a ``step(input)`` method. The harness owns the
side-effect channel: subjects record effects through ``ctx.effect(...)``, which
is where crashes get injected. Persistence across restart is the subject's job -
the factory is called again to model a fresh process. ``ctx.store`` is a
harness-owned dict that is durable within one scenario and fresh for the next;
each new process life gets a deep copy, so live objects cannot survive a crash.
"""
from __future__ import annotations

import collections.abc as _abc
import copy
import functools
import inspect
import json
import re
import reprlib
import threading
from collections import Counter
from dataclasses import asdict, dataclass, field, replace
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from .fs import SimFS

AFTER = "after"
BEFORE = "before"
TORN = "torn"   # crash after an fs.write with the un-fsynced tail torn to a prefix
_WHENS = (AFTER, BEFORE, TORN)

_short = reprlib.Repr()
_short.maxstring = 80
_short.maxother = 80
_short.maxlist = _short.maxtuple = _short.maxset = _short.maxdict = 8
_short.maxlevel = 3


def clean(text: str) -> str:
    """Escape terminal-control, bidi and zero-width characters from untrusted text
    (payloads, keys, exception messages) so a report cannot repaint a terminal or
    visually reorder a CI log."""
    if text.isprintable():
        return text
    out = []
    for ch in text:
        if ch.isprintable() or ch == " ":
            out.append(ch)
        elif ch in "\n\t":
            out.append(ch.encode("unicode_escape").decode("ascii"))
        else:
            out.append(f"\\u{ord(ch):04x}" if ord(ch) <= 0xFFFF else f"\\U{ord(ch):08x}")
    return "".join(out)


def _r(value: Any) -> str:
    """Bounded, sanitized repr for messages: huge or recursive payloads cannot blow
    up a report and control characters cannot reach the terminal."""
    try:
        return clean(_short.repr(value))
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
                # the type defines equality: trust it, except that builtin containers and
                # floats holding NaN are never == to themselves; equal reprs then mean equal
                return type(a).__module__ == "builtins" and _norm_sig(a) == _norm_sig(b)
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


class KeyView(_abc.Sequence):
    """Read-only, list-like snapshot of the idempotency keys issued so far.

    ``key in ctx.effects.keys()`` is O(1) (the README's reconcile idiom used to be
    O(n) per call, which made whole runs cubic). Behaves like the list the old
    ``keys()`` returned: ``len``, indexing, slicing (gives a list), iteration,
    ``==`` against lists, ``list(view)``.
    """

    __slots__ = ("_keys", "_first", "_n")

    def __init__(self, keys: List[Any], first: Dict[Any, int], n: int) -> None:
        self._keys, self._first, self._n = keys, first, n

    def __len__(self) -> int:
        return self._n

    def __getitem__(self, i: Any) -> Any:
        if isinstance(i, slice):
            return self._keys[: self._n][i]
        if i >= self._n or i < -self._n:
            raise IndexError("key index out of range")
        return self._keys[i if i >= 0 else self._n + i]

    def __iter__(self) -> Any:
        for i in range(self._n):
            yield self._keys[i]

    def __contains__(self, key: object) -> bool:
        try:
            return self._first.get(key, self._n) < self._n
        except TypeError:   # unhashable probe: cannot be a key
            return False

    def __eq__(self, other: object) -> bool:
        if isinstance(other, (KeyView, list, tuple)):
            return list(self) == list(other)
        return NotImplemented

    def __add__(self, other: Any) -> List[Any]:
        return list(self) + list(other)

    def __radd__(self, other: Any) -> List[Any]:
        return list(other) + list(self)

    __hash__ = None  # type: ignore[assignment]

    def __repr__(self) -> str:
        return repr(list(self))


class EffectLog:
    """Durable record of side effects, shared across restarts of one run."""

    def __init__(self) -> None:
        self._effects: List[Effect] = []
        self._keys: List[Any] = []          # keyed effects only, in order
        self._first: Dict[Any, int] = {}    # key -> index of its first occurrence in _keys

    def record(self, kind: str, payload: Any = None, key: Optional[str] = None) -> Effect:
        e = Effect(kind=kind, payload=payload, key=key, seq=len(self._effects))
        self._effects.append(e)
        if key is not None:
            self._first.setdefault(key, len(self._keys))
            self._keys.append(key)
        return e

    @property
    def effects(self) -> List[Effect]:
        return list(self._effects)

    def keys(self) -> "KeyView":
        """Keys issued so far (a snapshot; ``in`` is O(1))."""
        return KeyView(self._keys, self._first, len(self._keys))

    def has(self, key: Any) -> bool:
        """True if an effect with this idempotency key has already happened."""
        try:
            return key in self._first
        except TypeError:
            return False

    def count(self, key: Any) -> int:
        """How many effects carry this key."""
        return sum(1 for k in self._keys if k == key) if self.has(key) else 0

    def __len__(self) -> int:
        return len(self._effects)


class Context:
    """Handed to the factory on every (re)start; subjects record effects via it.

    ``run_id`` is the scenario namespace: the baseline run uses ``"run"``; a
    single crash after effect *k* and its resume share ``"run-<k>"``; a crash
    *before* effect *k* uses ``"run-b<k>"``; a torn write at point *k* uses
    ``"run-t<k>"``; a second crash *j* points into the resume uses
    ``"run-<k>-<j>"``. ``store`` is a dict that survives restarts within one
    scenario and starts empty for every scenario - use it, or key your own
    durable state by ``run_id``. ``fs`` is a ``SimFS`` that survives restarts
    (minus whatever was not fsynced) and whose mutating calls are crash points.
    """

    def __init__(self, run_id: str, log: EffectLog, crash_at: Optional[int] = None,
                 store: Optional[Dict[str, Any]] = None, crash_when: str = AFTER,
                 fs: Optional[SimFS] = None, trace: Optional[List[str]] = None) -> None:
        if crash_when not in _WHENS:
            raise ValueError(f"crash_when must be one of {_WHENS}, got {crash_when!r}")
        self.run_id = run_id
        self.effects = log
        self.store: Dict[str, Any] = {} if store is None else store
        self.fs: Any = SimFS() if fs is None else fs
        self._pending = ""
        self._owner = threading.get_ident()
        self.off_thread = False   # a crash point was hit on a thread other than the one that built the context
        self.trace = trace   # when a list: one line per crash point (used by explain())
        self.fs._bind(self._begin, self._end, self._note)
        self._log = log
        self._crash_at = crash_at
        self._crash_when = crash_when
        self._count = 0
        self.points: List[str] = []   # kind of every crash point this process life passed
        self.crashed = False    # True once a crash was injected in this process life
        self.swallowed = False  # True if the subject kept running after a crash

    # -- crash machinery -------------------------------------------------
    def _die(self, message: str) -> None:
        raise InjectedCrash(message)

    def _begin(self, kind: str, label: Optional[str] = None) -> Tuple[bool, str]:
        self._count += 1
        self.points.append(kind)
        desc = label or f"{kind} #{self._count}"
        if self.trace is not None:
            self.trace.append(f"#{self._count} {kind}:" + (f" {self._pending}" if self._pending else ""))
            self._pending = ""
        armed = self._crash_at is not None and self._count >= self._crash_at
        if armed:
            if self.crashed:
                self.swallowed = True   # already "dead" once: the first crash was caught
            self.crashed = True
            if threading.get_ident() != self._owner:
                self.off_thread = True
            if self._crash_when == BEFORE:
                self._die(f"crash injected before {desc}")
        return armed, desc

    def _note(self, text: str) -> None:
        """Describe the NEXT crash point (call just before ``_begin``); used by explain()."""
        if self.trace is not None:
            self._pending = text

    def _end(self, token: Tuple[bool, str], kind: str = "") -> None:
        armed, desc = token
        if armed:
            self._die(f"crash injected after {desc}")

    # -- the side-effect channel -----------------------------------------
    def effect(self, kind: str, payload: Any = None, key: Optional[str] = None,
               _do: Optional[Callable[[], Any]] = None) -> Effect:
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
        if self.trace is not None:
            self._note(f"{clean(str(kind))} key={_r(key)} payload={_r(payload)}")
        tok = self._begin("effect", f"effect #{self._count + 1} ({kind})")
        if _do is not None:
            _do()
        e = self._log.record(kind, payload, key)
        self._end(tok)
        return e

    def tool(self, fn: Callable[..., Any], *, kind: Optional[str] = None,
             key: Any = None, payload: Any = None) -> Callable[..., Any]:
        """Wrap a real tool/function so every call is one crash-tested effect.

        The wrapper runs ``fn`` between the "before" and "after" crash points, then
        records the effect and returns ``fn``'s result - a drop-in for the tool
        lists of agent frameworks. ``key`` / ``payload`` may be constants or
        callables receiving the call's ``(*args, **kwargs)``. Coroutine functions
        get an ``async`` wrapper.
        """
        name = kind or getattr(fn, "__name__", "tool")

        def meta(a: tuple, kw: dict) -> Tuple[Any, Any]:
            k = key(*a, **kw) if callable(key) else key
            p = payload(*a, **kw) if callable(payload) else ({"args": a, "kwargs": kw} if payload is None else payload)
            if k is not None:
                hash(k)
            return k, p

        if inspect.iscoroutinefunction(fn):
            @functools.wraps(fn)
            async def awrapper(*a: Any, **kw: Any) -> Any:
                k, p = meta(a, kw)
                if self.trace is not None:
                    self._note(f"{clean(str(name))} key={_r(k)} payload={_r(p)}")
                tok = self._begin("effect", f"effect #{self._count + 1} ({name})")
                result = await fn(*a, **kw)
                self._log.record(name, p, k)
                self._end(tok)
                return result
            return awrapper

        @functools.wraps(fn)
        def wrapper(*a: Any, **kw: Any) -> Any:
            k, p = meta(a, kw)
            box: List[Any] = []
            self.effect(name, p, key=k, _do=lambda: box.append(fn(*a, **kw)))
            return box[0]
        return wrapper


@dataclass(frozen=True)
class Finding:
    # state_lost | effect_replayed | effect_lost | effect_diverged |
    # resume_diverged | resume_crashed | subject_error | crash_swallowed | subject_hang
    kind: str
    crash_after: int   # crash point of the FIRST crash in the scenario
    detail: str
    scenario: str = ""  # run_id of the scenario, e.g. "run-2", "run-b1", "run-t4", "run-2-1"
    when: str = AFTER   # whether the first crash was "after", "before" or "torn"
    crashes: Tuple[Tuple[int, str], ...] = ()   # every crash in the scenario: (point, when)
    point: str = "effect"   # kind of the first crash point: "effect", "fs.write", "fs.fsync", ...


@dataclass(frozen=True)
class Repro:
    """A minimal reproduction: the fewest inputs and crashes that still show ``kind``."""
    kind: str
    inputs: Tuple[Any, ...]
    crashes: Tuple[Tuple[int, str], ...]
    detail: str

    def __str__(self) -> str:
        cr = ", ".join(f"{w} #{k}" for k, w in self.crashes)
        return f"minimal repro for {self.kind}: inputs={_r(list(self.inputs))} crashes=[{cr}] - {self.detail}"


@dataclass
class ConformanceReport:
    crash_points: int = 0          # number of crash scenarios executed
    findings: List[Finding] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    scenarios: List[str] = field(default_factory=list)   # id of every scenario executed
    repros: List[Repro] = field(default_factory=list)    # filled when conform(minimize=True)

    @property
    def passed(self) -> bool:
        return not self.findings

    @property
    def vacuous(self) -> bool:
        """True when no crash scenario ran at all (no effects recorded, or no inputs):
        ``passed`` is then true only because nothing was tested."""
        return self.crash_points == 0

    def __bool__(self) -> bool:
        return self.passed

    def to_dict(self) -> Dict[str, Any]:
        return {
            "passed": self.passed,
            "crash_points": self.crash_points,
            "findings": [asdict(f) for f in self.findings],
            "warnings": list(self.warnings),
            "scenarios": list(self.scenarios),
            "repros": [{"kind": r.kind, "inputs": [_r(i) for i in r.inputs], "crashes": [list(c) for c in r.crashes],
                        "detail": r.detail} for r in self.repros],
        }

    def to_json(self, **kwargs: Any) -> str:
        """JSON for CI artifacts. Extra kwargs go to ``json.dumps``."""
        return json.dumps(self.to_dict(), **kwargs)

    def to_junit(self, name: str = "crashresume") -> str:
        """JUnit XML: one testcase per crash scenario (see ``crashresume.reports``)."""
        from .reports import to_junit
        return to_junit(self, name)

    def to_sarif(self, artifact_uri: str = "crashresume") -> str:
        """SARIF 2.1.0 for GitHub code scanning (see ``crashresume.reports``)."""
        from .reports import to_sarif
        return to_sarif(self, artifact_uri)

    def __str__(self) -> str:
        head = (
            f"conformance: {self.crash_points} crash point(s) tested, "
            f"{len(self.findings)} finding(s)"
        )
        warn = "".join(f"\n  warning: {w}" for w in self.warnings)
        rep = "".join(f"\n  {r}" for r in self.repros)
        if not self.findings:
            return head + " - PASS" + warn
        body = "\n".join(
            f"  [{f.kind}] crash {f.when} {f.point} #{f.crash_after}"
            + (f" ({f.scenario})" if f.scenario else "")
            + f": {f.detail}"
            for f in self.findings
        )
        return head + "\n" + body + rep + warn


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
        return {k: v for k, v in vars(agent).items() if not k.startswith("_") and k not in ("ctx", "fs")}
    return None


def _fresh(inputs: Sequence[Any]) -> List[Any]:
    """A private copy of the inputs for one process life: a ``step()`` that mutates its
    input must not poison later scenarios (a real restart re-reads the input)."""
    try:
        return copy.deepcopy(list(inputs))
    except Exception:   # not copyable (locks, generators): share, as before
        return list(inputs)


def _run(factory: Callable[[Context], Any], inputs: Sequence[Any], ctx: Context) -> Any:
    agent = factory(ctx)
    if not callable(getattr(agent, "step", None)):
        raise TypeError(f"factory must return an object with a step(input) method, got {type(agent).__name__}")
    for item in _fresh(inputs):
        agent.step(item)
    return agent


Crash = Tuple[int, str]  # (crash point index within that process life, AFTER|BEFORE|TORN)

_TAG = {AFTER: "", BEFORE: "b", TORN: "t"}


def _scenario_id(run_id: str, crashes: Sequence[Crash]) -> str:
    # single after-crash keeps the 0.1.x name "run-<k>" for compatibility
    return run_id + "".join(f"-{_TAG[when]}{k}" for k, when in crashes)


@dataclass
class _Outcome:
    log: EffectLog
    final_state: Any = None
    last_life_effects: int = 0
    first_life_effects: int = 0
    last_life_kinds: List[str] = field(default_factory=list)
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
_THREAD_HINT = (" (a crash point was hit on a worker thread: an exception there cannot stop the process, "
                "so issue effects from the thread that runs step())")


def _play(factory: Callable[[Context], Any], inputs: Sequence[Any], run_id: str,
          crashes: Sequence[Crash], isolate: bool = True, strict_dirs: bool = False,
          trace: Optional[List[str]] = None) -> _Outcome:
    """Run one scenario: crash once per entry in ``crashes``, then resume clean."""
    sid = _scenario_id(run_id, crashes)
    first = crashes[0][0]
    log = EffectLog()
    store: Dict[str, Any] = {}
    fs = SimFS(dir_durability=strict_dirs)
    out = _Outcome(log=log)

    def lane(title: str) -> Optional[List[str]]:
        if trace is None:
            return None
        trace.append(title)
        return []

    def flush(lines: Optional[List[str]]) -> None:
        if trace is not None and lines is not None:
            trace.extend("  " + ln for ln in lines)

    def fail(kind: str, detail: str) -> _Outcome:
        out.error = Finding(kind, first, detail, sid, crashes[0][1], tuple(crashes))
        return out

    for life, (k, when) in enumerate(crashes, 1):
        if life > 1:
            store = _survive(store, isolate)
        lines = lane(f"process life {life}: killed {when} point #{k}")
        ctx = Context(sid, log, crash_at=k, store=store, crash_when=when, fs=fs, trace=lines)
        try:
            _run(factory, inputs, ctx)
        except InjectedCrash:
            if lines:
                lines[-1] += "   <-- KILLED " + {AFTER: "right after this committed", BEFORE: "BEFORE this committed",
                                                  TORN: "after this write, tail torn"}[when]
            flush(lines)
            if trace is not None:
                trace.append("  -- process dies here; "
                             + ("un-fsynced data is torn" if when == TORN else "un-fsynced file data is lost"))
            if life == 1:
                out.first_life_effects = len(log)
            fs.power_loss(torn=(when == TORN))
            if ctx.swallowed:
                return fail("crash_swallowed", f"process life {life} {_SWALLOWED}" + (_THREAD_HINT if ctx.off_thread else ""))
            continue
        except Exception as exc:  # noqa: BLE001 - reported, not hidden
            flush(lines)
            return fail("subject_error", f"process life {life} raised {type(exc).__name__}: {_r(exc)}")
        flush(lines)
        if ctx.crashed:
            return fail("crash_swallowed", f"process life {life} {_SWALLOWED}" + (_THREAD_HINT if ctx.off_thread else ""))
        out.unreached = (
            f"{sid}: crash {when} point #{k} never reached in process life {life} "
            "(subject issued fewer effects than the baseline - nondeterministic?); resumed without it"
        )
        if life == 1:
            out.first_life_effects = len(log)
        break
    before = len(log)
    store = _survive(store, isolate)
    lines = lane("resume (clean process, no crash)")
    ctx = Context(sid, log, store=store, fs=fs, trace=lines)
    try:
        agent = _run(factory, inputs, ctx)
        out.final_state = _snapshot(agent)
    except InjectedCrash:
        fail("resume_crashed", "resume run raised InjectedCrash again")
    except Exception as exc:  # noqa: BLE001
        fail("subject_error", f"resume raised {type(exc).__name__}: {_r(exc)}")
    flush(lines)
    out.last_life_effects = len(log) - before
    out.last_life_kinds = list(ctx.points)
    return out


def _check(report: ConformanceReport, sid: str, first: int, out: _Outcome,
           base_effects: List[Effect], base_state: Any,
           check_payloads: bool = True, payload_key: Optional[Callable[[Any], Any]] = None,
           strict_keyless: bool = False, crashes: Tuple[Crash, ...] = ()) -> None:
    final_effects = out.log.effects
    base_keys = Counter(e.key for e in base_effects if e.key is not None)
    final_keys = Counter(e.key for e in final_effects if e.key is not None)
    when = crashes[0][1] if crashes else AFTER

    def add(kind: str, detail: str) -> None:
        report.findings.append(Finding(kind, first, detail, sid, when, crashes))

    # 1. keyed effects issued more than once, or more often than in the baseline
    for key, n in final_keys.items():
        if n > 1 and n > base_keys.get(key, 0):
            add("effect_replayed", f"key {_r(key)} issued {n} times")
        elif n > base_keys.get(key, 0):
            add("effect_replayed", f"key {_r(key)} appears more often than in baseline")

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
                add("effect_diverged",
                    f"key {_r(e.key)} re-issued as {e.kind!r} {_r(e.payload)}; baseline was {want[0]!r} {_r(want[1])}")

    # 2. keyless effects: compare (kind, payload) counts against baseline
    #    (warning only, unless strict_keyless turns them into findings)
    base_sig = Counter((e.kind, _norm_sig(e.payload)) for e in base_effects if e.key is None)
    final_sig = Counter((e.kind, _norm_sig(e.payload)) for e in final_effects if e.key is None)
    for sig, n in sorted(final_sig.items()):
        if n > base_sig.get(sig, 0):
            if strict_keyless:
                add("effect_replayed", f"keyless effect {sig[0]!r} payload {_r(sig[1])} repeated after a crash")
                continue
            msg = (
                f"keyless effect {sig[0]!r} payload {_r(sig[1])} repeated after a crash "
                "(no idempotency key, so not counted as a finding; pass key= or strict_keyless=True to enforce)"
            )
            if msg not in report.warnings:
                report.warnings.append(msg)
    if strict_keyless:
        for sig, want in sorted(base_sig.items()):
            if final_sig.get(sig, 0) < want:
                got = final_sig.get(sig, 0)
                add("effect_lost", f"keyless effect {sig[0]!r} payload {_r(sig[1])} happened {got} time(s), baseline {want}")

    # 3. keyed effects that vanished, or were issued fewer times than in the baseline
    for key in sorted(base_keys, key=_sig):
        n, want = final_keys.get(key, 0), base_keys[key]
        if n == 0:
            add("effect_lost", f"key {_r(key)} never issued after resume")
        elif n < want:
            add("effect_lost", f"key {_r(key)} issued {n} time(s), baseline {want}")

    # 4. state invariants (only when the subject exposes comparable state).
    #    A subject that reconciles against the committed effect log may start
    #    from an empty checkpoint and still converge - that is fine. We flag
    #    state only when the loss survives the whole resume.
    final_state = out.final_state
    if base_state is not None and final_state is not None:
        if _is_empty(final_state) and not _is_empty(base_state):
            add("state_lost", f"final state {_r(final_state)} is empty but baseline state was {_r(base_state)}")
        elif not _equal(final_state, base_state):
            add("resume_diverged", f"final state {_r(final_state)} != baseline {_r(base_state)}")


@dataclass
class _Baseline:
    effects: List[Effect]
    state: Any
    kinds: List[str]


def _thin(points: List[int], sample: Optional[int], skipped: Optional[List[int]]) -> List[int]:
    """Keep ``sample`` evenly spaced points (always the first and the last)."""
    if sample is None or len(points) <= sample:
        return points
    if sample == 1:
        keep = [points[-1]]
    else:
        keep = sorted({points[round(i * (len(points) - 1) / (sample - 1))] for i in range(sample)})
    if skipped is not None:
        skipped[0] += len(points) - len(keep)
    return keep


def _menu(kinds: Sequence[str], modes: Sequence[str], torn: bool,
          sample: Optional[int] = None, skipped: Optional[List[int]] = None) -> List[Crash]:
    """Crash options for a process life whose crash points have these kinds."""
    every = list(range(1, len(kinds) + 1))
    out: List[Crash] = [(k, when) for when in modes for k in _thin(every, sample, skipped)]
    if torn:
        out += [(k, TORN) for k in _thin([k for k in every if kinds[k - 1] == "fs.write"], sample, skipped)]
    return out


def _explore(
    run_id: str,
    base: _Baseline,
    play: Callable[[Tuple[Crash, ...]], _Outcome],
    *,
    modes: Sequence[str],
    torn: bool,
    max_crashes: int,
    max_scenarios: Optional[int],
    check_payloads: bool,
    payload_key: Optional[Callable[[Any], Any]],
    strict_keyless: bool,
    only: Optional[Sequence[Tuple[Crash, ...]]] = None,
    stop: Optional[Callable[[Finding], bool]] = None,
    sample: Optional[int] = None,
) -> ConformanceReport:
    report = ConformanceReport()
    skipped = [0]
    if not base.kinds:
        report.warnings.append("subject recorded no effects through ctx.effect(); nothing was tested")
        return report
    if torn and "fs.write" not in base.kinds:
        report.warnings.append("torn_writes=True but the subject never wrote through ctx.fs; nothing was torn")
    for key, n in Counter(e.key for e in base.effects if e.key is not None).items():
        if n > 1:
            report.warnings.append(
                f"baseline issued key {_r(key)} {n} times with no crash at all - a double effect? "
                f"(treated as the expected count)"
            )

    # breadth-first: all single crashes first, so a cap trims the deepest scenarios
    first_level = [(c,) for c in _menu(base.kinds, modes, torn, sample, skipped)]
    queue: List[Tuple[Crash, ...]] = list(only) if only is not None else first_level
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
        out = play(crashes)
        if out.unreached and out.unreached not in report.warnings:
            report.warnings.append(out.unreached)
        report.crash_points += 1
        report.scenarios.append(sid)
        start = len(report.findings)
        if out.error is not None:
            report.findings.append(out.error)
        else:
            _check(report, sid, crashes[0][0], out, base.effects, base.state, check_payloads, payload_key,
                   strict_keyless, crashes)
        if len(report.findings) > start and 0 < crashes[0][0] <= len(base.kinds):
            pk = base.kinds[crashes[0][0] - 1]
            report.findings[start:] = [replace(f, point=pk) for f in report.findings[start:]]
        if stop is not None and any(stop(f) for f in report.findings[start:]):
            return report
        if out.error is not None:
            continue
        if only is None and len(crashes) < max_crashes and not out.unreached:
            for child in _menu(out.last_life_kinds, modes, torn, sample, skipped):
                if max_scenarios is not None and len(queue) >= max_scenarios:
                    dropped += 1
                else:
                    queue.append(crashes + (child,))
    if skipped[0]:
        report.warnings.append(
            f"sample={sample}: {skipped[0]} crash point(s) were NOT tested (evenly spaced subset only); "
            "a pass is not a proof"
        )
    if truncated or dropped:
        report.warnings.append(
            f"stopped at max_scenarios={max_scenarios}; at least {len(queue) - i + dropped} more scenario(s) were not run"
        )
    return report


def _validate(max_crashes: Any, max_scenarios: Any, sample: Any = None) -> None:
    if sample is not None and (not isinstance(sample, int) or isinstance(sample, bool) or sample < 1):
        raise ValueError("sample must be a positive int or None")
    if not isinstance(max_crashes, int) or isinstance(max_crashes, bool) or max_crashes < 1:
        raise ValueError("max_crashes must be an int >= 1")
    if max_scenarios is not None and (not isinstance(max_scenarios, int) or isinstance(max_scenarios, bool) or max_scenarios < 1):
        raise ValueError("max_scenarios must be a positive int or None")


def _conform(
    factory: Callable[[Context], Any],
    inputs: Sequence[Any],
    run_id: str,
    *,
    crash_before: bool,
    max_crashes: int,
    max_scenarios: Optional[int],
    check_payloads: bool,
    payload_key: Optional[Callable[[Any], Any]],
    isolate_store: bool,
    torn_writes: bool,
    strict_keyless: bool,
    only: Optional[Sequence[Tuple[Crash, ...]]] = None,
    stop: Optional[Callable[[Finding], bool]] = None,
    strict_dirs: bool = False,
    sample: Optional[int] = None,
    check_determinism: bool = False,
) -> ConformanceReport:
    modes = (AFTER, BEFORE) if crash_before else (AFTER,)
    bctx = Context(run_id, EffectLog(), fs=SimFS(dir_durability=strict_dirs))
    baseline_agent = _run(factory, inputs, bctx)
    base = _Baseline(bctx.effects.effects, _snapshot(baseline_agent), list(bctx.points))
    report = _explore(
        run_id, base, lambda crashes: _play(factory, inputs, run_id, crashes, isolate_store, strict_dirs),
        modes=modes, torn=torn_writes, max_crashes=max_crashes, max_scenarios=max_scenarios,
        check_payloads=check_payloads, payload_key=payload_key, strict_keyless=strict_keyless,
        only=only, stop=stop, sample=sample,
    )
    if check_determinism and base.kinds:
        note = _nondeterminism(factory, inputs, run_id, base, strict_dirs)
        if note:
            report.warnings.append(note)
    return report


def _nondeterminism(factory: Callable[[Context], Any], inputs: Sequence[Any], run_id: str,
                    base: "_Baseline", strict_dirs: bool) -> Optional[str]:
    """Run the crash-free baseline a second time under another run id; a subject whose
    crash points or keys change between identical runs makes crash numbers meaningless."""
    other = f"{run_id}~det"
    ctx = Context(other, EffectLog(), fs=SimFS(dir_durability=strict_dirs))
    try:
        _run(factory, inputs, ctx)
    except Exception as exc:  # noqa: BLE001 - only a warning
        return f"second crash-free baseline run raised {type(exc).__name__}: {_r(exc)} - is the subject deterministic?"

    def keys(es: Sequence[Effect], rid: str) -> List[str]:
        return [str(e.key).replace(rid, "<run>") if e.key is not None else "" for e in es]

    if ctx.points != base.kinds or keys(ctx.effects.effects, other) != keys(base.effects, run_id):
        return ("two identical crash-free runs issued different crash points or keys - the subject looks "
                "nondeterministic (time, randomness, dict/set order, or state not keyed by ctx.run_id?), "
                "so crash point numbers may not line up")
    return None


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
    torn_writes: bool = False,
    strict_keyless: bool = False,
    thorough: bool = False,
    minimize: bool = False,
    fail_fast: bool = False,
    strict_dirs: bool = False,
    sample: Optional[int] = None,
    check_determinism: bool = False,
) -> ConformanceReport:
    """Run ``factory(ctx) -> subject`` to completion, then re-run it crashing at
    every crash point and compare against the baseline.

    Crash points are every ``ctx.effect`` / ``ctx.tool`` call and every mutating
    ``ctx.fs`` call (write, fsync, rename, remove).

    * ``crash_before`` - also crash just *before* each point commits (catches
      subjects that checkpoint "done" before doing the work -> ``effect_lost``).
    * ``max_crashes`` - crash up to this many times per scenario; 2 also crashes
      the resume at each of its own points (crash-during-recovery).
    * ``max_scenarios`` - safety cap on scenarios executed; ``None`` for no cap.
      Hitting it adds a warning, so a truncated run is never silent. Pending
      scenarios are bounded by the cap too, so memory stays O(max_scenarios).
    * ``torn_writes`` - also crash after each ``ctx.fs.write`` with the un-fsynced
      tail torn to half (a power cut mid-write). Un-fsynced data is always lost.
    * ``strict_keyless`` - keyless effects that repeat (or vanish) become
      findings instead of warnings.
    * ``thorough`` - ``crash_before`` + ``max_crashes>=2`` + ``torn_writes`` +
      ``strict_keyless`` in one switch.
    * ``minimize`` - for each kind of finding, shrink the inputs (delta debugging)
      and the crash list to the smallest reproduction; see ``report.repros``.
    * ``fail_fast`` - stop at the first finding.
    * ``strict_dirs`` - stricter file-name model: creates, renames and removes are
      lost on a power cut until ``ctx.fs.fsync_dir(dir)`` (not part of ``thorough``).
    * ``check_determinism`` - run the crash-free baseline a second time (under another
      run id) and warn if crash points or keys differ; runs your tools once more, so
      it is off by default and on in ``thorough``.
    * ``sample`` - test only this many evenly spaced crash points per crash kind and
      level (huge inputs); the report warns how many points were skipped.
    * ``check_payloads`` / ``payload_key`` - opt in to ``effect_diverged``: compare
      the payload of each keyed effect issued after a crash with the baseline's.
      Off by default (request ids and timestamps legitimately differ). Passing
      ``payload_key=lambda p: p["amount"]`` turns it on and compares only that.
    * ``isolate_store`` - give each new process life a deep copy of ``ctx.store``
      so live objects (iterators, locks, the old agent) cannot survive a crash.
      ``False`` shares one live dict between lives (faster, less realistic).

    Each scenario is independent: the factory is called again from scratch and
    ``ctx.store`` / ``ctx.fs`` start empty. Key any other durable state by
    ``ctx.run_id``.
    """
    _validate(max_crashes, max_scenarios, sample)
    if check_payloads is None:
        check_payloads = payload_key is not None
    if thorough:
        crash_before, torn_writes, strict_keyless = True, True, True
        max_crashes = max(max_crashes, 2)
        check_determinism = True
    inputs = list(inputs)
    opts: Dict[str, Any] = dict(
        crash_before=crash_before, max_crashes=max_crashes, max_scenarios=max_scenarios,
        check_payloads=check_payloads, payload_key=payload_key, isolate_store=isolate_store,
        torn_writes=torn_writes, strict_keyless=strict_keyless, strict_dirs=strict_dirs, sample=sample,
    )
    report = _conform(factory, inputs, run_id, **opts, stop=(lambda f: True) if fail_fast else None,
                      check_determinism=check_determinism)
    if minimize and report.findings:
        report.repros = _minimize(factory, inputs, run_id, report, opts)
    return report


def replay(
    factory: Callable[[Context], Any],
    inputs: Iterable[Any],
    crashes: Sequence[Tuple[int, str]],
    run_id: str = "run",
    **options: Any,
) -> ConformanceReport:
    """Re-run exactly one scenario (e.g. ``Finding.crashes``) for debugging.

    ``crashes`` is a list of ``(point, when)`` with ``when`` in
    ``"after" | "before" | "torn"``. Accepts the same options as ``conform``.
    """
    crashes = tuple((int(k), w) for k, w in crashes)
    if not crashes or any(w not in _WHENS or k < 1 for k, w in crashes):
        raise ValueError(f"crashes must be a non-empty list of (point>=1, one of {_WHENS})")
    options.setdefault("max_crashes", len(crashes))
    options.pop("minimize", None)
    options.pop("fail_fast", None)
    options.pop("thorough", None)
    opts: Dict[str, Any] = dict(
        crash_before=False, max_crashes=len(crashes), max_scenarios=None, check_payloads=None, payload_key=None,
        isolate_store=True, torn_writes=False, strict_keyless=False, strict_dirs=False, sample=None,
    )
    opts.update(options)
    if opts["check_payloads"] is None:
        opts["check_payloads"] = opts["payload_key"] is not None
    return _conform(factory, list(inputs), run_id, **opts, only=[crashes])


def explain(
    factory: Callable[[Context], Any],
    inputs: Iterable[Any],
    crashes: Sequence[Tuple[int, str]],
    run_id: str = "run",
    **options: Any,
) -> str:
    """Human-readable timeline of ONE scenario: every crash point each process life
    passed (effects with key and payload, file operations), where it was killed, what
    the baseline did, and what the checks concluded. Pass ``Finding.crashes``.

    Accepts ``check_payloads``, ``payload_key``, ``strict_keyless``, ``strict_dirs``
    and ``isolate_store`` like ``conform``.
    """
    crashes = tuple((int(k), w) for k, w in crashes)
    if not crashes or any(w not in _WHENS or k < 1 for k, w in crashes):
        raise ValueError(f"crashes must be a non-empty list of (point>=1, one of {_WHENS})")
    inputs = list(inputs)
    strict_dirs = bool(options.get("strict_dirs", False))
    payload_key = options.get("payload_key")
    check_payloads = options.get("check_payloads")
    if check_payloads is None:
        check_payloads = payload_key is not None
    bctx = Context(run_id, EffectLog(), fs=SimFS(dir_durability=strict_dirs))
    base_agent = _run(factory, inputs, bctx)
    base = _Baseline(bctx.effects.effects, _snapshot(base_agent), list(bctx.points))
    lines: List[str] = []
    out = _play(factory, inputs, run_id, crashes, options.get("isolate_store", True), strict_dirs, trace=lines)
    sid = _scenario_id(run_id, crashes)
    rep = ConformanceReport()
    if out.error is not None:
        rep.findings.append(out.error)
    else:
        _check(rep, sid, crashes[0][0], out, base.effects, base.state, check_payloads, payload_key,
               bool(options.get("strict_keyless", False)), crashes)
    head = [f"scenario {sid}: " + ", then ".join(f"crash {w} point #{k}" for k, w in crashes)]
    head.append(f"baseline (no crash): {len(base.kinds)} crash point(s) ({', '.join(sorted(set(base.kinds)))})")
    head.append(f"baseline keyed effects: {_r([e.key for e in base.effects if e.key is not None])}")
    tail = [f"effects that happened across all lives: {_r([e.key if e.key is not None else e.kind for e in out.log.effects])}"]
    if rep.findings:
        tail += [f"finding [{f.kind}]: {f.detail}" for f in rep.findings]
    else:
        tail.append("no finding for this scenario")
    tail += [f"warning: {w}" for w in rep.warnings]
    return clean_block("\n".join(head + lines + tail))


def clean_block(text: str) -> str:
    """``clean`` for multi-line text (keeps real newlines)."""
    return "\n".join(clean(line) for line in text.split("\n"))


def _ddmin(items: List[Any], test: Callable[[List[Any]], bool], budget: List[int]) -> List[Any]:
    """Classic delta debugging over a list; ``budget`` is a one-element mutable counter."""
    n = 2
    while len(items) >= 2 and budget[0] > 0:
        size = max(1, -(-len(items) // n))
        reduced = False
        for start in range(0, len(items), size):
            comp = items[:start] + items[start + size:]
            if not comp:
                continue
            budget[0] -= 1
            if test(comp):
                items, n, reduced = comp, max(n - 1, 2), True
                break
            if budget[0] <= 0:
                break
        if not reduced:
            if n >= len(items):
                break
            n = min(len(items), n * 2)
    return items


def _minimize(factory: Callable[[Context], Any], inputs: List[Any], run_id: str,
              report: ConformanceReport, opts: Dict[str, Any], max_runs: int = 400) -> List[Repro]:
    seen: Dict[str, Finding] = {}
    for f in report.findings:
        seen.setdefault(f.kind, f)
    counter = [0]
    repros: List[Repro] = []
    budget = [max_runs]

    def find(cur: List[Any], kind: str) -> Optional[Finding]:
        counter[0] += 1
        rep = _conform(factory, cur, f"{run_id}~m{counter[0]}", **{**opts, "max_scenarios": opts["max_scenarios"]},
                       stop=lambda f: f.kind == kind)
        return next((f for f in rep.findings if f.kind == kind), None)

    for kind in seen:
        small = _ddmin(list(inputs), lambda cur, k=kind: find(cur, k) is not None, budget)
        hit = find(small, kind)
        if hit is None:  # budget ran out mid-way or the finding was flaky: fall back to the original
            small, hit = list(inputs), seen[kind]
        repros.append(Repro(kind, tuple(small), hit.crashes, hit.detail))
    return repros
