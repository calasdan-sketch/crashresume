"""Child side of process mode: run the subject for one process life, and really die.

Invoked as ``python -m crashresume._child <spec.json>`` by ``conform_process``. At the
armed crash point the interpreter is killed with SIGKILL (``os._exit(137)`` where
SIGKILL does not exist): no ``finally`` blocks, no ``atexit``, no buffered flushes.
"""
from __future__ import annotations

import importlib
import importlib.util
import json
import os
import signal
import sys
import traceback
from typing import Any, Dict, List, Optional

from .core import AFTER, Context, EffectLog, _snapshot
from .fs import RealFS

EXIT_SETUP = 70
DIED = (-9, 137)


class _NoStore(dict):
    """ctx.store does not survive a real kill; fail loudly instead of silently losing data."""

    def _no(self, *a: Any, **k: Any) -> Any:
        raise RuntimeError("ctx.store is unavailable in process mode; keep durable state in ctx.fs (a real directory)")

    __getitem__ = __setitem__ = __contains__ = __iter__ = __len__ = _no  # type: ignore[assignment]
    get = setdefault = update = pop = items = keys = values = _no  # type: ignore[assignment]


def load_ledger(path: str, repair: bool = False) -> List[Dict[str, Any]]:
    """Records of an effect ledger. A torn final line (no trailing newline: the writer was
    killed mid-append by a timeout) is dropped, and cut from the file when ``repair`` is
    set so the next append starts on a clean line. A bad line anywhere else raises
    ``ValueError`` (fail closed)."""
    if not os.path.exists(path):
        return []
    with open(path, "rb") as fh:
        raw = fh.read()
    *complete, tail = raw.split(b"\n")
    out: List[Dict[str, Any]] = []
    for i, chunk in enumerate(complete, 1):
        if not chunk.strip():
            continue
        try:
            d = json.loads(chunk.decode("utf-8"))
            if not isinstance(d, dict) or not {"kind", "payload", "key"} <= set(d):
                raise ValueError("not a ledger record")
        except ValueError:
            raise ValueError(f"effect ledger {path!r} is corrupt at line {i}") from None
        out.append(d)
    if tail and repair:
        with open(path, "r+b") as fh:
            fh.truncate(len(raw) - len(tail))
    return out


class _FileLog(EffectLog):
    """Effect ledger on disk, fsynced per record, reloaded by every process life."""

    def __init__(self, path: str) -> None:
        super().__init__()
        self._path = path
        for d in load_ledger(path, repair=True):
            super().record(d["kind"], d["payload"], d["key"])

    def record(self, kind: str, payload: Any = None, key: Optional[str] = None) -> Any:
        if key is not None and not isinstance(key, (str, int)):
            raise TypeError("process mode needs str or int idempotency keys (they cross a process boundary as JSON)")
        payload = json.loads(json.dumps(payload, default=repr))
        line = json.dumps({"kind": kind, "payload": payload, "key": key}, default=repr)
        with open(self._path, "a", encoding="utf-8", newline="\n") as fh:  # "\n" even on Windows
            fh.write(line + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        return super().record(kind, payload, key)


class _ProcContext(Context):
    def _die(self, message: str) -> None:
        if hasattr(signal, "SIGKILL"):
            os.kill(os.getpid(), signal.SIGKILL)
        os._exit(137)


def _load(target: str) -> Any:
    mod_name, _, attr = target.rpartition(":")
    if not mod_name or not attr:
        raise ValueError(f"target must look like 'package.module:factory', got {target!r}")
    if mod_name.endswith(".py"):
        spec = importlib.util.spec_from_file_location("_crashresume_target", mod_name)
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot load {mod_name}")
        mod = importlib.util.module_from_spec(spec)
        sys.modules["_crashresume_target"] = mod
        spec.loader.exec_module(mod)
    else:
        mod = importlib.import_module(mod_name)
    obj = mod
    for part in attr.split("."):
        obj = getattr(obj, part)
    return obj


def main(argv: List[str]) -> int:
    try:
        with open(argv[1], "r", encoding="utf-8") as fh:
            spec: Dict[str, Any] = json.load(fh)
        sys.path.insert(0, os.getcwd())
        factory = _load(spec["target"])
        if not callable(factory):
            raise TypeError(f"{spec['target']} is not callable")
    except BaseException:  # noqa: BLE001
        traceback.print_exc()
        return EXIT_SETUP
    d = spec["dir"]
    crash_at = spec.get("crash_at")
    ctx = _ProcContext(spec["run_id"], _FileLog(os.path.join(d, "effects.jsonl")), crash_at=crash_at,
                       store=_NoStore(), crash_when=spec.get("when", AFTER), fs=RealFS(os.path.join(d, "data")))
    try:
        agent = factory(ctx)
        for item in spec["inputs"]:
            agent.step(item)
        state = json.loads(json.dumps(_snapshot(agent), default=repr))
    except Exception:  # noqa: BLE001 - reported to the parent as subject_error
        traceback.print_exc()
        return 1
    with open(os.path.join(d, "result.json"), "w", encoding="utf-8") as fh:
        json.dump({"state": state, "kinds": ctx.points}, fh)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main(sys.argv))
