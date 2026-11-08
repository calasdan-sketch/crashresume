"""Re-runnable benchmark: crash-safety bug detection on a labelled corpus.

    python benchmarks/run.py            # prints the table, rewrites benchmarks/RESULTS.{md,json}

Strategies compared on the SAME oracle (the baseline-diff invariants), differing only
in how crash points are chosen and what the fault model can express:

* random-kill x5     chaos-monkey style: 5 runs, each killing at one uniformly random
                     effect (seeded; mean over 200 seeds), fs treated as always durable
* 0.2.0-equivalent   exhaustive single crash after each effect; fs durable and invisible
* default            conform() in 0.3.0: exhaustive after-crash, fs ops are crash points
* thorough           conform(thorough=True, check_payloads=True): + crash-before,
                     crash-during-recovery, torn writes, strict keyless
* process mode       conform_process() on the real-kill corpus
"""
from __future__ import annotations

import json
import os
import random
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "src"))

from corpus import corpus, corpus_dirs, corpus_faults, corpus_stale  # noqa: E402

from crashresume import conform, conform_process, replay  # noqa: E402
from crashresume.core import Context, EffectLog, _run  # noqa: E402


def fs_as_in_020(factory):
    """What 0.2.0 modelled: file writes are durable immediately and are not crash points."""
    def wrapped(ctx):
        ctx.fs._bind(lambda kind: None, lambda tok, kind: None)
        ctx.fs.power_loss = lambda torn=False: None
        return factory(ctx)
    return wrapped


def effect_points(factory, inputs):
    ctx = Context("probe", EffectLog())
    _run(factory, inputs, ctx)
    return [i + 1 for i, k in enumerate(ctx.points) if k == "effect"]


def detects(report, expected):
    return any(f.kind in expected for f in report.findings)


def run_random(case, seeds=200, runs=5):
    fac = fs_as_in_020(case.factory)
    pts = effect_points(fac, case.inputs)
    rate, scen = 0.0, 0
    for seed in range(seeds):
        rng = random.Random(seed)
        hit = False
        for r in range(runs):
            k = rng.choice(pts)
            rep = replay(fac, case.inputs, [(k, "after")], run_id=f"rnd{seed}-{r}")
            scen += 1
            hit = hit or (detects(rep, case.expected) if case.expected else bool(rep.findings))
        rate += hit
    return rate / seeds, scen / seeds


def run_config(name, cases):
    rows, total = [], 0
    for c in cases:
        if name == "0.2.0-equivalent":
            rep = conform(fs_as_in_020(c.factory), c.inputs, run_id=f"b-{name}-{c.name}")
        elif name == "default":
            rep = conform(c.factory, c.inputs, run_id=f"b-{name}-{c.name}")
        elif name == "thorough":
            rep = conform(c.factory, c.inputs, run_id=f"b-{name}-{c.name}", thorough=True, check_payloads=True)
        else:
            raise ValueError(name)
        total += rep.crash_points
        rows.append({"case": c.name, "expected": sorted(c.expected), "kinds": sorted({f.kind for f in rep.findings}),
                     "flagged": bool(rep.findings), "detected": detects(rep, c.expected) if c.expected else None,
                     "crash_points": rep.crash_points})
    return rows, total


def score(rows):
    bugs = [r for r in rows if r["expected"]]
    clean = [r for r in rows if not r["expected"]]
    tp = sum(1 for r in bugs if r["detected"])
    fp = sum(1 for r in clean if r["flagged"])
    fn = len(bugs) - tp
    prec = tp / (tp + fp) if tp + fp else 1.0
    rec = tp / len(bugs)
    return {"tp": tp, "fp": fp, "fn": fn, "precision": round(prec, 3), "recall": round(rec, 3),
            "f1": round(2 * prec * rec / (prec + rec), 3) if prec + rec else 0.0,
            "missed": [r["case"] for r in bugs if not r["detected"]], "false_alarms": [r["case"] for r in clean if r["flagged"]]}


PROC = {"ProcGood": set(), "ProcMemoryCheckpoint": {"effect_replayed"}, "ProcFinallyCheckpoint": {"effect_replayed"}}


def run_process():
    import corpus_proc
    out = []
    for name, expected in PROC.items():
        fac = getattr(corpus_proc, name)
        corpus_proc._MEM.clear()
        sim = conform(fac, ["a", "b", "c"], run_id=f"sim-{name}")
        corpus_proc._MEM.clear()
        sim_t = conform(fac, ["a", "b", "c"], run_id=f"simt-{name}", thorough=True)
        corpus_proc._MEM.clear()
        os.environ["PYTHONPATH"] = str(HERE) + os.pathsep + os.environ.get("PYTHONPATH", "")
        real = conform_process(f"corpus_proc:{name}", ["a", "b", "c"], run_id=f"proc-{name}")
        out.append({"case": name, "expected": sorted(expected),
                    "simulated_flagged": bool(sim.findings), "simulated_thorough_flagged": bool(sim_t.findings),
                    "simulated_thorough_kinds": sorted({f.kind for f in sim_t.findings}),
                    "process_kinds": sorted({f.kind for f in real.findings}),
                    "process_flagged": bool(real.findings),
                    "process_crash_points": real.crash_points})
    return out


