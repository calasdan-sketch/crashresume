"""Round 5: failure injection beyond crashes (timeouts, refusals, stale workers), fencing, author pin,
file-system differential test, seeded property tests, golden files for every output format."""
import json
import os
import random
import re
import subprocess
import sys
import textwrap
import time

import pytest

import crashresume
from crashresume import FencedOut, InjectedRefusal, InjectedTimeout, conform, conform_process, explain, replay
from crashresume.cli import main
from crashresume.core import Context, EffectLog, clean
from crashresume.fs import RealFS, SimFS

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AUTHOR = "Calas Automations Inc."


# -- author line and version are pinned -------------------------------------------------------------
def test_author_string_is_pinned_everywhere(capsys):
    assert crashresume.__author__ == AUTHOR
    pyproject = open(os.path.join(ROOT, "pyproject.toml"), encoding="utf-8").read()
    assert re.search(r'^authors = \[\{ name = "Calas Automations Inc\." \}\]$', pyproject, re.M)
    assert re.search(r'^maintainers = \[\{ name = "Calas Automations Inc\." \}\]$', pyproject, re.M)
    assert "calasdan" not in pyproject.split("[project.urls]")[0]
    assert open(os.path.join(ROOT, "LICENSE"), encoding="utf-8").read().splitlines()[2] == f"Copyright (c) 2026 {AUTHOR}"
    with pytest.raises(SystemExit):
        main(["--version"])
    assert AUTHOR in capsys.readouterr().out


def test_report_formats_name_the_tool_author():
    class A:
        def __init__(self, ctx): self.ctx = ctx
        def step(self, i): self.ctx.effect("e", i, key=i)
    rep = conform(A, ["a"])
    sarif = json.loads(rep.to_sarif())
    driver = sarif["runs"][0]["tool"]["driver"]
    assert driver["organization"] == AUTHOR and driver["version"] == crashresume.__version__
    assert f"by {AUTHOR}" in rep.to_junit()


def test_version_is_the_same_everywhere():
    pyproject = open(os.path.join(ROOT, "pyproject.toml"), encoding="utf-8").read()
    assert re.search(rf'^version = "{re.escape(crashresume.__version__)}"$', pyproject, re.M)
    changelog = open(os.path.join(ROOT, "CHANGELOG.md"), encoding="utf-8").read()
    assert re.search(rf"^## {re.escape(crashresume.__version__)}\b", changelog, re.M)


# -- faults: timeout after commit, refusal before it ----------------------------------------------------
class RetryBlind:
    """Retries on error without asking whether the first try went through."""
    def __init__(self, ctx):
        self.ctx = ctx
        ctx.store.setdefault("done", [])

    def step(self, j):
        if j in self.ctx.store["done"]:
            return
        for _ in range(3):
            try:
                self.ctx.effect("charge", j, key=f"c:{j}")
                break
            except OSError:
                continue
        self.ctx.store["done"].append(j)
    def state(self): return sorted(self.ctx.store["done"])


class RetryChecks(RetryBlind):
    def step(self, j):
        if j in self.ctx.store["done"]:
            return
        for _ in range(3):
            if f"c:{j}" in self.ctx.effects.keys():
                break
            try:
                self.ctx.effect("charge", j, key=f"c:{j}")
                break
            except OSError:
                continue
        self.ctx.store["done"].append(j)


class Swallows(RetryBlind):
    def step(self, j):
        if j in self.ctx.store["done"]:
            return
        try:
            self.ctx.effect("charge", j, key=f"c:{j}")
        except Exception:
            pass
        self.ctx.store["done"].append(j)


class LetItCrash(RetryBlind):
    def step(self, j):
        if j in self.ctx.store["done"]:
            return
        if f"c:{j}" not in self.ctx.effects.keys():
            self.ctx.effect("charge", j, key=f"c:{j}")
        self.ctx.store["done"].append(j)


def test_blind_retry_double_charges_after_a_timeout():
    rep = conform(RetryBlind, ["a", "b"], faults=True)
    timeouts = [f for f in rep.findings if f.when == "timeout"]
    assert {f.kind for f in timeouts} == {"effect_replayed"} and len(timeouts) == 2
    assert not [f for f in rep.findings if f.when == "refused"]   # a blind retry IS right when nothing ran
    assert "timeout after effect #1" in str(rep)


