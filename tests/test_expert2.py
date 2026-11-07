"""Tests for the 0.3.0 features: SimFS/torn writes, process mode, minimize/replay, adapters, reports, CLI, benchmark."""
import asyncio
import json
import pathlib
import sys
import xml.etree.ElementTree as ET

import pytest

from crashresume import SetupError, SimFS, conform, conform_process, replay
from crashresume.adapters import from_async, from_function
from crashresume.cli import main
from crashresume.core import clean
from crashresume.fs import RealFS

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "benchmarks"))

import corpus as C  # noqa: E402
import run as bench  # noqa: E402


# ---------------------------------------------------------------- SimFS
def test_simfs_unsynced_data_is_lost_and_synced_data_survives():
    fs = SimFS()
    fs.write("a", "one")
    fs.fsync("a")
    fs.write("a", "twotwo")           # truncating rewrite, not synced
    fs.write("b", "never synced")
    fs.power_loss()
    assert fs.read_text("a") == "one"
    assert fs.read_text("b") == ""     # name durable, data not


def test_simfs_torn_keeps_half_of_the_unsynced_tail():
    fs = SimFS()
    fs.write("j", "AAAA\n", append=True)
    fs.fsync("j")
    fs.write("j", "BBBBBB\n", append=True)
    fs.power_loss(torn=True)
    assert fs.read_text("j") == "AAAA\nBBB"
    fs2 = SimFS()
    fs2.write("x", "0123456789")
    fs2.power_loss(torn=True)
    assert fs2.read_text("x") == "01234"


def test_simfs_rename_without_fsync_leaves_empty_file():
    fs = SimFS()
    fs.write("t", "payload")
    fs.rename("t", "final")
    fs.power_loss()
    assert fs.exists("final") and fs.read("final") == b""


def test_simfs_write_atomic_is_crash_safe():
    fs = SimFS()
    fs.write_atomic("f", "v1")
    fs.power_loss()
    assert fs.read_text("f") == "v1"


@pytest.mark.parametrize("bad", ["", "../x", "a/../../x", "/abs", "C:\\x", "a/..", "."])
def test_simfs_rejects_escaping_or_empty_paths(bad):
    with pytest.raises(ValueError):
        SimFS().write(bad, "x")


def test_simfs_windows_separators_normalised():
    fs = SimFS()
    fs.write("out\\a.txt", "x")
    assert fs.exists("out/a.txt") and fs.listdir("out") == ["out/a.txt"]


def test_simfs_type_errors_and_missing_files():
    fs = SimFS()
    with pytest.raises(TypeError):
        fs.write("a", 12)
    with pytest.raises(FileNotFoundError):
        fs.read("nope")
    with pytest.raises(FileNotFoundError):
        fs.fsync("nope")


def test_realfs_matches_api_and_blocks_escape(tmp_path):
    fs = RealFS(str(tmp_path / "root"))
    fs.write_atomic("d/x", "hi")
    assert fs.read_text("d/x") == "hi" and fs.exists("d/x") and fs.listdir("d") == ["d/x"]
    fs.remove("d/x")
    assert not fs.exists("d/x")
    with pytest.raises(ValueError):
        fs.write("../evil", "x")


# ---------------------------------------------------------------- fs crash points
def case(name):
    return next(c for c in C.corpus() if c.name == name)


def test_fs_ops_are_crash_points_and_good_fs_agents_pass_everything():
    for name in ("fs_atomic_artifact", "fs_journal_fsync"):
        c = case(name)
        rep = conform(c.factory, c.inputs, run_id="g" + name, thorough=True)
        assert rep.passed, str(rep)
        assert rep.crash_points > 3 * len(c.inputs)   # fs ops multiplied the crash points


@pytest.mark.parametrize("name", [c.name for c in C.corpus() if c.name.startswith("fs_") and c.expected])
def test_each_broken_fs_agent_is_caught_by_thorough(name):
    c = case(name)
    rep = conform(c.factory, c.inputs, run_id="b" + name, thorough=True)
    assert any(f.kind in c.expected for f in rep.findings), str(rep)


def test_torn_writes_add_torn_scenarios_on_top_of_unsynced_loss():
    c = case("fs_torn_cursor")
    base = conform(c.factory, c.inputs, run_id="t0")
    assert not base.passed and not any(f.when == "torn" for f in base.findings)   # empty file after unsynced create
    rep = conform(c.factory, c.inputs, run_id="t1", torn_writes=True)
    assert rep.crash_points > base.crash_points
    assert any(f.kind == "subject_error" and f.when == "torn" for f in rep.findings), str(rep)
    assert "fs.write" in str(rep)