def main(argv):
    cases = corpus()
    bugs = sum(1 for c in cases if c.expected)
    result = {"corpus": {"agents": len(cases), "broken": bugs, "correct": len(cases) - bugs}, "strategies": {}}
    rate, avg = [], []
    for c in cases:
        if c.expected:
            r, s = run_random(c)
            rate.append(r)
            avg.append(s)
    cleanfp = sum(1 for c in cases if not c.expected and run_random(c, seeds=20)[0] > 0)
    result["strategies"]["random-kill x5"] = {
        "recall": round(sum(rate) / len(rate), 3), "precision": 1.0 if not cleanfp else 0.0,
        "crash_points_per_agent": round(sum(avg) / len(avg), 1), "note": "expected recall over 200 seeds"}
    for name in ("0.2.0-equivalent", "default", "thorough"):
        rows, total = run_config(name, cases)
        sc = score(rows)
        sc["crash_points_total"] = total
        sc["rows"] = rows
        result["strategies"][name] = sc
    dirs = corpus_dirs()
    result["strict_dirs"] = {}
    for label, kw in (("thorough (names always durable)", {}), ("thorough + strict_dirs", {"strict_dirs": True})):
        rows = []
        for c in dirs:
            rep = conform(c.factory, c.inputs, run_id=f"b-dirs-{c.name}", thorough=True, **kw)
            rows.append({"case": c.name, "expected": sorted(c.expected), "kinds": sorted({f.kind for f in rep.findings}),
                         "flagged": bool(rep.findings), "detected": detects(rep, c.expected) if c.expected else None,
                         "crash_points": rep.crash_points})
        sc = score(rows)
        sc["crash_points_total"] = sum(r_["crash_points"] for r_ in rows)
        sc["rows"] = rows
        result["strict_dirs"][label] = sc
    flt, stl = corpus_faults(), corpus_stale()
    result["faults"], result["stale"] = {}, {}
    for key, cases_, label_kw in (
            ("faults", flt, (("thorough (crashes only)", {}), ("thorough + faults", {"faults": True}))),
            ("stale", stl, (("thorough (crashes only)", {}), ("thorough + stale_worker", {"stale_worker": True})))):
        for label, kw in label_kw:
            rows = []
            for c in cases_:
                rep = conform(c.factory, c.inputs, run_id=f"b-{key}-{c.name}", thorough=True, **kw)
                rows.append({"case": c.name, "expected": sorted(c.expected), "kinds": sorted({f.kind for f in rep.findings}),
                             "flagged": bool(rep.findings), "detected": detects(rep, c.expected) if c.expected else None,
                             "crash_points": rep.crash_points})
            sc = score(rows)
            sc["crash_points_total"] = sum(r_["crash_points"] for r_ in rows)
            sc["rows"] = rows
            result[key][label] = sc
    result["process_mode"] = run_process()
    text = json.dumps(result, indent=2, sort_keys=True) + "\n"
    with open(HERE / "RESULTS.json", "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    md = ["# Benchmark results (generated by benchmarks/run.py)", "",
          f"Corpus: {result['corpus']['agents']} agents ({result['corpus']['broken']} broken, "
          f"{result['corpus']['correct']} correct). Detection = at least one finding of an expected kind; "
          "a finding on a correct agent is a false positive.", "",
          "| strategy | recall | precision | F1 | crash points (total) | missed bugs |", "|---|---|---|---|---|---|"]
    r = result["strategies"]["random-kill x5"]
    md.append(f"| random-kill x5 (chaos-style, mean of 200 seeds) | {r['recall']:.3f} | {r['precision']:.3f} | - | "
              f"{r['crash_points_per_agent']} per agent | (probabilistic) |")
    for name in ("0.2.0-equivalent", "default", "thorough"):
        s = result["strategies"][name]
        md.append(f"| {name} | {s['recall']:.3f} | {s['precision']:.3f} | {s['f1']:.3f} | {s['crash_points_total']} | "
                  f"{', '.join(s['missed']) or 'none'} |")
    nd = len(dirs)
    nb = sum(1 for c in dirs if c.expected)
    md += ["", f"Directory-durability corpus ({nd} agents, {nb} broken: a new name is not durable until its "
           "directory is fsynced):",
           "", "| strategy | recall | precision | crash points (total) | missed bugs |", "|---|---|---|---|---|"]
    for label, s in result["strict_dirs"].items():
        md.append(f"| {label} | {s['recall']:.3f} | {s['precision']:.3f} | {s['crash_points_total']} | "
                  f"{', '.join(s['missed']) or 'none'} |")
    for key, cases_, title in (
            ("faults", flt, "retries after a timeout or a refusal (faults=True)"),
            ("stale", stl, "workers that wake after their replacement finished (stale_worker=True)")):
        nb_ = sum(1 for c in cases_ if c.expected)
        md += ["", f"Failure-injection corpus, {title}: {len(cases_)} agents, {nb_} broken:",
               "", "| strategy | recall | precision | crash points (total) | missed bugs |", "|---|---|---|---|---|"]
        for label, s_ in result[key].items():
            md.append(f"| {label} | {s_['recall']:.3f} | {s_['precision']:.3f} | {s_['crash_points_total']} | "
                      f"{', '.join(s_['missed']) or 'none'} |")
    md += ["", "Process mode (real SIGKILL, fresh interpreter per life) on the cases a simulator cannot judge:", "",
           "| agent | truth | simulated (default) | simulated (thorough) | real kill (default) | real-kill scenarios |",
           "|---|---|---|---|---|---|"]
    for p in result["process_mode"]:
        md.append(f"| {p['case']} | {'broken' if p['expected'] else 'correct'} | "
                  f"{'FLAGGED' if p['simulated_flagged'] else 'pass'} | "
                  f"{', '.join(p['simulated_thorough_kinds']) or 'pass'} | {', '.join(p['process_kinds']) or 'pass'} | "
                  f"{p['process_crash_points']} |")
    with open(HERE / "RESULTS.md", "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(md) + "\n")
    print("\n".join(md))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
