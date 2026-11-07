"""Process mode: really kill the subject.

``conform`` simulates a crash by raising ``InjectedCrash`` inside one interpreter,
which means ``finally`` blocks, context managers and ``atexit`` handlers still run
and in-memory caches survive. ``conform_process`` runs every process life in a fresh
interpreter and kills it with SIGKILL at the crash point, so those whole classes of
"it only works because cleanup ran" bugs show up.

Rules: the target is ``"package.module:factory"`` (or ``"path/to/file.py:factory"``);
inputs must be JSON; idempotency keys must be str or int; durable state lives in
``ctx.fs`` (a real directory) because ``ctx.store`` cannot survive a kill.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Any, Callable, Dict, Iterable, Optional, Tuple

from ._child import DIED, EXIT_SETUP
from .core import (
    AFTER,
    BEFORE,
    ConformanceReport,
    Crash,
    EffectLog,
    Finding,
    _Baseline,
    _explore,
    _Outcome,
    _scenario_id,
    _validate,
    clean,
)

_SAFE = re.compile(r"[^A-Za-z0-9._-]")


class SetupError(RuntimeError):
    """The target could not be loaded or the harness could not start a child (fail closed)."""


def _read_ledger(path: str) -> EffectLog:
    log = EffectLog()
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                d = json.loads(line)
                log.record(d["kind"], d["payload"], d["key"])
    return log


def conform_process(
    target: str,
    inputs: Iterable[Any],
    run_id: str = "run",
    *,
    crash_before: bool = False,
    max_crashes: int = 1,
    max_scenarios: Optional[int] = 2_000,
    timeout: float = 30.0,
    check_payloads: Optional[bool] = None,
    payload_key: Optional[Callable[[Any], Any]] = None,
    strict_keyless: bool = False,
    thorough: bool = False,
    python: Optional[str] = None,
    workdir: Optional[str] = None,
) -> ConformanceReport:
    """Like ``conform`` but each process life is a real subprocess killed with SIGKILL."""
    _validate(max_crashes, max_scenarios)
    if check_payloads is None:
        check_payloads = payload_key is not None
    if thorough:
        crash_before, strict_keyless = True, True
        max_crashes = max(max_crashes, 2)
    inputs = list(inputs)
    try:
        json.dumps(inputs)
    except (TypeError, ValueError) as exc:
        raise TypeError(f"process mode needs JSON-serializable inputs: {exc}") from None
    py = python or sys.executable
    if workdir is not None and os.path.exists(workdir) and os.listdir(workdir):
        raise ValueError(f"workdir {workdir!r} must be empty or not exist (stale ledgers would corrupt the baseline)")
    root = workdir or tempfile.mkdtemp(prefix="crashresume-")
    os.makedirs(root, exist_ok=True)
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([p for p in sys.path if p] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))
    env["PYTHONUTF8"] = "1"
    counter = [0]

    def life(sid: str, crash: Optional[Crash]) -> Tuple[Optional[int], str]:
        """Run one process life. Returns (returncode, stderr tail); returncode None = timeout."""
        d = os.path.join(root, _SAFE.sub("_", sid))
        os.makedirs(d, exist_ok=True)
        counter[0] += 1
        spec_path = os.path.join(root, f"spec-{counter[0]}.json")
        spec: Dict[str, Any] = {"target": target, "inputs": inputs, "dir": d, "run_id": sid}
        if crash:
            spec["crash_at"], spec["when"] = crash
        with open(spec_path, "w", encoding="utf-8") as fh:
            json.dump(spec, fh)
        try:
            p = subprocess.run([py, "-m", "crashresume._child", spec_path], env=env, capture_output=True,
                               timeout=timeout, cwd=os.getcwd())
        except subprocess.TimeoutExpired:
            return None, ""
        except OSError as exc:
            raise SetupError(f"cannot start {py!r}: {exc}") from exc
        err = p.stderr.decode("utf-8", errors="replace").strip().splitlines()
        tail = clean(err[-1]) if err else ""
        if p.returncode == EXIT_SETUP:
            raise SetupError(f"cannot load {target!r}:\n{clean(chr(10).join(err[-8:]))}")
        return p.returncode, tail

    def result(sid: str) -> Dict[str, Any]:
        with open(os.path.join(root, _SAFE.sub("_", sid), "result.json"), "r", encoding="utf-8") as fh:
            return json.load(fh)

    def play(crashes: Tuple[Crash, ...]) -> _Outcome:
        sid = _scenario_id(run_id, crashes)
        first = crashes[0][0]
        d = os.path.join(root, _SAFE.sub("_", sid))
        ledger = os.path.join(d, "effects.jsonl")
        out = _Outcome(log=EffectLog())

        def fail(kind: str, detail: str) -> _Outcome:
            out.log = _read_ledger(ledger)
            out.error = Finding(kind, first, detail, sid, crashes[0][1], tuple(crashes))
            return out

        for n, crash in enumerate(crashes, 1):
            rc, tail = life(sid, crash)
            if rc is None:
                return fail("subject_hang", f"process life {n} exceeded {timeout}s and was killed")
            if rc in DIED:
                if n == 1:
                    out.first_life_effects = len(_read_ledger(ledger))
                continue
            if rc != 0:
                return fail("subject_error", f"process life {n} exited {rc}: {tail}")
            out.unreached = (f"{sid}: crash {crash[1]} point #{crash[0]} never reached in process life {n} "
                             "(subject issued fewer effects than the baseline - nondeterministic?); resumed without it")
            if n == 1:
                out.first_life_effects = len(_read_ledger(ledger))
            break
        before = len(_read_ledger(ledger))
        rc, tail = life(sid, None)
        if rc is None:
            return fail("subject_hang", f"resume exceeded {timeout}s and was killed")
        if rc != 0:
            return fail("subject_error", f"resume exited {rc}: {tail}")
        out.log = _read_ledger(ledger)
        res = result(sid)
        out.final_state = res["state"]
        out.last_life_effects = len(out.log) - before
        out.last_life_kinds = list(res["kinds"])
        return out

    try:
        rc, tail = life(run_id, None)
        if rc is None:
            raise SetupError(f"baseline run exceeded {timeout}s")
        if rc != 0:
            raise SetupError(f"baseline (crash-free) run failed with exit {rc}: {tail}")
        res = result(run_id)
        base = _Baseline(_read_ledger(os.path.join(root, _SAFE.sub("_", run_id), "effects.jsonl")).effects,
                         res["state"], list(res["kinds"]))
        modes = (AFTER, BEFORE) if crash_before else (AFTER,)
        report = _explore(run_id, base, play, modes=modes, torn=False, max_crashes=max_crashes,
                          max_scenarios=max_scenarios, check_payloads=check_payloads, payload_key=payload_key,
                          strict_keyless=strict_keyless)
        report.warnings.insert(0, "process mode: crashes are real SIGKILLs; torn writes are not modelled here (use conform)")
        return report
    finally:
        if workdir is None:
            shutil.rmtree(root, ignore_errors=True)


__all__ = ["conform_process", "SetupError"]
