"""A corpus of intentionally correct and intentionally broken sample agents.

Each entry has a ground-truth label: ``expected`` is the set of finding kinds that
count as a correct detection (empty set = the agent is correct and must pass).
The agents are small on purpose - each isolates exactly one crash-safety bug class
that real agent code has, so the benchmark measures *detection*, not agent size.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, FrozenSet, List

from crashresume.adapters import from_function

INPUTS = ["a", "b", "c"]


@dataclass(frozen=True)
class Case:
    name: str
    factory: Callable[[Any], Any]
    expected: FrozenSet[str]            # empty => correct agent, must produce no finding
    why: str
    inputs: List[Any] = field(default_factory=lambda: list(INPUTS))


def _done(ctx):
    return ctx.store.setdefault("done", [])


# --------------------------------------------------------------------------- correct
class ReconcileLedger:
    """Checkpoint after the work, reconcile against the effect ledger (provider-side dedup)."""

    def __init__(self, ctx):
        self.ctx = ctx
        _done(ctx)

    def step(self, item):
        if item in self.ctx.store["done"]:
            return
        if f"pay:{item}" not in self.ctx.effects.keys():
            self.ctx.effect("pay", item, key=f"pay:{item}")
        self.ctx.store["done"].append(item)

    def state(self):
        return sorted(self.ctx.store["done"])


class LedgerOnly:
    """No checkpoint at all: the effect ledger is the only memory."""

    def __init__(self, ctx):
        self.ctx = ctx

    def step(self, item):
        if f"pay:{item}" not in self.ctx.effects.keys():
            self.ctx.effect("pay", item, key=f"pay:{item}")

    def state(self):
        return sorted(k.split(":")[1] for k in self.ctx.effects.keys())


class TwoEffectsReconciled:
    """Charge then email, each guarded by its own key."""

    def __init__(self, ctx):
        self.ctx = ctx

    def step(self, item):
        keys = self.ctx.effects.keys()
        if f"charge:{item}" not in keys:
            self.ctx.effect("charge", item, key=f"charge:{item}")
        if f"email:{item}" not in self.ctx.effects.keys():
            self.ctx.effect("email", item, key=f"email:{item}")

    def state(self):
        return sorted(self.ctx.effects.keys())


class WriteAheadIntent:
    """Intent -> effect -> done; recovery re-checks the ledger for intents without done."""

    def __init__(self, ctx):
        self.ctx = ctx
        ctx.store.setdefault("intent", [])
        ctx.store.setdefault("done", [])

    def step(self, item):
        s = self.ctx.store
        if item in s["done"]:
            return
        if item not in s["intent"]:
            s["intent"].append(item)
        if f"pay:{item}" not in self.ctx.effects.keys():
            self.ctx.effect("pay", item, key=f"pay:{item}")
        s["done"].append(item)

    def state(self):
        return sorted(self.ctx.store["done"])


class ToolWrappedGood:
    """Real tool wrapped with ctx.tool; guarded by the ledger."""

    def __init__(self, ctx):
        self.ctx = ctx
        self.send = ctx.tool(lambda who: f"sent:{who}", kind="email", key=lambda who: f"mail:{who}")

    def step(self, item):
        if f"mail:{item}" not in self.ctx.effects.keys():
            self.send(item)

    def state(self):
        return sorted(self.ctx.effects.keys())


class PayloadStable:
    """Re-issues the same payload after a crash (deterministic)."""

    def __init__(self, ctx):
        self.ctx = ctx
        self.ctx.store.setdefault("n", 0)

    def step(self, item):
        if f"pay:{item}" not in self.ctx.effects.keys():
            self.ctx.effect("pay", {"item": item, "amount": 5}, key=f"pay:{item}")

    def state(self):
        return sorted(self.ctx.effects.keys())


class FsAtomicArtifact:
    """Artifact and cursor both written fsync+rename; effect guarded by ledger."""

    def __init__(self, ctx):
        self.ctx = ctx

    def _done(self):
        fs = self.ctx.fs
        return fs.read_text("cursor").split() if fs.exists("cursor") else []

    def step(self, item):
        fs = self.ctx.fs
        done = self._done()
        if item in done:
            return
        if f"pub:{item}" not in self.ctx.effects.keys():
            self.ctx.effect("publish", item, key=f"pub:{item}")
        fs.write_atomic(f"out/{item}.txt", f"report for {item}")
        fs.write_atomic("cursor", " ".join(done + [item]))

    def state(self):
        fs = self.ctx.fs
        return {i: fs.read_text(f"out/{i}.txt") for i in self._done()}


class FsJournalFsync:
    """Append-only journal, fsync after each append, recovery drops a torn last line."""

    def __init__(self, ctx):
        self.ctx = ctx
        fs = ctx.fs
        if fs.exists("journal"):                      # recovery: cut a torn tail back to the last newline
            raw = fs.read_text("journal")
            if raw and not raw.endswith("\n"):
                fs.write_atomic("journal", raw[: raw.rfind("\n") + 1])

    def _done(self):
        fs = self.ctx.fs
        if not fs.exists("journal"):
            return []
        raw = fs.read_text("journal")
        lines = raw.split("\n")
        return [ln[5:] for ln in lines[:-1] if ln.startswith("done:")]   # last element has no newline: drop it

    def step(self, item):
        fs = self.ctx.fs
        if item in self._done():
            return
        if f"pub:{item}" not in self.ctx.effects.keys():
            self.ctx.effect("publish", item, key=f"pub:{item}")
        fs.write("journal", f"done:{item}\n", append=True)
        fs.fsync("journal")

    def state(self):
        return self._done()


# --------------------------------------------------------------------------- broken
class ReplayNoReconcile:
    """Persists progress after the effect but never checks whether the effect already happened."""

    def __init__(self, ctx):
        self.ctx = ctx
        _done(ctx)

    def step(self, item):
        if item in self.ctx.store["done"]:
            return
        self.ctx.effect("pay", item, key=f"pay:{item}")
        self.ctx.store["done"].append(item)

    def state(self):
        return sorted(self.ctx.store["done"])


class EagerCheckpoint:
    """Marks the item done before doing the work."""

    def __init__(self, ctx):
        self.ctx = ctx
        _done(ctx)

    def step(self, item):
        if item in self.ctx.store["done"]:
            return
        self.ctx.store["done"].append(item)
        self.ctx.effect("pay", item, key=f"pay:{item}")

    def state(self):
        return sorted(self.ctx.store["done"])


class RestartMeansEof:
    """Treats any restart as 'the stream already ended'."""

    def __init__(self, ctx):
        self.ctx = ctx
        _done(ctx)
        if ctx.store.get("started"):
            ctx.store["eof"] = True
        ctx.store["started"] = True

    def step(self, item):
        if self.ctx.store.get("eof") or item in self.ctx.store["done"]:
            return
        self.ctx.effect("pay", item, key=f"pay:{item}")
        self.ctx.store["done"].append(item)

    def state(self):
        return sorted(self.ctx.store["done"])


class OneShotRecovery:
    """Heals from the ledger on the first restart only; the flag is set before healing finishes."""

    def __init__(self, ctx):
        self.ctx = ctx
        st = ctx.store
        st.setdefault("done", [])
        if st.get("started") and not st.get("recovered"):
            st["recovered"] = True
            st["done"] = sorted(set(st["done"]) | {k.split(":", 1)[1] for k in ctx.effects.keys()})
        st["started"] = True

    def step(self, item):
        if item in self.ctx.store["done"]:
            return
        self.ctx.effect("pay", item, key=f"pay:{item}")
        self.ctx.store["done"].append(item)

    def state(self):
        return sorted(self.ctx.store["done"])


class PartialBatch:
    """Two effects per item but only the first is used to decide 'already handled'."""

    def __init__(self, ctx):
        self.ctx = ctx

    def step(self, item):
        if f"charge:{item}" in self.ctx.effects.keys():
            return
        self.ctx.effect("charge", item, key=f"charge:{item}")
        self.ctx.effect("email", item, key=f"email:{item}")

    def state(self):
        return sorted(self.ctx.effects.keys())


class PayloadDrift:
    """Re-issues a keyed effect after a crash with a per-process counter in the payload."""

    _proc = [0]

    def __init__(self, ctx):
        self.ctx = ctx
        PayloadDrift._proc[0] += 1
        self.life = PayloadDrift._proc[0]

    def step(self, item):
        if f"pay:{item}" not in self.ctx.effects.keys():
            self.ctx.effect("pay", {"item": item, "attempt": self.life}, key=f"pay:{item}")

    def state(self):
        return sorted(self.ctx.effects.keys())


class KeylessDoubleSend:
    """No idempotency key and no ledger check: a retry sends the email twice."""

    def __init__(self, ctx):
        self.ctx = ctx
        _done(ctx)

    def step(self, item):
        if item in self.ctx.store["done"]:
            return
        self.ctx.effect("email", item)
        self.ctx.store["done"].append(item)

    def state(self):
        return sorted(self.ctx.store["done"])


class SwallowsCrash:
    """A broad except BaseException around the work keeps the process 'alive'."""

    def __init__(self, ctx):
        self.ctx = ctx

    def step(self, item):
        try:
            self.ctx.effect("pay", item, key=f"pay:{item}")
        except BaseException:   # noqa: BLE001 - the bug under test
            pass

    def state(self):
        return sorted(self.ctx.effects.keys())


class DoubleCountOnResume:
    """Total is rebuilt by adding every item again after a restart."""

    def __init__(self, ctx):
        self.ctx = ctx
        ctx.store.setdefault("total", 0)
        ctx.store.setdefault("done", [])

    def step(self, item):
        if f"pay:{item}" not in self.ctx.effects.keys():
            self.ctx.effect("pay", item, key=f"pay:{item}")
        self.ctx.store["total"] += 1                      # bug: counted again on every re-run
        if item not in self.ctx.store["done"]:
            self.ctx.store["done"].append(item)

    def state(self):
        return {"total": self.ctx.store["total"]}


class ToolWrappedReplay:
    """ctx.tool-wrapped tool, no ledger check."""

    def __init__(self, ctx):
        self.ctx = ctx
        _done(ctx)
        self.send = ctx.tool(lambda who: f"sent:{who}", kind="email", key=lambda who: f"mail:{who}")

    def step(self, item):
        if item in self.ctx.store["done"]:
            return
        self.send(item)
        self.ctx.store["done"].append(item)

    def state(self):
        return sorted(self.ctx.store["done"])


class FsArtifactNoFsync(FsAtomicArtifact):
    """Cursor is durable but the artifact it points at was never fsynced."""

    def step(self, item):
        fs = self.ctx.fs
        done = self._done()
        if item in done:
            return
        if f"pub:{item}" not in self.ctx.effects.keys():
            self.ctx.effect("publish", item, key=f"pub:{item}")
        fs.write(f"out/{item}.txt", f"report for {item}")               # bug: no fsync
        fs.write_atomic("cursor", " ".join(done + [item]))


class FsRenameNoFsync(FsAtomicArtifact):
    """Artifact via temp file + rename, but the temp file is never fsynced (zero-length file after power loss)."""

    def step(self, item):
        fs = self.ctx.fs
        done = self._done()
        if item in done:
            return
        if f"pub:{item}" not in self.ctx.effects.keys():
            self.ctx.effect("publish", item, key=f"pub:{item}")
        fs.write(f"out/{item}.tmp", f"report for {item}")
        fs.rename(f"out/{item}.tmp", f"out/{item}.txt")                  # bug: rename without fsync
        fs.write_atomic("cursor", " ".join(done + [item]))


class FsTornCursor:
    """JSON cursor rewritten in place (truncate + write + fsync): a torn write corrupts it."""

    def __init__(self, ctx):
        self.ctx = ctx

    def _load(self):
        import json
        fs = self.ctx.fs
        return json.loads(fs.read_text("cursor.json")) if fs.exists("cursor.json") else {"done": []}

    def step(self, item):
        import json
        fs = self.ctx.fs
        cur = self._load()
        if item in cur["done"]:
            return
        if f"pub:{item}" not in self.ctx.effects.keys():
            self.ctx.effect("publish", item, key=f"pub:{item}")
        cur["done"].append(item)
        fs.write("cursor.json", json.dumps(cur))
        fs.fsync("cursor.json")

    def state(self):
        return sorted(self._load()["done"])


class FsTornJournal(FsJournalFsync):
    """Append-only journal read back without validating the last line or repairing the tail."""

    def __init__(self, ctx):
        self.ctx = ctx

    def _done(self):
        fs = self.ctx.fs
        if not fs.exists("journal"):
            return []
        return [ln[5:] for ln in fs.read_text("journal").split("\n") if ln.startswith("done:")]   # bug: keeps torn tail


class FsStaleLock:
    """Takes a lock file around each step and fails on restart when it finds a stale one."""

    def __init__(self, ctx):
        self.ctx = ctx

    def step(self, item):
        fs = self.ctx.fs
        if fs.exists("lock"):
            raise RuntimeError("stale lock from a previous run")
        fs.write("lock", "held")
        if f"pay:{item}" not in self.ctx.effects.keys():
            self.ctx.effect("pay", item, key=f"pay:{item}")
        fs.remove("lock")

    def state(self):
        return sorted(self.ctx.effects.keys())


def _loop(ctx, item):
    # a plain function agent adapted with from_function: no ledger check -> replay
    done = ctx.store.setdefault("done", [])
    if item in done:
        return
    ctx.effect("pay", item, key=f"pay:{item}")
    done.append(item)


def corpus() -> List[Case]:
    ok: FrozenSet[str] = frozenset()
    return [
        # correct agents
        Case("reconcile_ledger", ReconcileLedger, ok, "checkpoint after work + ledger reconcile"),
        Case("ledger_only", LedgerOnly, ok, "ledger is the only memory"),
        Case("two_effects_reconciled", TwoEffectsReconciled, ok, "each effect guarded by its own key"),
        Case("write_ahead_intent", WriteAheadIntent, ok, "intent/effect/done with ledger recheck"),
        Case("tool_wrapped_good", ToolWrappedGood, ok, "ctx.tool wrapper guarded by ledger"),
        Case("payload_stable", PayloadStable, ok, "deterministic payloads"),
        Case("fs_atomic_artifact", FsAtomicArtifact, ok, "fsync+rename for artifact and cursor"),
        Case("fs_journal_fsync", FsJournalFsync, ok, "fsynced append journal, torn tail dropped"),
        # broken agents
        Case("replay_no_reconcile", ReplayNoReconcile, frozenset({"effect_replayed"}), "no check that the effect already ran"),
        Case("eager_checkpoint", EagerCheckpoint, frozenset({"effect_lost"}), "marks done before the work"),
        Case("restart_means_eof", RestartMeansEof, frozenset({"effect_lost", "state_lost", "resume_diverged"}),
             "any restart is treated as end of stream"),
        Case("one_shot_recovery", OneShotRecovery, frozenset({"effect_replayed"}), "recovery only safe once"),
        Case("partial_batch", PartialBatch, frozenset({"effect_lost"}), "second effect of a step lost"),
        Case("payload_drift", PayloadDrift, frozenset({"effect_diverged"}), "re-issued key with a different payload"),
        Case("keyless_double_send", KeylessDoubleSend, frozenset({"effect_replayed"}), "retry duplicates a keyless email"),
        Case("swallows_crash", SwallowsCrash, frozenset({"crash_swallowed"}), "except BaseException keeps running"),
        Case("double_count_on_resume", DoubleCountOnResume, frozenset({"resume_diverged"}), "total re-added after restart"),
        Case("tool_wrapped_replay", ToolWrappedReplay, frozenset({"effect_replayed"}), "ctx.tool without a ledger check"),
        Case("function_loop_replay", from_function(_loop), frozenset({"effect_replayed"}), "from_function loop, no ledger check"),
        Case("fs_artifact_no_fsync", FsArtifactNoFsync, frozenset({"resume_diverged", "state_lost"}),
             "durable cursor points at an unsynced artifact"),
        Case("fs_rename_no_fsync", FsRenameNoFsync, frozenset({"resume_diverged", "state_lost", "subject_error"}),
             "rename of an unsynced temp file"),
        Case("fs_torn_cursor", FsTornCursor, frozenset({"subject_error"}), "in-place JSON rewrite torn by a power cut"),
        Case("fs_torn_journal", FsTornJournal, frozenset({"resume_diverged"}), "torn journal tail accepted as data"),
        Case("fs_stale_lock", FsStaleLock, frozenset({"subject_error"}), "lock file left behind by a crash"),
    ]


# -- directory-durability cases (strict_dirs): a separate, smaller corpus so the 24-agent
#    numbers above stay comparable across versions ---------------------------------------
class _Publisher:
    """Writes a result file, then records 'published'; recovery trusts the record."""

    def __init__(self, ctx):
        self.ctx = ctx

    def _write(self, item):
        raise NotImplementedError

    def step(self, item):
        if f"pub:{item}" in self.ctx.effects.keys():
            return
        self._write(item)
        self.ctx.effect("published", item, key=f"pub:{item}")

    def state(self):
        return self.ctx.fs.listdir("out")


class DirsAtomicWrite(_Publisher):
    def _write(self, item):
        self.ctx.fs.write_atomic(f"out/{item}", "report")


class DirsRenameThenFsyncDir(_Publisher):
    def _write(self, item):
        fs = self.ctx.fs
        fs.write(f"out/{item}.tmp", "report")
        fs.fsync(f"out/{item}.tmp")
        fs.rename(f"out/{item}.tmp", f"out/{item}")
        fs.fsync_dir("out")


class DirsRenameNoDirFsync(_Publisher):
    """temp + fsync + rename, but the directory is never fsynced: the new name can vanish."""

    def _write(self, item):
        fs = self.ctx.fs
        fs.write(f"out/{item}.tmp", "report")
        fs.fsync(f"out/{item}.tmp")
        fs.rename(f"out/{item}.tmp", f"out/{item}")


class DirsCreateNoDirFsync(_Publisher):
    """New file written and fsynced in place, but its directory entry was never made durable."""

    def _write(self, item):
        fs = self.ctx.fs
        fs.write(f"out/{item}", "report")
        fs.fsync(f"out/{item}")


def corpus_dirs() -> List[Case]:
    ok: FrozenSet[str] = frozenset()
    bad = frozenset({"resume_diverged", "state_lost"})
    return [
        Case("dirs_atomic_write", DirsAtomicWrite, ok, "write_atomic includes the directory fsync under strict_dirs"),
        Case("dirs_rename_then_fsync_dir", DirsRenameThenFsyncDir, ok, "explicit fsync_dir after rename"),
        Case("dirs_rename_no_dir_fsync", DirsRenameNoDirFsync, bad, "fsynced temp file renamed, directory never synced"),
        Case("dirs_create_no_dir_fsync", DirsCreateNoDirFsync, bad, "fsynced new file whose name is not durable"),
    ]


# -- failures that are not crashes (faults=True) and stale workers (stale_worker=True): a third small
#    corpus, so the 24-agent and directory numbers stay comparable across versions ----------------
class _Charger:
    """Base: charge each input once; subclasses differ in how they react to errors and to a replacement worker."""

    def __init__(self, ctx):
        self.ctx = ctx
        _done(ctx)

    def state(self):
        return sorted(self.ctx.store["done"])

    def step(self, item):
        if item in self.ctx.store["done"]:
            return
        self.charge(item)
        self.ctx.store["done"].append(item)

    def charge(self, item):
        raise NotImplementedError


class FaultsRetryChecksKey(_Charger):
    """Retries, but first asks whether the earlier try already went through."""

    def charge(self, item):
        for _ in range(3):
            if f"pay:{item}" in self.ctx.effects.keys():
                return
            try:
                self.ctx.effect("pay", item, key=f"pay:{item}")
                return
            except OSError:
                continue


class FaultsLetItCrash(_Charger):
    """No retry at all: an error ends the run, and the restart reconciles."""

    def charge(self, item):
        if f"pay:{item}" not in self.ctx.effects.keys():
            self.ctx.effect("pay", item, key=f"pay:{item}")


class _Journal:
    """Append-only journal of finished items. Restart repairs a torn tail; ``retry_repairs`` says whether the
    live retry path (after a failed append, with the process still running) repairs it too."""

    retry_repairs = False

    def __init__(self, ctx):
        self.ctx = ctx
        self._repair()

    def _lines(self):
        fs = self.ctx.fs
        return fs.read_text("journal").split("\n") if fs.exists("journal") else []

    def _items(self):
        out = []
        for line in self._lines():
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out

    def _repair(self):
        complete = [ln for ln in self._lines()[:-1] if ln]   # the last piece has no newline after it: torn or empty
        text = "".join(ln + "\n" for ln in complete)
        fs = self.ctx.fs
        if fs.exists("journal") and fs.read_text("journal") != text:
            fs.write_atomic("journal", text)

    def step(self, item):
        if item in self._items():
            return
        for _ in range(3):
            try:
                self.ctx.fs.write("journal", json.dumps(item) + "\n", append=True)
                self.ctx.fs.fsync("journal")
                return
            except OSError:
                if self.retry_repairs:
                    self._repair()

    def state(self):
        return sorted(self._items())


class FaultsJournalRepairsOnRetry(_Journal):
    retry_repairs = True


class FaultsJournalRetryWelds(_Journal):
    """Repairs a torn tail on restart but not before a live retry: the retried entry is welded to the half line."""


class StaleFencedWorker(_Charger):
    """Reconciles, and passes a fencing token so a stale worker is rejected."""

    def charge(self, item):
        if f"pay:{item}" not in self.ctx.effects.keys():
            self.ctx.effect("pay", item, key=f"pay:{item}", fence=self.ctx.generation)


class FaultsRetryBlind(_Charger):
    """Retries on any error without asking whether the first try committed (timeout -> double charge)."""

    def charge(self, item):
        if f"pay:{item}" in self.ctx.effects.keys():   # reconciles on restart, but not inside the retry loop
            return
        for _ in range(3):
            try:
                self.ctx.effect("pay", item, key=f"pay:{item}")
                return
            except OSError:
                continue


class FaultsRetryNewKey(_Charger):
    """Retries under a fresh key (a request id), so the provider cannot recognise the repeat."""

    def charge(self, item):
        if any(k.startswith(f"pay:{item}:") for k in self.ctx.effects.keys()):
            return
        for attempt in range(3):
            try:
                self.ctx.effect("pay", item, key=f"pay:{item}:{attempt}")
                return
            except OSError:
                continue


class FaultsSwallowAndCheckpoint(_Charger):
    """Logs and ignores the error, then marks the item done (refusal -> the work is silently lost)."""

    def charge(self, item):
        try:
            if f"pay:{item}" not in self.ctx.effects.keys():
                self.ctx.effect("pay", item, key=f"pay:{item}")
        except OSError:
            pass


class StaleUnfencedWorker(_Charger):
    """Reconciles against the ledger but has no fencing token: a worker that wakes late writes again."""

    def charge(self, item):
        if f"pay:{item}" not in self.ctx.effects.keys():
            self.ctx.effect("pay", item, key=f"pay:{item}")


class StaleEpochCheckOnly(_Charger):
    """Checks a lease epoch in the store before the call; a stall between check and call defeats it."""

    def __init__(self, ctx):
        super().__init__(ctx)
        ctx.store["epoch"] = ctx.store.get("epoch", 0) + 1
        self.epoch = ctx.store["epoch"]

    def state(self):
        return sorted(self.ctx.store["done"])

    def charge(self, item):
        if self.ctx.store["epoch"] != self.epoch:
            raise RuntimeError("lease lost")
        if f"pay:{item}" not in self.ctx.effects.keys():
            self.ctx.effect("pay", item, key=f"pay:{item}")


class StaleProviderDedup(_Charger):
    """No fencing token, but the resource honours the idempotency key (dedupe=True): a late duplicate is ignored."""

    def charge(self, item):
        self.ctx.effect("pay", item, key=f"pay:{item}", dedupe=True)

    def step(self, item):   # the checkpoint must also tolerate a second writer (a set, not an append)
        self.charge(item)
        if item not in self.ctx.store["done"]:
            self.ctx.store["done"].append(item)


class StaleFencedTool(_Charger):
    """The same, through ctx.tool(..., fence=...)."""

    def __init__(self, ctx):
        super().__init__(ctx)
        self.pay = ctx.tool(lambda item: None, kind="pay", key=lambda item: f"pay:{item}", fence=ctx.generation)

    def charge(self, item):
        if f"pay:{item}" not in self.ctx.effects.keys():
            self.pay(item)


def corpus_faults() -> List[Case]:
    ok: FrozenSet[str] = frozenset()
    return [
        Case("faults_retry_checks_key", FaultsRetryChecksKey, ok, "retry asks the ledger first"),
        Case("faults_let_it_crash", FaultsLetItCrash, ok, "no retry; restart reconciles"),
        Case("faults_retry_blind", FaultsRetryBlind, frozenset({"effect_replayed"}),
             "retry after a timeout repeats a committed charge"),
        Case("faults_retry_new_key", FaultsRetryNewKey, frozenset({"effect_replayed"}),
             "retry under a fresh key defeats idempotency"),
        Case("faults_swallow_and_checkpoint", FaultsSwallowAndCheckpoint, frozenset({"effect_lost"}),
             "ignored refusal, item marked done"),
        Case("faults_journal_repairs_on_retry", FaultsJournalRepairsOnRetry, ok,
             "disk full mid-append, retry repairs the tail first"),
        Case("faults_journal_retry_welds", FaultsJournalRetryWelds, frozenset({"resume_diverged"}),
             "disk full mid-append, retried entry welded to the half line"),
    ]


def corpus_stale() -> List[Case]:
    ok: FrozenSet[str] = frozenset()
    return [
        Case("stale_fenced_worker", StaleFencedWorker, ok, "fencing token rejects the stale worker"),
        Case("stale_fenced_tool", StaleFencedTool, ok, "same, through ctx.tool"),
        Case("stale_provider_dedup", StaleProviderDedup, ok, "the resource ignores a repeated idempotency key"),
        Case("stale_unfenced_worker", StaleUnfencedWorker, frozenset({"stale_worker"}), "no fencing token"),
        Case("stale_epoch_check_only", StaleEpochCheckOnly, frozenset({"stale_worker"}),
             "check-then-act race with a stalled worker"),
    ]
