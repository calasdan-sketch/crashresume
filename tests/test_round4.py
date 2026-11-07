"""Round 4: what a stranger hits in the first hour (CLI errors, consoles, report files, huge reports)."""
import os
import subprocess
import sys
import textwrap

import pytest

from crashresume import conform
from crashresume.cli import main

REPLAY = textwrap.dedent('''
    class A:
        def __init__(self, ctx): self.ctx = ctx
        def step(self, item): self.ctx.effect("e", item, key=item)     # replays after a crash
    def make(ctx): return A(ctx)
    class Exits:
        def __init__(self, ctx): self.ctx = ctx
        def step(self, item): raise SystemExit(7)
    def make_exit(ctx): return Exits(ctx)
''')


@pytest.fixture
def agent(tmp_path, monkeypatch):
    (tmp_path / "ag.py").write_text(REPLAY, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def run_cli(args, cwd, env_extra=None):
    env = dict(os.environ)
    src = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
    env["PYTHONPATH"] = src + os.pathsep + env.get("PYTHONPATH", "")
    env.update(env_extra or {})
    return subprocess.run([sys.executable, "-m", "crashresume", *args], cwd=cwd, env=env, capture_output=True, timeout=120)


def test_unwritable_report_path_is_a_plain_error_not_a_traceback(agent, capsys):
    rc = main(["ag.py:make", "--inputs", '["a"]', "--json", str(agent / "no" / "such" / "dir" / "r.json")])
    err = capsys.readouterr().err
    assert rc == 2 and "cannot write report file" in err and "Traceback" not in err and ".tmp" not in err
    assert not list(agent.glob("**/*.tmp"))


def test_report_file_is_written_whole_and_leaves_no_temp_file(agent):
    rc = main(["ag.py:make", "--inputs", '["a"]', "--json", "r.json", "--junit", "j.xml", "--sarif", "s.sarif"])
    assert rc == 1
    assert sorted(p.name for p in agent.iterdir()) == ["__pycache__", "ag.py", "j.xml", "r.json", "s.sarif"] or \
        sorted(p.name for p in agent.iterdir() if p.name != "__pycache__") == ["ag.py", "j.xml", "r.json", "s.sarif"]


def test_console_that_cannot_show_a_character_does_not_crash(agent):
    p = run_cli(["ag.py:make", "--inputs", '["caf\\u00e9 \\u2603"]'], agent, {"PYTHONIOENCODING": "cp1252"})
    assert p.returncode == 1, p.stderr
    assert b"Traceback" not in p.stderr
    assert b"effect_replayed" in p.stdout


def test_bad_json_and_missing_module_messages_are_plain(agent, capsys):
    assert main(["ag.py:make", "--inputs", "not json"]) == 2
    err = capsys.readouterr().err
    assert "not valid JSON" in err and "JSONDecodeError" not in err
    assert main(["nosuchmod_r4:make", "--inputs", "[1]"]) == 2
    err = capsys.readouterr().err
    assert "nosuchmod_r4" in err and "folder that contains your code" in err
    assert main(["ag.py:nope", "--inputs", "[1]"]) == 2
    assert "name after the ':'" in capsys.readouterr().err


def test_agent_calling_sys_exit_is_a_setup_error_not_a_silent_exit(agent, capsys):
    assert main(["ag.py:make_exit", "--inputs", "[1]"]) == 2
    err = capsys.readouterr().err
    assert "sys.exit(7)" in err and "must not exit" in err


def test_huge_report_is_capped_in_text_but_complete_in_data():
    class A:
        def __init__(self, ctx): self.ctx = ctx
        def step(self, i): self.ctx.effect("e", i, key=i)
    r = conform(A, list(range(60)))
    assert len(r.findings) > 100
    text = str(r)
    assert text.count("[effect_replayed]") == r.MAX_SHOWN
    assert f"and {len(r.findings) - r.MAX_SHOWN} more finding(s)" in text
    assert len(r.to_dict()["findings"]) == len(r.findings)


def test_process_ledger_uses_plain_newlines(tmp_path):
    from crashresume._child import _FileLog
    path = tmp_path / "effects.jsonl"
    _FileLog(str(path)).record("k", {"a": 1}, "x")
    assert b"\r" not in path.read_bytes()


def test_nonpositive_timeout_is_rejected(agent, capsys):
    assert main(["ag.py:make", "--inputs", '["a"]', "--process", "--timeout", "0"]) == 2
    assert "greater than 0" in capsys.readouterr().err
