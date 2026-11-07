"""Round 3 regression tests: input isolation, NaN-safe compare, O(1) key lookups,
directory-durability model, torn process ledger, explain(), sampling, vacuous runs."""
from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from crashresume import KeyView, SimFS, conform, explain
from crashresume.core import EffectLog, _same_payload
from crashresume.fs import RealFS, _norm


# -- inputs are private to each process life ---------------------------------------------
def test_step_that_mutates_its_input_does_not_poison_other_scenarios():
    class Mut:
        def __init__(self, ctx):
            self.ctx = ctx

        def step(self, job):
            n = job.pop("n")                      # consumes the dict it was handed
            if f"k{n}" not in self.ctx.effects.keys():
                self.ctx.effect("x", n, key=f"k{n}")

    inputs = [{"n": 1}, {"n": 2}]
    report = conform(Mut, inputs)
    assert report.passed, str(report)
    assert inputs == [{"n": 1}, {"n": 2}]         # the caller's objects are untouched


def test_uncopyable_inputs_still_run():
    import threading

    lock = threading.Lock()

    class A:
        def __init__(self, ctx):
            self.ctx = ctx

        def step(self, item):
            self.ctx.effect("x", 1, key="k")

    assert conform(A, [lock]).crash_points >= 1


# -- NaN ----------------------------------------------------------------------------------
def test_nan_payload_and_state_are_not_false_alarms():
    class Nan:
        def __init__(self, ctx):
            self.ctx = ctx

        def step(self, j):
            if f"k{j}" not in self.ctx.effects.keys():
                self.ctx.effect("x", {"v": float("nan")}, key=f"k{j}")

        def state(self):
            return {"v": float("nan")}

    report = conform(Nan, [1, 2], check_payloads=True, crash_before=True)
    assert report.passed, str(report)
    assert _same_payload(float("nan"), float("nan"))
    assert not _same_payload(1.0, 2.0)
    assert not _same_payload([1, float("nan")], [2, float("nan")])


# -- keys() is a cheap list-like view ---------------------------------------------------------
def test_keyview_behaves_like_the_old_list():
    log = EffectLog()
    log.record("a", 1, "k1")
    log.record("b", 2)                            # keyless
    snap = log.keys()
    log.record("a", 3, "k2")
    assert isinstance(snap, KeyView)
    assert list(snap) == ["k1"] and len(snap) == 1 and snap == ["k1"] and snap[0] == "k1" and snap[-1] == "k1"
    assert "k2" not in snap and "k2" in log.keys() and [] not in snap
    assert log.keys()[:] == ["k1", "k2"] and log.has("k2") and not log.has([1]) and log.count("k1") == 1
    with pytest.raises(IndexError):
        snap[1]
    assert sorted(log.keys()) == ["k1", "k2"] and repr(log.keys()) == "['k1', 'k2']"


def test_reconcile_idiom_scales():
    class A:
        def __init__(self, ctx):
            self.ctx = ctx

        def step(self, j):
            if f"k{j}" not in self.ctx.effects.keys():
                self.ctx.effect("x", j, key=f"k{j}")

    import time

    t = time.time()
    report = conform(A, list(range(400)))
    assert report.passed and report.crash_points == 400
    assert time.time() - t < 20      # was ~10 s of pure list copying before the index


# -- directory durability ------------------------------------------------------------------
class Publish:
    """Writes a result file atomically, then records 'published'. Recovery trusts the record."""

    FSYNC_DIR = False

    def __init__(self, ctx):
        self.ctx = ctx

    def step(self, item):
        if f"pub:{item}" in self.ctx.effects.keys():
            return
        fs = self.ctx.fs
        fs.write(f"out/{item}.tmp", "data")
        fs.fsync(f"out/{item}.tmp")
        fs.rename(f"out/{item}.tmp", f"out/{item}")
        if self.FSYNC_DIR:
            fs.fsync_dir("out")
        self.ctx.effect("published", item, key=f"pub:{item}")

    def state(self):
        return self.ctx.fs.listdir("out")


class PublishSafe(Publish):
    FSYNC_DIR = True


def test_rename_without_dir_fsync_is_only_caught_in_strict_dirs():
    assert conform(Publish, ["a"]).passed                  # optimistic model: names are durable
    report = conform(Publish, ["a"], strict_dirs=True)
    assert not report.passed
    assert {f.kind for f in report.findings} <= {"state_lost", "resume_diverged"}
    assert conform(PublishSafe, ["a"], strict_dirs=True).passed
    assert conform(PublishSafe, ["a"], strict_dirs=True, crash_before=True, torn_writes=True, max_crashes=2).passed