def test_checked_retry_and_let_it_crash_pass_all_faults():
    for cls in (RetryChecks, LetItCrash):
        rep = conform(cls, ["a", "b", "c"], faults=True)
        assert rep.passed, str(rep)
        assert {"run-o1", "run-r1"} <= set(rep.scenarios)


def test_swallowing_a_refusal_loses_the_effect():
    rep = conform(Swallows, ["a", "b"], faults=True)
    assert any(f.when == "refused" and f.kind == "effect_lost" for f in rep.findings), str(rep)


def test_faults_are_off_by_default_and_not_part_of_thorough():
    base = conform(RetryBlind, ["a", "b"], thorough=True)
    assert not any(f.when in ("timeout", "refused") for f in base.findings)
    assert not any(re.search(r"-[or]\d", s) for s in base.scenarios)


def test_fault_is_an_oserror_and_fires_exactly_once():
    seen = []

    class A:
        def __init__(self, ctx): self.ctx = ctx
        def step(self, j):
            try:
                self.ctx.effect("e", j, key=j)
            except OSError as exc:
                seen.append(type(exc))
    conform(A, ["a", "b", "c"], faults=True)
    assert set(seen) == {InjectedTimeout, InjectedRefusal}
    assert issubclass(InjectedTimeout, TimeoutError) and issubclass(InjectedRefusal, ConnectionError)
    assert len(seen) == 6   # 3 inputs x (timeout, refusal), each scenario faulted once


def test_unhandled_fault_is_a_process_exit_not_a_power_cut():
    """An error that escapes step() ends the process normally: unfsynced file data is still there
    afterwards (only a power cut loses it). A subject that trusts such a file is wrong only under faults."""
    class A:
        def __init__(self, ctx): self.ctx = ctx
        def step(self, j):
            fs = self.ctx.fs
            if fs.exists("m") and fs.read("m") == b"x":
                return                                  # "already done", judged from an unfsynced marker
            fs.write("m", "x")
            self.ctx.effect("e", j, key=j)
    rep = conform(A, ["a"], faults=True)
    assert any(f.when == "refused" and f.kind == "effect_lost" for f in rep.findings), str(rep)
    # a power cut would have erased the marker, so the same trust is fine after a crash
    assert not any(f.kind == "effect_lost" and f.when == "after" for f in rep.findings)


def test_faults_work_through_ctx_tool():
    calls = []

    class A:
        def __init__(self, ctx):
            self.ctx = ctx
            self.t = ctx.tool(lambda x: calls.append(x), kind="send", key=lambda x: f"s:{x}")

        def step(self, j):
            try:
                self.t(j)
            except OSError:
                self.t(j)       # blind retry
    rep = conform(A, ["a"], faults=True)
    timeouts = [f for f in rep.findings if f.when == "timeout"]
    assert [f.kind for f in timeouts] == ["effect_replayed"]
    assert not [f for f in rep.findings if f.when == "refused"]


def test_replay_and_explain_accept_fault_scenarios():
    rep = conform(RetryBlind, ["a", "b"], faults=True)
    f = next(x for x in rep.findings if x.when == "timeout")
    again = replay(RetryBlind, ["a", "b"], f.crashes)
    assert [x.kind for x in again.findings] == [f.kind]
    text = explain(RetryBlind, ["a", "b"], f.crashes)
    assert "FAULT: executes, but the caller will see a timeout" in text and "effect_replayed" in text
    with pytest.raises(ValueError):
        replay(RetryBlind, ["a"], [(1, "meltdown")])


def test_sample_and_scenario_ids_cover_faults():
    rep = conform(RetryChecks, [str(i) for i in range(10)], faults=True, sample=3)
    assert any("skipped" in w or "NOT tested" in w for w in rep.warnings)
    assert sum(1 for s in rep.scenarios if s.split("-")[-1].startswith("o")) == 3


def test_faults_need_something_to_fail():
    class OnlyDirs:
        def __init__(self, ctx): self.ctx = ctx
        def step(self, j): self.ctx.fs.fsync_dir("")
    rep = conform(OnlyDirs, ["a"], faults=True)
    assert any("nothing was injected" in w for w in rep.warnings)

    class OnlyFiles:
        def __init__(self, ctx): self.ctx = ctx
        def step(self, j): self.ctx.fs.write("a", "x")
    rep2 = conform(OnlyFiles, ["a"], faults=True)
    assert not any("nothing was injected" in w for w in rep2.warnings) and "run-d1" in rep2.scenarios