def test_torn_writes_without_fs_warns():
    rep = conform(C.ReconcileLedger, ["a"], torn_writes=True)
    assert any("never wrote through ctx.fs" in w for w in rep.warnings)


def test_artifact_without_fsync_found_in_default_mode():
    c = case("fs_artifact_no_fsync")
    rep = conform(c.factory, c.inputs, run_id="d")
    assert any(f.kind in ("resume_diverged", "state_lost") for f in rep.findings)


# ---------------------------------------------------------------- strict keyless / tool
def test_strict_keyless_turns_warning_into_finding():
    c = case("keyless_double_send")
    soft = conform(c.factory, c.inputs, run_id="k0")
    assert soft.passed and any("keyless" in w for w in soft.warnings)
    hard = conform(c.factory, c.inputs, run_id="k1", strict_keyless=True)
    assert any(f.kind == "effect_replayed" for f in hard.findings)


def test_tool_wrapper_runs_fn_between_before_and_after_points():
    ran = []

    class A:
        def __init__(self, ctx):
            self.ctx = ctx
            self.t = ctx.tool(lambda x: ran.append(x) or x * 2, kind="dbl", key=lambda x: f"k{x}")

        def step(self, item):
            assert self.t(item) == item * 2

    conform(A, [1], run_id="tw", crash_before=True)
    # runs: baseline; after-crash life + its resume; before-crash resume only (fn must NOT run before a 'before' crash)
    assert ran.count(1) == 4


def test_tool_wrapper_rejects_unhashable_key_and_supports_async():
    from crashresume import Context, EffectLog
    ctx = Context("r", EffectLog())
    with pytest.raises(TypeError):
        ctx.tool(lambda: 1, key=lambda: [1])()

    async def atool(x):
        return x + 1

    wrapped = ctx.tool(atool, key="a")
    assert asyncio.run(wrapped(1)) == 2 and ctx.effects.keys() == ["a"]


# ---------------------------------------------------------------- adapters
def test_from_async_detects_replay_and_propagates_the_crash():
    class AsyncAgent:
        def __init__(self, ctx):
            self.ctx = ctx

        async def step(self, item):
            await asyncio.sleep(0)
            self.ctx.effect("pay", item, key=f"pay:{item}")

        def state(self):
            return sorted(self.ctx.effects.keys())

    rep = conform(from_async(AsyncAgent), ["a", "b"], run_id="as")
    assert not rep.passed and {f.kind for f in rep.findings} == {"effect_replayed", "resume_diverged"}


def test_from_async_inside_a_running_loop():
    class A:
        def __init__(self, ctx):
            self.ctx = ctx

        async def step(self, item):
            self.ctx.effect("e", item, key=item)

    async def go():
        return conform(from_async(A), ["a"], run_id="loop")

    assert not asyncio.run(go()).passed


def test_from_function_default_state_is_store():
    rep = conform(from_function(C._loop), ["a", "b"], run_id="ff")
    assert any(f.kind == "effect_replayed" for f in rep.findings)


# ---------------------------------------------------------------- replay / minimize / fail_fast
def test_minimize_shrinks_inputs_and_crashes():
    c = case("replay_no_reconcile")
    rep = conform(c.factory, list("abcdefgh"), run_id="m", minimize=True)
    r = next(r for r in rep.repros if r.kind == "effect_replayed")
    assert len(r.inputs) == 1 and len(r.crashes) == 1
    assert "minimal repro" in str(rep)
    again = replay(c.factory, list(r.inputs), r.crashes, run_id="m-replay")
    assert any(f.kind == "effect_replayed" for f in again.findings)


def test_minimize_needs_two_crashes_for_recovery_bug():
    c = case("one_shot_recovery")
    rep = conform(c.factory, list("abcd"), run_id="m2", max_crashes=2, minimize=True)
    r = next(r for r in rep.repros if r.kind == "effect_replayed")
    assert len(r.crashes) == 2


def test_fail_fast_stops_at_first_finding():
    c = case("replay_no_reconcile")
    rep = conform(c.factory, c.inputs, run_id="ff1", fail_fast=True)
    assert len(rep.findings) == 1 and rep.crash_points == 1


def test_replay_validates_crash_list():
    c = case("replay_no_reconcile")
    for bad in ([], [(0, "after")], [(1, "sideways")]):
        with pytest.raises(ValueError):
            replay(c.factory, c.inputs, bad)


def test_finding_exposes_crash_sequence():
    c = case("one_shot_recovery")
    rep = conform(c.factory, c.inputs, run_id="seq", max_crashes=2)
    f = next(f for f in rep.findings if len(f.crashes) == 2)
    assert f.scenario.count("-") >= 2 and all(w in ("after", "before", "torn") for _, w in f.crashes)