def test_write_atomic_is_safe_under_strict_dirs():
    class A:
        def __init__(self, ctx):
            self.ctx = ctx

        def step(self, item):
            if f"pub:{item}" in self.ctx.effects.keys():
                return
            self.ctx.fs.write_atomic(f"out/{item}", "data")
            self.ctx.effect("published", item, key=f"pub:{item}")

        def state(self):
            return self.ctx.fs.listdir("out")

    assert conform(A, ["a", "b"], strict_dirs=True, crash_before=True, max_crashes=2).passed


def test_simfs_strict_names_semantics():
    fs = SimFS(dir_durability=True)
    fs.write("d/a", "1")
    fs.fsync("d/a")
    fs.power_loss()
    assert not fs.exists("d/a")                       # created + fsynced, directory never synced
    fs.write("d/a", "1")
    fs.fsync("d/a")
    fs.fsync_dir("d")
    fs.rename("d/a", "d/b")                           # rename not yet durable
    fs.power_loss()
    assert fs.exists("d/a") and not fs.exists("d/b") and fs.read("d/a") == b"1"
    fs.rename("d/a", "e/a")                           # cross-directory move needs both syncs
    fs.fsync_dir("e")
    fs.power_loss()
    assert fs.exists("d/a") and fs.exists("e/a")      # either or both may survive; the model keeps both
    fs.rename("d/a", "e/a")
    fs.fsync_dir("e")
    fs.fsync_dir("d")
    fs.power_loss()
    assert fs.exists("e/a") and not fs.exists("d/a")


def test_fsync_dir_is_a_crash_point_in_every_mode():
    class A:
        def __init__(self, ctx):
            self.ctx = ctx

        def step(self, item):
            self.ctx.fs.write("a", "x")
            self.ctx.fs.fsync("a")
            self.ctx.fs.fsync_dir("")
            self.ctx.effect("e", item, key="k")

    assert conform(A, [1]).crash_points == 4


def test_realfs_fsync_dir_and_paths(tmp_path):
    fs = RealFS(str(tmp_path))
    fs.write("d/a", "1")
    fs.fsync("d/a")
    fs.fsync_dir("d")
    fs.fsync_dir("")
    with pytest.raises(ValueError):
        fs.write("a\x00b", "x")
    with pytest.raises(ValueError):
        _norm("a\x00b")
    with pytest.raises(ValueError):
        _norm("CON.txt", portable=True)
    with pytest.raises(ValueError):
        _norm("a:stream", portable=True)
    with pytest.raises(ValueError):
        _norm("name.", portable=True)
    assert _norm("lock:1") == "lock:1"                # fine on POSIX


# -- process ledger ------------------------------------------------------------------------
def test_torn_ledger_tail_is_dropped_and_repaired(tmp_path):
    from crashresume._child import load_ledger

    p = tmp_path / "effects.jsonl"
    good = json.dumps({"kind": "k", "payload": 1, "key": "a"}) + "\n"
    p.write_bytes((good + '{"kind": "k", "paylo').encode())
    assert [d["key"] for d in load_ledger(str(p))] == ["a"]
    assert len(load_ledger(str(p), repair=True)) == 1
    assert p.read_text() == good                      # next append starts on a clean line
    p.write_bytes(("garbage\n" + good).encode())
    with pytest.raises(ValueError, match="corrupt"):
        load_ledger(str(p))


# -- explain --------------------------------------------------------------------------------
def test_explain_shows_lives_kill_point_and_finding():
    class Replay:
        def __init__(self, ctx):
            self.ctx = ctx

        def step(self, item):
            self.ctx.effect("charge\x1b[31m", item, key=f"c:{item}")

    report = conform(Replay, ["a", "b"])
    text = explain(Replay, ["a", "b"], report.findings[0].crashes)
    assert "process life 1: killed after point #1" in text
    assert "KILLED right after this committed" in text
    assert "resume (clean process, no crash)" in text
    assert "[effect_replayed]" in text and "\x1b" not in text
    with pytest.raises(ValueError):
        explain(Replay, ["a"], [])


def test_explain_lists_file_operations():
    text = explain(Publish, ["a"], [(3, "before")], strict_dirs=True)
    assert "write out/a.tmp" in text and "rename out/a.tmp -> out/a" in text and "BEFORE this committed" in text