# -- fencing tokens and the stale worker ----------------------------------------------------------------
class Unfenced:
    def __init__(self, ctx): self.ctx = ctx
    def step(self, j):
        if f"c:{j}" in self.ctx.effects.keys():
            return
        self.ctx.effect("charge", j, key=f"c:{j}")


class Fenced(Unfenced):
    def step(self, j):
        if f"c:{j}" in self.ctx.effects.keys():
            return
        self.ctx.effect("charge", j, key=f"c:{j}", fence=self.ctx.generation)


class EpochCheckOnly:
    """Checks a lease epoch in the store before the call: a pause between check and call defeats it."""
    def __init__(self, ctx):
        self.ctx = ctx
        ctx.store["epoch"] = ctx.store.get("epoch", 0) + 1
        self.epoch = ctx.store["epoch"]

    def step(self, j):
        if self.ctx.store["epoch"] != self.epoch:
            raise RuntimeError("lease lost")
        if f"c:{j}" not in self.ctx.effects.keys():
            self.ctx.effect("charge", j, key=f"c:{j}")

    def state(self): return None


def test_unfenced_worker_is_caught_and_fenced_worker_passes():
    bad = conform(Unfenced, ["a", "b", "c"], stale_worker=True)
    assert {f.kind for f in bad.findings} == {"stale_worker"} and len(bad.findings) == 3
    assert "stale worker paused at effect #1" in str(bad)
    assert bad.findings[0].when == "stale" and bad.findings[0].scenario == "run-z1"
    good = conform(Fenced, ["a", "b", "c"], stale_worker=True)
    assert good.passed and good.crash_points == 6, str(good)


def test_check_before_the_call_is_not_fencing():
    rep = conform(EpochCheckOnly, ["a", "b"], stale_worker=True)
    assert {f.kind for f in rep.findings} == {"stale_worker"}, str(rep)


def test_stale_worker_leaves_normal_modes_untouched():
    plain = conform(Unfenced, ["a", "b"])
    both = conform(Unfenced, ["a", "b"], stale_worker=True)
    assert plain.passed and both.crash_points == plain.crash_points + 2


def test_fenced_out_is_a_plain_exception_and_rejected_effect_is_not_recorded():
    log = EffectLog()
    c1, c2 = Context("r", log, generation=1), Context("r", log, generation=2)
    c2.effect("e", 1, key="k", fence=c2.generation)
    with pytest.raises(FencedOut):
        c1.effect("e", 2, key="k2", fence=c1.generation)
    assert [e.key for e in log.effects] == ["k"]
    c2.effect("e", 3, key="k3")            # no fence given: not checked
    assert log.fence_high == 2


def test_stale_scenario_explain_shows_both_workers():
    rep = conform(Unfenced, ["a", "b"], stale_worker=True)
    text = explain(Unfenced, ["a", "b"], rep.findings[0].crashes)
    assert "worker PAUSES here" in text and "WAKES UP" in text and "stale worker effect" in text
    fenced = explain(Fenced, ["a", "b"], rep.findings[0].crashes)
    assert "stale worker stopped" in fenced and "no finding" in fenced


def test_stale_worker_that_never_returns_is_reported_not_hung(monkeypatch):
    import crashresume.core as core
    monkeypatch.setattr(core, "_STALE_TIMEOUT", 0.5)

    class Hangs:
        def __init__(self, ctx): self.ctx = ctx
        def step(self, j):
            self.ctx.effect("e", j, key=j)
            if self.ctx.generation == 1:
                time.sleep(2)          # the stale worker is slow to finish after waking up
    t0 = time.time()
    rep = conform(Hangs, ["a"], stale_worker=True)
    assert time.time() - t0 < 10
    assert isinstance(rep.passed, bool)


