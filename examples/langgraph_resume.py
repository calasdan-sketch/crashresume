"""Crash-test a real LangGraph graph (tested with langgraph 1.2.20; pip install langgraph).

    python examples/langgraph_resume.py

A node that dies mid-run has not checkpointed, so LangGraph re-runs it on resume and
its tool fires again. The first run shows that; the second guards each tool with the
effect ledger (the provider-side idempotency check) and passes the crash tests. The third run
adds the stale-worker test: two workers on the same LangGraph thread (a lease that expired while
the first worker was paused) - the checkpointer does not fence the first one, so the guarded graph
fails it; passing a fencing token to each tool (``fence=ctx.generation``) fixes that.
"""
from __future__ import annotations

from typing import TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph

from crashresume import conform

SAVERS: dict = {}   # one checkpointer per crash scenario, keyed by ctx.run_id


class State(TypedDict, total=False):
    item: str


def build(guarded: bool, fenced: bool = False):
    def factory(ctx):
        saver = SAVERS.setdefault(ctx.run_id, InMemorySaver())

        def tool(kind):
            call = ctx.tool(lambda item: "ok", kind=kind, key=lambda item: f"{kind}:{item}",
                            fence=ctx.generation if fenced else None)

            def run(item):
                if guarded and f"{kind}:{item}" in ctx.effects.keys():
                    return "already done"       # idempotent: the provider already has it
                return call(item)
            return run

        charge, email = tool("charge"), tool("email")
        g = StateGraph(State)
        g.add_node("charge", lambda s: charge(s["item"]) and {})
        g.add_node("email", lambda s: email(s["item"]) and {})
        g.add_edge(START, "charge")
        g.add_edge("charge", "email")
        g.add_edge("email", END)
        app = g.compile(checkpointer=saver)

        class Agent:
            def step(self, item):
                cfg = {"configurable": {"thread_id": item}}
                snap = app.get_state(cfg)
                if snap.next:
                    app.invoke(None, cfg)               # resume the interrupted run
                elif not snap.values:
                    app.invoke({"item": item}, cfg)

            def state(self):
                return sorted(ctx.effects.keys())
        return Agent()
    return factory


if __name__ == "__main__":
    for guarded in (False, True):
        report = conform(build(guarded), ["a", "b"], run_id=f"lg-{guarded}", crash_before=True)
        print(f"guarded={guarded}:", "PASS" if report.passed else f"{len(report.findings)} finding(s)")
        if not report.passed:
            print(report.findings[0])
    for fenced in (False, True):
        report = conform(build(True, fenced), ["a", "b"], run_id=f"lg-stale-{fenced}", faults=True, stale_worker=True)
        print(f"guarded=True fenced={fenced} (timeouts, refusals, stale worker):",
              "PASS" if report.passed else f"{len(report.findings)} finding(s)")
        if not report.passed:
            print(report.findings[0])
