# ruff: noqa: E501
"""Mutation sanity check: break the library on purpose and confirm the test suite notices.

    python benchmarks/mutants.py            # runs every mutant, prints a table, rewrites benchmarks/MUTANTS.md

Each mutant is one exact text replacement in a COPY of ``src/`` (the real tree is never touched). The fast part
of the test suite (everything except real-process and slow-corpus tests) runs against the copy; a mutant is
"killed" when that run fails. A surviving mutant means a rule the tests do not pin; it is listed, not hidden.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

# (file, old text - must occur exactly once, new text, what the mutant breaks)
MUTANTS = [
    ("core.py", "        elif n > base_keys.get(key, 0):\n            add(\"effect_replayed\"",
     "        elif n > base_keys.get(key, 0) + 1:\n            add(\"effect_replayed\"",
     "one extra issue of a key is no longer called a replay"),
    ("core.py", "        elif seen_n < want_n:\n            add(\"effect_lost\"", "        elif seen_n < want_n - 5:\n            add(\"effect_lost\"",
     "a key issued fewer times than the baseline is ignored"),
    ("core.py", "armed = self._crash_at is not None and self._count >= self._crash_at",
     "armed = self._crash_at is not None and self._count > self._crash_at", "crash points are off by one"),
    ("core.py", "if _is_empty(final_state) and not _is_empty(base_state):", "if _is_empty(final_state) or _is_empty(base_state):",
     "state_lost fires on any empty state"),
    ("core.py", "modes = (AFTER, BEFORE) if crash_before else (AFTER,)\n    extra",
     "modes = (AFTER,)\n    extra", "crash_before does nothing"),
    ("core.py", "        crash_before, torn_writes, strict_keyless = True, True, True\n        max_crashes = max(max_crashes, 2)",
     "        crash_before, torn_writes, strict_keyless = True, True, True", "thorough forgets crash-during-recovery"),
    ("core.py", "            if self._crash_when == TIMEOUT:\n                raise InjectedTimeout",
     "            if self._crash_when == REFUSED:\n                raise InjectedTimeout", "a timeout fault does not raise after the commit"),
    ("core.py", "                if self._crash_when == REFUSED:\n                    if self.trace is not None:",
     "                if self._crash_when == TIMEOUT:\n                    if self.trace is not None:", "a refusal fault lets the effect run"),
    ("core.py", "        if high is not None and fence < high:", "        if high is not None and fence <= high:",
     "a fencing token equal to the newest is rejected"),
    ("core.py", "        if high is not None and fence < high:", "        if high is not None and fence < high - 10:",
     "a stale fencing token is accepted"),
    ("core.py", "        mark = len(log)\n        release.set()", "        mark = len(log)\n        pass",
     "the stale worker is never woken"),
    ("core.py", "if when == STALE and kind in (\"effect_replayed\", \"effect_diverged\", \"resume_diverged\"):",
     "if False:", "stale-worker findings keep the generic kind"),
    ("core.py", "        if ch.isprintable() or ch == \" \":", "        if True:", "terminal control characters are not escaped"),
    ("core.py", "return self._first.get(key, self._n) < self._n", "return self._first.get(key, self._n) <= self._n",
     "KeyView says every key is present"),
    ("core.py", "        return sum(1 for k in self._keys if k == key) if self.has(key) else 0",
     "        return 0", "EffectLog.count always says 0"),
    ("core.py", "points[round(i * (len(points) - 1) / (sample - 1))]", "points[round(i * (len(points) - 2) / (sample - 1))]",
     "sample= drops the last crash point"),
    ("core.py", "return self.crash_points == 0", "return False", "a run that tested nothing is not vacuous"),
    ("core.py", "TIMEOUT: \"o\", REFUSED: \"r\", STALE: \"z\"", "TIMEOUT: \"o\", REFUSED: \"o\", STALE: \"z\"",
     "two fault kinds share a scenario id"),
    ("fs.py", "self._files[p].durable = self._files[p].current", "pass", "fsync does not make data durable"),
    ("fs.py", "                f.current = f.durable\n            f.durable = f.current", "                pass\n            f.durable = f.current",
     "a power cut loses nothing"),
    ("fs.py", "f.current = f.durable + tail[: len(tail) // 2]", "f.current = f.durable + tail", "a torn write is not torn"),
    ("fs.py", "            self._files = dict(self._dnames)", "            pass", "names do not need fsync_dir under strict_dirs"),
    ("fs.py", "os.makedirs(os.path.dirname(b), exist_ok=True)   # directories are implicit, as in SimFS", "pass",
     "RealFS.rename into a new directory fails"),
    ("core.py", "                raise OSError(errno.ENOSPC, f\"No space left on device (fault injected after half of {desc})\")", "                pass",
     "a full disk does not fail the write"),
    ("fs.py", "            d = d[: len(d) // 2]   # disk-full fault: only half reaches the file before the error", "            pass",
     "a full disk stores all of the data"),
    ("core.py", "        if dedupe and key is not None and key in self._log._by_key:", "        if False:", "dedupe=True is ignored by ctx.effect"),
    ("core.py", "            seen = dedupe and k is not None and k in self._log._by_key", "            seen = False",
     "a deduplicated tool call does not return the first result"),
    ("cli.py", "    human = sys.stderr if to_stdout else sys.stdout", "    human = sys.stdout", "--json - mixes the text report into the JSON"),
    ("reports.py", "\"organization\": __author__,", "", "SARIF does not name the author"),
]

# Skip the slow, real-subprocess tests; everything else is a few seconds.
PYTEST = ["-q", "-x", "-p", "no:cacheprovider", "-k", "not process and not benchmark_corpus and not real_filesystem",
          "tests/test_core.py", "tests/test_round3.py", "tests/test_round4.py", "tests/test_round5.py",
          "tests/test_modes.py", "tests/test_expert2.py", "tests/test_plugin.py"]


def run(src: Path) -> int:
    env = dict(os.environ, PYTHONPATH=str(src))
    # pyproject's pythonpath = ["src"] would put the real tree first, so point pytest at the copy explicitly
    p = subprocess.run([sys.executable, "-m", "pytest", "-o", f"pythonpath={src}", *PYTEST], cwd=ROOT, env=env,
                       capture_output=True, timeout=900)
    return p.returncode


def main() -> int:
    rows = []
    with tempfile.TemporaryDirectory(prefix="crashresume-mutants-") as tmp:
        base = Path(tmp) / "base"
        shutil.copytree(ROOT / "src", base, ignore=shutil.ignore_patterns("__pycache__"))
        clean_rc = run(base)
        if clean_rc != 0:
            print("the unmodified copy already fails the fast tests; fix that first", file=sys.stderr)
            return 2
        for n, (name, old, new, why) in enumerate(MUTANTS, 1):
            work = Path(tmp) / f"m{n}"
            shutil.copytree(base, work)
            target = work / "crashresume" / name
            text = target.read_text(encoding="utf-8")
            if text.count(old) != 1:
                print(f"mutant {n}: text not found exactly once in {name}: {old!r}", file=sys.stderr)
                return 2
            target.write_text(text.replace(old, new), encoding="utf-8", newline="\n")
            killed = run(work) != 0
            rows.append((n, name, why, killed))
            print(f"{n:2d} {'killed  ' if killed else 'SURVIVED'} {name}: {why}", flush=True)
            shutil.rmtree(work)
    killed = sum(1 for r in rows if r[3])
    md = ["# Mutation sanity check (generated by benchmarks/mutants.py)", "",
          f"{killed} of {len(rows)} deliberate breakages were caught by the fast test suite.", "",
          "| # | file | what was broken | result |", "|---|---|---|---|"]
    md += [f"| {n} | {name} | {why} | {'killed' if k else '**SURVIVED**'} |" for n, name, why, k in rows]
    (HERE / "MUTANTS.md").write_text("\n".join(md) + "\n", encoding="utf-8", newline="\n")
    return 0 if killed == len(rows) else 1


if __name__ == "__main__":
    sys.exit(main())