def test_stale_worker_cli_flag(tmp_path, capsys, monkeypatch):
    (tmp_path / "ag.py").write_text(textwrap.dedent('''
        class U:
            def __init__(self, ctx): self.ctx = ctx
            def step(self, j):
                if j not in self.ctx.effects.keys():
                    self.ctx.effect("e", j, key=j)
        def make(ctx): return U(ctx)
    '''), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert main(["ag.py:make", "--inputs", '["a","b"]']) == 0
    assert main(["ag.py:make", "--inputs", '["a","b"]', "--stale-worker", "--faults"]) == 1
    out = capsys.readouterr().out
    assert "[stale_worker]" in out
    capsys.readouterr()
    assert main(["ag.py:make", "--inputs", '["a"]', "--process", "--faults", "--stale-worker"]) == 0
    assert "--faults, --stale-worker not supported with --process" in capsys.readouterr().err


# -- SimFS and RealFS must agree on every operation without a crash (differential test) ----------------
NAMES = ["a", "b", "d/a", "d/b", "d/e/f", "a.tmp", "x y/z", "d/q"]


def _ops(rng):
    ops = []
    for _ in range(14):
        op = rng.choice(["write", "write", "fsync", "rename", "remove", "read", "exists", "listdir", "fsync_dir", "write_atomic"])
        n = rng.choice(NAMES)
        if op == "write":
            ops.append((op, (n, rng.choice(["x", "yy", b"z", "ü"])), {"append": rng.random() < 0.3}))
        elif op == "rename":
            ops.append((op, (n, rng.choice(NAMES)), {}))
        elif op == "listdir":
            ops.append((op, (rng.choice(["", "d", "a", "d/e"]),), {}))
        elif op == "fsync_dir":
            ops.append((op, (rng.choice(["", "d", "d/e", "zz"]),), {}))
        elif op == "write_atomic":
            ops.append((op, (n, "q"), {}))
        else:
            ops.append((op, (n,), {}))
    return ops


def _apply(fs, ops):
    out = []
    for op, args, kw in ops:
        try:
            r = getattr(fs, op)(*args, **kw)
            out.append((op, "ok", r if op in ("read", "exists", "listdir") else None))
        except Exception as exc:  # noqa: BLE001
            out.append((op, type(exc).__name__))
    names = fs.listdir()
    out.append(("final", names, [fs.read(p) for p in names]))
    return out


@pytest.mark.parametrize("seed", range(60))
def test_simfs_and_realfs_agree_without_crashes(seed, tmp_path):
    ops = _ops(random.Random(seed))
    assert _apply(SimFS(), ops) == _apply(RealFS(str(tmp_path / "real")), ops)


def test_realfs_rename_into_a_new_directory_and_fsync_of_a_missing_directory(tmp_path):
    fs = RealFS(str(tmp_path))
    fs.write("a", "x")
    fs.rename("a", "new/dir/b")
    assert fs.read("new/dir/b") == b"x"
    fs.fsync_dir("never/written")          # SimFS accepts any prefix; so does RealFS now


# -- seeded property tests (hypothesis is optional; these run everywhere) --------------------------------
def _agent(spec):
    """Build an agent class from a tiny random program: guard style x checkpoint position x retry style."""
    guard, cp, retry, n_fs = spec

    class Gen:
        def __init__(self, ctx):
            self.ctx = ctx
            ctx.store.setdefault("done", [])

        def _do(self, j):
            key = f"k:{j}"
            if guard == "ledger" and key in self.ctx.effects.keys():
                return
            self.ctx.effect("e", j, key=key)

        def step(self, j):
            if n_fs:
                self.ctx.fs.write_atomic(f"cur/{j}", "x")
            if j in self.ctx.store["done"]:
                return
            if cp == "before":
                self.ctx.store["done"].append(j)
            self._do(j)
            if cp == "after":
                self.ctx.store["done"].append(j)

        def state(self): return sorted(self.ctx.store["done"])
    return Gen


SPECS = [(g, c, r, f) for g in ("ledger", "none") for c in ("before", "after") for r in (0, 1) for f in (0, 1)]


@pytest.mark.parametrize("spec", SPECS)
def test_property_generated_agents_match_the_oracle(spec):
    guard, cp, _retry, _fs = spec
    rep = conform(_agent(spec), ["a", "b", "c"], crash_before=True, max_crashes=2)
    # ground truth by construction: lost work if checkpointed first; double effects if the ledger is not consulted
    # (a checkpoint after a ledger-less effect is replayed by the crash between effect and checkpoint)
    broken = cp == "before" or guard == "none"
    assert (not rep.passed) == broken, str(rep)
    # determinism: the same run twice gives the same text and bytes
    again = conform(_agent(spec), ["a", "b", "c"], crash_before=True, max_crashes=2)
    assert str(rep) == str(again) and rep.to_json(sort_keys=True) == again.to_json(sort_keys=True)
    # every finding replays to the same kind, and the JSON form round-trips
    for f in rep.findings[:6]:
        assert f.kind in {x.kind for x in replay(_agent(spec), ["a", "b", "c"], f.crashes, crash_before=True,
                                                 max_crashes=len(f.crashes)).findings}
    assert json.loads(rep.to_json()) == json.loads(json.dumps(rep.to_dict()))


@pytest.mark.parametrize("seed", range(8))
def test_property_sampled_findings_are_a_subset_of_the_full_run(seed):
    rng = random.Random(seed)
    spec = rng.choice(SPECS)
    inputs = [str(i) for i in range(rng.randint(3, 9))]
    full = conform(_agent(spec), inputs)
    part = conform(_agent(spec), inputs, sample=rng.randint(1, 3))
    key = lambda f: (f.kind, f.scenario)  # noqa: E731
    assert {key(f) for f in part.findings} <= {key(f) for f in full.findings}
    assert part.crash_points <= full.crash_points


@pytest.mark.parametrize("seed", range(5))
def test_property_minimized_repro_still_fails(seed):
    spec = ("none", "after", 0, 0)
    inputs = [str(i) for i in range(random.Random(seed).randint(3, 8))]
    rep = conform(_agent(spec), inputs, minimize=True)
    assert rep.repros
    for r in rep.repros:
        small = replay(_agent(spec), list(r.inputs), r.crashes)
        assert r.kind in {f.kind for f in small.findings}
        assert len(r.inputs) <= len(inputs)


def test_property_clean_output_is_printable_idempotent_and_keeps_ascii():
    rng = random.Random(1)
    pool = [chr(c) for c in list(range(0, 0x250)) + [0x200B, 0x202E, 0x2066, 0x1F600, 0xFEFF, 0x7F, 0x9B, 0xE000]]
    for _ in range(300):
        s = "".join(rng.choice(pool) for _ in range(rng.randint(0, 30)))
        c = clean(s)
        assert c.isprintable(), repr(s)
        assert clean(c) == c
        assert "\x1b" not in c and "\u202e" not in c
    assert clean("plain text 123 -_/") == "plain text 123 -_/"


def test_property_keyview_matches_a_plain_list():
    rng = random.Random(7)
    for _ in range(200):
        log = EffectLog()
        ref = []
        for _ in range(rng.randint(0, 12)):
            k = rng.choice(["a", "b", "c", 1, 2, (1, 2), None])
            log.record("e", None, k)
            if k is not None:
                ref.append(k)
            view = log.keys()
            assert list(view) == ref and len(view) == len(ref) and view == ref
            probe = rng.choice(["a", "b", "z", 1, 3, (1, 2), None, [1]])
            assert (probe in view) == (probe in ref if not isinstance(probe, list) else False)
            assert log.count(probe) == (ref.count(probe) if not isinstance(probe, list) else 0)
            if ref:
                i = rng.randrange(-len(ref), len(ref))
                assert view[i] == ref[i]
            assert view[1:3] == ref[1:3]
        snap = log.keys()
        n = len(snap)
        log.record("e", None, "later")
        assert len(snap) == n and "later" not in snap


# -- golden files: every output format, byte for byte ---------------------------------------------------
GOLDEN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "golden")


