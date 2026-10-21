"""crashresume CLI: a self-test demo that exercises good and bad agents."""
from __future__ import annotations

import argparse
import sys
from typing import Any, Dict

from . import __version__
from .core import Context, InjectedCrash, conform


class _Store:
    """Stand-in for durable storage shared across process restarts."""

    def __init__(self) -> None:
        self.data: Dict[str, Any] = {}


class _Stores:
    """Per-scenario durable stores, isolated by ctx.run_id (see README)."""

    def __init__(self) -> None:
        self.d: Dict[str, _Store] = {}

    def get(self, ctx) -> "_Store":
        return self.d.setdefault(ctx.run_id, _Store())


class GoodAgent:
    """Persists after every input and reconciles against committed effects."""

    def __init__(self, ctx: Context, store: _Store) -> None:
        self.ctx = ctx
        self.store = store
        self.done = set(store.data.get("done", set()))

    def step(self, item: str) -> None:
        key = f"process:{item}"
        if item in self.done or key in self.ctx.effects.keys():
            self.done.add(item)
            self.store.data["done"] = set(self.done)
            return
        self.ctx.effect("process", item, key=key)
        self.done.add(item)
        self.store.data["done"] = set(self.done)

    def state(self):
        return {"done": sorted(self.store.data.get("done", set()))}


class ReplayAgent:
    """Persists state but re-issues effects for everything on resume."""

    def __init__(self, ctx: Context, store: _Store) -> None:
        self.ctx = ctx
        self.store = store

    def step(self, item: str) -> None:
        self.ctx.effect("process", item, key=f"process:{item}")  # always fires
        self.store.data.setdefault("done", set()).add(item)

    def state(self):
        return {"done": sorted(self.store.data.get("done", set()))}


class LostAgent:
    """Loses its checkpoint and wrongly believes the stream is exhausted."""

    def __init__(self, ctx: Context, store: _Store) -> None:
        self.ctx = ctx
        self.store = store

    def step(self, item: str) -> None:
        if self.store.data.get("exhausted"):
            return
        self.ctx.effect("process", item, key=f"process:{item}")
        self.store.data.setdefault("done", []).append(item)

    def state(self):
        return {"done": list(self.store.data.get("done", []))}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="crashresume", description=__doc__.splitlines()[0])
    ap.add_argument("--version", action="version", version=f"crashresume {__version__}")
    ap.add_argument("--selftest", action="store_true", help="run the built-in good/bad agent demo")
    args = ap.parse_args(argv)
    if not args.selftest:
        ap.print_help()
        return 0

    inputs = ["a", "b", "c"]
    ok = True

    good_stores = _Stores()
    good = conform(lambda ctx: GoodAgent(ctx, good_stores.get(ctx)), inputs)
    print("GoodAgent (persist + reconcile against committed effects):")
    print(" ", good)
    ok &= good.passed

    replay_stores = _Stores()
    replay = conform(lambda ctx: ReplayAgent(ctx, replay_stores.get(ctx)), inputs)
    print("ReplayAgent (re-fires effects on resume):")
    print(" ", replay)
    ok &= not replay.passed and any(f.kind == "effect_replayed" for f in replay.findings)

    lost_stores = _Stores()

    def make_lost(ctx):
        store = lost_stores.get(ctx)
        if ctx.run_id != "run":  # checkpoint lost in the crash
            store.data["done"] = []
            store.data["exhausted"] = True
        return LostAgent(ctx, store)

    lost = conform(make_lost, inputs)
    print("LostAgent (checkpoint lost, wrongly believes stream is done):")
    print(" ", lost)
    ok &= not lost.passed and any(f.kind == "state_lost" for f in lost.findings)

    print("\nselftest:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