# ---------------------------------------------------------------- sanitising
def test_clean_escapes_terminal_and_bidi_characters():
    out = clean("ok\x1b[31m\u202e\u200b\x00 end\n")
    assert "\x1b" not in out and "\u202e" not in out and "\u200b" not in out and "\x00" not in out
    assert clean("plain text") == "plain text"


def test_hostile_payload_cannot_reach_the_report_raw():
    class P:
        def __repr__(self):
            return "\x1b[2J\u202egnp.exe"

    class A:
        def __init__(self, ctx):
            self.ctx = ctx

        def step(self, item):
            self.ctx.effect("pay", P(), key="bad\x1b[2J")

    rep = conform(A, ["a"], run_id="hostile")
    text = str(rep) + rep.to_junit() + rep.to_sarif()
    assert "\x1b" not in text and "\u202e" not in text


# ---------------------------------------------------------------- reports
def test_junit_is_valid_deterministic_and_has_one_case_per_scenario():
    c = case("replay_no_reconcile")
    rep = conform(c.factory, c.inputs, run_id="j")
    xml = rep.to_junit("suite")
    root = ET.fromstring(xml)
    assert int(root.get("tests")) == rep.crash_points == len(root.findall("testcase"))
    assert int(root.get("failures")) == len({f.scenario for f in rep.findings})
    assert xml == conform(c.factory, c.inputs, run_id="j").to_junit("suite")  # byte-identical


def test_sarif_shape():
    c = case("replay_no_reconcile")
    doc = json.loads(conform(c.factory, c.inputs, run_id="s").to_sarif("agent.py"))
    run = doc["runs"][0]
    assert doc["version"] == "2.1.0" and run["tool"]["driver"]["name"] == "crashresume"
    assert run["results"][0]["ruleId"] == "crashresume/effect_replayed"
    assert run["results"][0]["locations"][0]["physicalLocation"]["artifactLocation"]["uri"] == "agent.py"
    assert {r["id"] for r in run["tool"]["driver"]["rules"]} == {"crashresume/effect_replayed"}


def test_passing_report_has_no_results_and_valid_xml():
    c = case("reconcile_ledger")
    rep = conform(c.factory, c.inputs, run_id="p")
    assert json.loads(rep.to_sarif())["runs"][0]["results"] == []
    assert ET.fromstring(rep.to_junit()).get("failures") == "0"


# ---------------------------------------------------------------- process mode
def _proc(name, **kw):
    import os
    old = os.environ.get("PYTHONPATH")
    os.environ["PYTHONPATH"] = str(ROOT / "benchmarks") + (os.pathsep + old if old else "")
    try:
        return conform_process(f"corpus_proc:{name}", ["a", "b", "c"], run_id=f"p-{name}", **kw)
    finally:
        if old is None:
            os.environ.pop("PYTHONPATH", None)
        else:
            os.environ["PYTHONPATH"] = old


def test_process_mode_good_agent_passes_and_counts_real_kills():
    rep = _proc("ProcGood", crash_before=True)
    assert rep.passed, str(rep)
    assert rep.crash_points == 6
    assert any("real SIGKILL" in w for w in rep.warnings)


@pytest.mark.parametrize("name", ["ProcMemoryCheckpoint", "ProcFinallyCheckpoint"])
def test_process_mode_catches_what_the_simulator_cannot(name):
    import corpus_proc
    corpus_proc._MEM.clear()
    sim = conform(getattr(corpus_proc, name), ["a", "b", "c"], run_id=f"sim-{name}")
    assert sim.passed, str(sim)                    # same-process crash keeps the "database"
    real = _proc(name)
    assert any(f.kind == "effect_replayed" for f in real.findings), str(real)


def test_process_mode_real_filesystem_two_crashes(tmp_path):
    mod = tmp_path / "agent_mod.py"
    mod.write_text(
        "class A:\n"
        "    def __init__(self, ctx): self.ctx = ctx\n"
        "    def _done(self):\n"
        "        f = self.ctx.fs\n"
        "        return f.read_text('c').split() if f.exists('c') else []\n"
        "    def step(self, item):\n"
        "        if item in self._done(): return\n"
        "        if f'p:{item}' not in self.ctx.effects.keys(): self.ctx.effect('p', item, key=f'p:{item}')\n"
        "        self.ctx.fs.write_atomic('c', ' '.join(self._done() + [item]))\n"
        "    def state(self): return self._done()\n",
        encoding="utf-8")
    rep = conform_process(f"{mod}:A", ["x"], max_crashes=2, crash_before=True)
    assert rep.passed, str(rep)
    assert rep.crash_points > 10