# -- sampling -----------------------------------------------------------------------------------
def test_sample_limits_scenarios_and_warns():
    class A:
        def __init__(self, ctx):
            self.ctx = ctx

        def step(self, j):
            if f"k{j}" not in self.ctx.effects.keys():
                self.ctx.effect("x", j, key=f"k{j}")

    full = conform(A, list(range(40)))
    cut = conform(A, list(range(40)), sample=5)
    assert full.crash_points == 40 and cut.crash_points == 5
    assert any("sample=5" in w and "35" in w for w in cut.warnings)
    assert cut.scenarios[0] == "run-1" and cut.scenarios[-1] == "run-40"
    assert conform(A, list(range(3)), sample=5).warnings == []
    for bad in (0, -1, 1.5, True):
        with pytest.raises(ValueError):
            conform(A, [1], sample=bad)


def test_sample_finds_a_bug_that_hits_every_point():
    class Replay:
        def __init__(self, ctx):
            self.ctx = ctx

        def step(self, j):
            self.ctx.effect("x", j, key=f"k{j}")

    assert not conform(Replay, list(range(30)), sample=3).passed


# -- determinism / factory / vacuous ------------------------------------------------------------
def test_nondeterministic_subject_warns_when_asked():
    import itertools

    counter = itertools.count()

    class Flaky:
        def __init__(self, ctx):
            self.ctx = ctx

        def step(self, j):
            if f"k{j}" not in self.ctx.effects.keys():
                self.ctx.effect("x", j, key=f"k{j}-{next(counter)}")

    assert any("nondeterministic" in w for w in conform(Flaky, [1], check_determinism=True).warnings)
    assert not any("nondeterministic" in w for w in conform(Flaky, [1]).warnings)


def test_deterministic_subject_has_no_determinism_warning():
    class A:
        def __init__(self, ctx):
            self.ctx = ctx

        def step(self, j):
            self.ctx.effect("x", j, key=f"{self.ctx.run_id}:{j}")

    assert conform(A, [1], check_determinism=True).warnings == []


def test_factory_without_step_is_a_clear_type_error():
    with pytest.raises(TypeError, match="step"):
        conform(lambda ctx: object(), [1])


def test_vacuous_run_is_flagged():
    class Quiet:
        def __init__(self, ctx):
            pass

        def step(self, j):
            pass

    report = conform(Quiet, [1])
    assert report.passed and report.vacuous


def test_pytest_fixture_rejects_vacuous(pytester):
    pytester.makeconftest("from crashresume.pytest_plugin import conform  # noqa: F401")
    pytester.makepyfile(
        """
        class Quiet:
            def __init__(self, ctx): pass
            def step(self, j): pass

        def test_vacuous(conform):
            conform(Quiet, [1])

        def test_allowed(conform):
            conform(Quiet, [1], allow_vacuous=True)
        """
    )
    result = pytester.runpytest()
    result.assert_outcomes(passed=1, failed=1)
    result.stdout.fnmatch_lines(["*nothing was tested*"])


# -- CLI ----------------------------------------------------------------------------------------
def _cli(tmp_path, source, *args):
    (tmp_path / "agent_mod.py").write_text(source)
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(p for p in sys.path if p)
    return subprocess.run([sys.executable, "-m", "crashresume", "agent_mod:Agent", *args], cwd=tmp_path, env=env,
                          capture_output=True, text=True, timeout=120)


QUIET = "class Agent:\n    def __init__(self, ctx): pass\n    def step(self, j): pass\n"
REPLAY = ("class Agent:\n    def __init__(self, ctx): self.ctx = ctx\n"
          "    def step(self, j): self.ctx.effect('x', j, key=f'k{j}')\n")


def test_cli_fails_closed_on_vacuous_run(tmp_path):
    r = _cli(tmp_path, QUIET, "--inputs", "[1]")
    assert r.returncode == 2 and "nothing was tested" in r.stderr
    assert _cli(tmp_path, QUIET, "--inputs", "[1]", "--allow-vacuous").returncode == 0


def test_cli_explain_and_sample_and_strict_dirs(tmp_path):
    r = _cli(tmp_path, REPLAY, "--inputs", "[1,2,3]", "--explain")
    assert r.returncode == 1 and "KILLED right after this committed" in r.stdout
    r = _cli(tmp_path, REPLAY, "--inputs", "[1,2,3,4,5,6]", "--sample", "2")
    assert "sample=2" in r.stdout
    assert _cli(tmp_path, REPLAY, "--inputs", "[1]", "--strict-dirs").returncode == 1


def test_cli_warns_about_flags_process_mode_ignores(tmp_path):
    r = _cli(tmp_path, REPLAY, "--inputs", "[1]", "--process", "--minimize", "--torn-writes")
    assert "--minimize, --torn-writes not supported with --process" in r.stderr
