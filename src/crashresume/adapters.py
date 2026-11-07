"""Adapters: point crashresume at agents that are not a plain ``step()`` object.

The whole contract an agent framework has to meet is tiny:

1. route each real side effect through ``ctx.tool(fn, key=...)`` (or ``ctx.effect``);
2. keep durable state in ``ctx.store`` / ``ctx.fs`` (or your own store keyed by
   ``ctx.run_id``) and rebuild the agent from it in the factory.

Then ``conform`` can kill it anywhere. Helpers here cover the common shapes.
"""
from __future__ import annotations

import asyncio
import threading
from typing import Any, Callable, Optional

from .core import Context


class _AsyncSubject:
    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def step(self, item: Any) -> Any:
        coro = self._inner.step(item)
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(coro)
        # called from inside a running loop (e.g. pytest-asyncio): use a private thread
        box: dict = {}

        def runner() -> None:
            try:
                box["value"] = asyncio.run(coro)
            except BaseException as exc:  # noqa: BLE001 - re-raised below, InjectedCrash included
                box["error"] = exc

        t = threading.Thread(target=runner)
        t.start()
        t.join()
        if "error" in box:
            raise box["error"]
        return box.get("value")

    def state(self) -> Any:
        state = getattr(self._inner, "state", None)
        if callable(state):
            return state()
        return {k: v for k, v in vars(self._inner).items() if not k.startswith("_") and k != "ctx"}


def from_async(factory: Callable[[Context], Any]) -> Callable[[Context], Any]:
    """Wrap a factory whose subject has ``async def step(item)`` so ``conform`` can drive it."""
    return lambda ctx: _AsyncSubject(factory(ctx))


class _FunctionSubject:
    def __init__(self, ctx: Context, fn: Callable[[Context, Any], Any], state: Optional[Callable[[Context], Any]]) -> None:
        self.ctx, self._fn, self._state = ctx, fn, state

    def step(self, item: Any) -> Any:
        return self._fn(self.ctx, item)

    def state(self) -> Any:
        return self._state(self.ctx) if self._state else dict(self.ctx.store)


def from_function(
    fn: Callable[[Context, Any], Any], state: Optional[Callable[[Context], Any]] = None
) -> Callable[[Context], Any]:
    """Turn a plain ``fn(ctx, item)`` agent loop into a factory. State defaults to ``ctx.store``."""
    return lambda ctx: _FunctionSubject(ctx, fn, state)