class GoldenAgent:
    def __init__(self, ctx):
        self.ctx = ctx
        ctx.store.setdefault("done", [])

    def step(self, j):
        if j in self.ctx.store["done"]:
            return
        self.ctx.effect("charge", {"id": j}, key=f"c:{j}")
        self.ctx.store["done"].append(j)
    def state(self): return sorted(self.ctx.store["done"])


def _golden(name, text):
    path = os.path.join(GOLDEN, name)
    if os.environ.get("CRASHRESUME_UPDATE_GOLDEN") == "1":
        os.makedirs(GOLDEN, exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
    with open(path, encoding="utf-8", newline="") as fh:
        want = fh.read()
    assert text == want, f"{name} changed; if intended run with CRASHRESUME_UPDATE_GOLDEN=1"


def _golden_report():
    return conform(GoldenAgent, ["a", "b"], crash_before=True, faults=True, minimize=True)


def test_golden_text():
    _golden("report.txt", str(_golden_report()) + "\n")


def test_golden_json():
    _golden("report.json", _golden_report().to_json(indent=2, sort_keys=True) + "\n")


def test_golden_junit():
    _golden("report.junit.xml", _golden_report().to_junit("golden"))


def test_golden_sarif():
    _golden("report.sarif", _golden_report().to_sarif("golden.py"))


def test_golden_explain():
    rep = _golden_report()
    _golden("explain.txt", explain(GoldenAgent, ["a", "b"], rep.findings[0].crashes) + "\n")


def test_golden_stale_explain():
    _golden("explain_stale.txt", explain(Unfenced, ["a"], [(1, "stale")]) + "\n")


def test_junit_and_sarif_are_well_formed():
    from xml.etree import ElementTree as ET
    rep = _golden_report()
    ET.fromstring(rep.to_junit())
    doc = json.loads(rep.to_sarif())
    assert doc["version"] == "2.1.0" and doc["runs"][0]["results"]


def test_wheel_metadata_names_the_author_when_a_wheel_exists():
    """Only runs when a built wheel is lying around (the release check builds one)."""
    dist = os.path.join(ROOT, "dist")
    wheels = [f for f in os.listdir(dist) if f.endswith(".whl")] if os.path.isdir(dist) else []
    if not wheels:
        pytest.skip("no dist/*.whl built")
    import zipfile
    with zipfile.ZipFile(os.path.join(dist, sorted(wheels)[-1])) as z:
        meta = z.read([n for n in z.namelist() if n.endswith("METADATA")][0]).decode("utf-8")
    assert f"Author: {AUTHOR}" in meta or f"Maintainer: {AUTHOR}" in meta


def test_module_runs_from_a_path_with_spaces(tmp_path):
    d = tmp_path / "my agent dir"
    d.mkdir()
    (d / "ag.py").write_text("class A:\n    def __init__(self, ctx): self.ctx = ctx\n"
                             "    def step(self, j): self.ctx.effect('e', j, key=j)\n"
                             "def make(ctx): return A(ctx)\n", encoding="utf-8")
    env = dict(os.environ, PYTHONPATH=os.path.join(ROOT, "src"))
    p = subprocess.run([sys.executable, "-m", "crashresume", str(d / "ag.py") + ":make", "--inputs", '["a"]', "--process"],
                       capture_output=True, env=env, timeout=120)
    assert p.returncode == 1 and b"effect_replayed" in p.stdout


# -- process mode: parallel scenarios give the identical report ----------------------------------------
def test_process_workers_give_the_same_report_as_one_worker(tmp_path):
    mod = tmp_path / "pw.py"
    mod.write_text("class A:\n    def __init__(self, ctx): self.ctx = ctx\n"
                   "    def step(self, j): self.ctx.effect('e', j, key=f'k:{j}')\n"
                   "def make(ctx): return A(ctx)\n", encoding="utf-8")
    one = conform_process(f"{mod}:make", ["a", "b", "c"], crash_before=True)
    four = conform_process(f"{mod}:make", ["a", "b", "c"], crash_before=True, workers=4)
    assert str(one) == str(four) and one.to_json(sort_keys=True) == four.to_json(sort_keys=True)
    assert one.crash_points == 6 and not one.passed
    for bad in (0, -1, True, 1.5):
        with pytest.raises(ValueError, match="workers"):
            conform_process(f"{mod}:make", ["a"], workers=bad)


def test_a_key_the_baseline_repeats_but_resume_issues_fewer_times_is_lost_work():
    class Eager:
        """Both inputs legitimately use the key 'tick'; the agent checkpoints before doing the work."""
        def __init__(self, ctx):
            self.ctx = ctx
            ctx.store.setdefault("done", [])

        def step(self, j):
            if j in self.ctx.store["done"]:
                return
            self.ctx.store["done"].append(j)
            self.ctx.effect("tick", j, key="tick")

        def state(self): return None
    rep = conform(Eager, ["a", "b"], crash_before=True)
    assert any(f.kind == "effect_lost" and "issued 1 time(s), baseline 2" in f.detail for f in rep.findings), str(rep)
    assert any("baseline issued key 'tick' 2 times" in w for w in rep.warnings)


def test_a_key_that_did_not_exist_in_the_baseline_is_flagged():
    class Drift:
        """A request counter leaks into the idempotency key: after a crash the same work gets a new key."""
        def __init__(self, ctx):
            self.ctx = ctx
            ctx.store.setdefault("done", [])
            ctx.store["n"] = ctx.store.get("n", 0) + 1

        def step(self, j):
            if j in self.ctx.store["done"]:
                return
            self.ctx.effect("e", j, key=f"{j}:attempt{self.ctx.store['n']}")
            self.ctx.store["done"].append(j)

        def state(self): return None
    rep = conform(Drift, ["a"])
    assert any(f.kind == "effect_replayed" and "more often than in baseline" in f.detail for f in rep.findings), str(rep)


def test_thorough_includes_crash_during_recovery():
    from crashresume.cli import OneShotRecoveryAgent
    assert conform(OneShotRecoveryAgent, ["a", "b", "c"]).passed
    rep = conform(OneShotRecoveryAgent, ["a", "b", "c"], thorough=True)
    assert any(f.kind == "effect_replayed" and len(f.crashes) == 2 for f in rep.findings)


# -- disk full (faults=True at ctx.fs.write) ------------------------------------------------------------
def _corpus():
    import importlib.util
    spec = importlib.util.spec_from_file_location("crashresume_bench_corpus", os.path.join(ROOT, "benchmarks", "corpus.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["crashresume_bench_corpus"] = mod   # dataclasses look the module up by name
    spec.loader.exec_module(mod)
    return mod


def test_disk_full_stores_half_then_fails_with_enospc():
    import errno
    fs = SimFS()
    ctx = Context("r", EffectLog(), crash_at=1, crash_when="diskfull", fs=fs)
    with pytest.raises(OSError) as ei:
        fs.write("f", "abcdef")
    assert ei.value.errno == errno.ENOSPC
    assert fs.read("f") == b"abc" and ctx.faulted
    fs.write("f", "xyz", append=True)          # the fault fires once; the next write is whole
    assert fs.read("f") == b"abcxyz"


def test_retrying_an_append_after_a_half_written_line_corrupts_the_journal():
    corpus = _corpus()
    welds, repairs = corpus.FaultsJournalRetryWelds, corpus.FaultsJournalRepairsOnRetry
    assert conform(welds, ["a", "b"], thorough=True).passed        # restart repair is fine: crashes alone never show it
    rep = conform(welds, ["a", "b"], faults=True)
    bad = [f for f in rep.findings if f.when == "diskfull"]
    assert bad and {f.kind for f in bad} == {"resume_diverged"}, str(rep)
    assert "disk full during fs.write #1" in str(rep)
    assert conform(repairs, ["a", "b"], thorough=True, faults=True).passed


def test_atomic_replace_survives_a_full_disk():
    class Atomic:
        def __init__(self, ctx): self.ctx = ctx
        def step(self, item):
            fs = self.ctx.fs
            done = json.loads(fs.read_text("cur")) if fs.exists("cur") else []
            if item not in done:
                fs.write_atomic("cur", json.dumps(done + [item]))
        def state(self): return json.loads(self.ctx.fs.read_text("cur")) if self.ctx.fs.exists("cur") else []
    rep = conform(Atomic, ["a", "b"], faults=True, strict_dirs=True)
    assert rep.passed, str(rep)
    assert any(s.endswith("-d1") for s in rep.scenarios)


# -- dedupe: a resource that honours the idempotency key ------------------------------------------------
class Dedupes:
    def __init__(self, ctx):
        self.ctx = ctx
        ctx.store.setdefault("done", [])

    def step(self, j):
        self.ctx.effect("charge", j, key=f"c:{j}", dedupe=True)          # no reconcile at all
        if j not in self.ctx.store["done"]:
            self.ctx.store["done"].append(j)

    def state(self): return sorted(self.ctx.store["done"])


def test_dedupe_removes_replays_in_every_mode():
    class Plain(Dedupes):
        def step(self, j):
            self.ctx.effect("charge", j, key=f"c:{j}")
            if j not in self.ctx.store["done"]:
                self.ctx.store["done"].append(j)
    assert not conform(Plain, ["a", "b"]).passed
    rep = conform(Dedupes, ["a", "b"], thorough=True, faults=True, stale_worker=True)
    assert rep.passed, str(rep)


def test_dedupe_hit_records_nothing_and_a_tool_returns_the_first_result():
    calls = []
    log = EffectLog()
    ctx = Context("r", log)
    first = ctx.effect("e", 1, key="k", dedupe=True)
    again = ctx.effect("e", 2, key="k", dedupe=True)
    assert again is first and len(log) == 1
    tool = ctx.tool(lambda x: calls.append(x) or f"result-{x}", kind="t", key="tk", dedupe=True)
    assert tool(1) == "result-1" and tool(2) == "result-1" and calls == [1]
    ctx.effect("e", 3, key="k")                # not asking for dedupe: recorded again
    assert len(log) == 3
    ctx.effect("e", 4, key=None, dedupe=True)  # keyless effects cannot be deduplicated
    ctx.effect("e", 4, key=None, dedupe=True)
    assert len(log) == 5


def test_fence_must_be_an_int():
    ctx = Context("r", EffectLog())
    for bad in ("1", 1.5, True):
        with pytest.raises(TypeError, match="fence must be an int"):
            ctx.effect("e", key="k", fence=bad)


# -- report to standard output -------------------------------------------------------------------------
def test_report_to_stdout_keeps_it_parseable(tmp_path, monkeypatch, capsys):
    (tmp_path / "ag.py").write_text("class A:\n    def __init__(self, ctx): self.ctx = ctx\n"
                                    "    def step(self, j): self.ctx.effect('e', j, key=j)\n"
                                    "def make(ctx): return A(ctx)\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert main(["ag.py:make", "--inputs", '["a"]', "--json", "-"]) == 1
    out, err = capsys.readouterr()
    assert json.loads(out)["passed"] is False and "[effect_replayed]" in err and "[effect_replayed]" not in out
    assert main(["ag.py:make", "--inputs", '["a"]', "--json", "-", "--sarif", "-"]) == 2
    assert "only one of --json, --sarif" in capsys.readouterr().err
    assert not (tmp_path / "-").exists()


# -- the mutation list stays applicable --------------------------------------------------------------
def test_every_mutant_still_applies_to_the_source():
    import importlib.util
    spec = importlib.util.spec_from_file_location("crashresume_mutants", os.path.join(ROOT, "benchmarks", "mutants.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert len(mod.MUTANTS) >= 20
    for name, old, new, why in mod.MUTANTS:
        text = open(os.path.join(ROOT, "src", "crashresume", name), encoding="utf-8").read()
        assert text.count(old) == 1 and old != new, f"mutant no longer applies: {why}"


def test_process_mode_hands_out_generations(tmp_path):
    mod = tmp_path / "gen.py"
    mod.write_text("class A:\n    def __init__(self, ctx): self.ctx = ctx\n"
                   "    def step(self, j): self.ctx.effect('gen', self.ctx.generation, key=j)\n"
                   "def make(ctx): return A(ctx)\n", encoding="utf-8")
    rep = conform_process(f"{mod}:make", ["a"], check_payloads=True)
    # baseline is generation 1; the resume after one crash is generation 2, so its payload differs from the baseline's
    assert any(f.kind == "effect_diverged" for f in rep.findings), str(rep)


def test_failure_injection_corpora_match_the_committed_numbers():
    corpus = _corpus()
    committed = json.load(open(os.path.join(ROOT, "benchmarks", "RESULTS.json"), encoding="utf-8"))
    for key, cases, opt in (("faults", corpus.corpus_faults(), "faults"), ("stale", corpus.corpus_stale(), "stale_worker")):
        base, full = (committed[key][label] for label in ("thorough (crashes only)", f"thorough + {opt}"))
        broken = [c for c in cases if c.expected]
        assert base["recall"] == 0.0 and full["recall"] == 1.0 and full["precision"] == 1.0 and not full["false_alarms"]
        for c in cases:            # re-run the cheap half live: the options find exactly what the labels say
            rep = conform(c.factory, c.inputs, thorough=True, **{opt: True})
            assert bool(rep.findings) == bool(c.expected), (c.name, str(rep))
            if c.expected:
                assert {f.kind for f in rep.findings} & c.expected, c.name
        assert len(broken) == full["tp"]