def test_process_mode_fails_closed_on_bad_setup():
    with pytest.raises(SetupError, match="cannot load"):
        conform_process("no_such_module_xyz:f", ["a"])
    with pytest.raises(TypeError, match="JSON"):
        conform_process("m:f", [object()])
    with pytest.raises(ValueError):
        conform_process("m:f", ["a"], max_crashes=0)


def test_process_mode_store_use_is_a_clear_failure(tmp_path):
    mod = tmp_path / "storey.py"
    mod.write_text("class A:\n    def __init__(self, ctx): ctx.store['x'] = 1\n    def step(self, i): pass\n", encoding="utf-8")
    with pytest.raises(SetupError, match="baseline"):
        conform_process(f"{mod}:A", ["a"])


def test_process_mode_hang_is_reported(tmp_path):
    mod = tmp_path / "hang.py"
    mod.write_text(
        "import time\nclass A:\n    def __init__(self, ctx): self.ctx = ctx\n"
        "    def step(self, i):\n"
        "        self.ctx.effect('e', i, key=i)\n"
        "        if self.ctx.run_id != 'run': time.sleep(30)\n", encoding="utf-8")
    rep = conform_process(f"{mod}:A", ["a"], timeout=2)
    assert any(f.kind == "subject_hang" for f in rep.findings), str(rep)


# ---------------------------------------------------------------- CLI
def test_cli_exit_codes_and_artifacts(tmp_path, monkeypatch, capsys):
    monkeypatch.syspath_prepend(str(ROOT / "benchmarks"))
    out = lambda n: str(tmp_path / n)  # noqa: E731
    rc = main(["corpus:ReplayNoReconcile", "--inputs", '["a","b"]', "--json", out("r.json"),
               "--junit", out("j.xml"), "--sarif", out("s.sarif"), "--minimize"])
    assert rc == 1
    assert json.loads((tmp_path / "r.json").read_text())["passed"] is False
    ET.parse(out("j.xml"))
    assert json.loads((tmp_path / "s.sarif").read_text())["runs"][0]["results"]
    assert "minimal repro" in capsys.readouterr().out
    assert main(["corpus:ReconcileLedger", "--inputs", '["a","b"]']) == 0


@pytest.mark.parametrize("argv", [
    ["nope_mod:f", "--inputs", '["a"]'],
    ["corpus:ReconcileLedger"],
    ["corpus:ReconcileLedger", "--inputs", "not json"],
    ["corpus:ReconcileLedger", "--inputs", "[]"],
    ["corpus:ReconcileLedger", "--inputs", '["a"]', "--max-crashes", "0"],
    ["corpus:NoSuchAttr", "--inputs", '["a"]'],
])
def test_cli_bad_setup_exits_2_not_pass(argv, monkeypatch, capsys):
    monkeypatch.syspath_prepend(str(ROOT / "benchmarks"))
    assert main(argv) == 2
    assert "cannot run" in capsys.readouterr().err


def test_cli_process_flag(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "benchmarks"))
    monkeypatch.setenv("PYTHONPATH", str(ROOT / "benchmarks"))
    assert main(["corpus_proc:ProcMemoryCheckpoint", "--inputs", '["a"]', "--process"]) == 1


# ---------------------------------------------------------------- benchmark guards
def test_benchmark_corpus_labels_and_committed_numbers():
    cases = C.corpus()
    assert len({c.name for c in cases}) == len(cases)
    rows, total = bench.run_config("thorough", cases)
    sc = bench.score(rows)
    assert sc["recall"] == 1.0 and sc["precision"] == 1.0, sc
    committed = json.loads((ROOT / "benchmarks" / "RESULTS.json").read_text())
    assert committed["strategies"]["thorough"]["crash_points_total"] == total
    rows0, _ = bench.run_config("0.2.0-equivalent", cases)
    assert bench.score(rows0)["recall"] == committed["strategies"]["0.2.0-equivalent"]["recall"] < 0.6
    rows1, _ = bench.run_config("default", cases)
    assert bench.score(rows1)["recall"] == committed["strategies"]["default"]["recall"]


# ---------------------------------------------------------------- real framework (optional dependency)
def test_langgraph_example_replays_unless_guarded():
    pytest.importorskip("langgraph")
    sys.path.insert(0, str(ROOT / "examples"))
    import langgraph_resume as lg
    bad = conform(lg.build(False), ["a", "b"], run_id="lg-t0", crash_before=True)
    good = conform(lg.build(True), ["a", "b"], run_id="lg-t1", crash_before=True)
    assert any(f.kind == "effect_replayed" for f in bad.findings)
    assert good.passed, str(good)
