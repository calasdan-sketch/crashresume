"""CI report formats: JUnit XML and SARIF 2.1.0 (GitHub code scanning).

Both are deterministic: no timestamps, durations or host names, so the same
subject produces byte-identical files and CI diffs stay quiet.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List
from xml.etree import ElementTree as ET

from .core import ConformanceReport, clean, where

_XML_BAD = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]")

RULES: Dict[str, str] = {
    "effect_replayed": "A side effect with the same idempotency key ran again after a crash (double charge, duplicate email).",
    "effect_lost": "A side effect that happens in a crash-free run never happened after a crash and resume.",
    "effect_diverged": "After a crash a key was re-issued with a different payload than in the baseline.",
    "state_lost": "Final state is empty after resume although the crash-free run had state.",
    "resume_diverged": "Final state after crash and resume differs from the crash-free run.",
    "resume_crashed": "The clean resume hit an injected crash it was not given.",
    "subject_error": "The subject raised an ordinary exception after a crash (often a torn or missing checkpoint).",
    "crash_swallowed": "The subject caught the injected crash and kept running; a real crash cannot be caught.",
    "subject_hang": "A child process exceeded its timeout (process mode).",
    "stale_worker": "A worker that was paused woke up after its replacement had finished and wrote again (no fencing).",
}


FIXES: Dict[str, str] = {
    "effect_replayed": "Before issuing an effect, look its idempotency key up in the durable record of what already happened "
                       "(ctx.effects.keys() in tests; your ledger or the provider's idempotency key in production), and "
                       "make that check survive a crash during recovery too.",
    "effect_lost": "Do not mark work done before the effect has committed: checkpoint after the effect, or write an intent "
                   "first and reconcile it on restart.",
    "effect_diverged": "Derive the payload for a key from stable inputs; request ids and timestamps belong outside the "
                       "idempotent payload (or compare with payload_key=).",
    "state_lost": "Persist the checkpoint durably (fsync the file, and its directory under strict_dirs) before relying on it.",
    "resume_diverged": "Make recovery idempotent: re-running a step from a half-done state must reach the crash-free state.",
    "resume_crashed": "Recovery code must itself be crash-safe; reconcile on every start, not just the first.",
    "subject_error": "Validate checkpoints on load (torn or missing file, partial journal line) and recover instead of raising.",
    "crash_swallowed": "Do not catch BaseException around code that performs effects; a real crash cannot be caught.",
    "subject_hang": "Recovery waited on something a dead process still holds (a lock file, a lease): add expiry or owner checks.",
    "stale_worker": "Pass a fencing token with every effect (ctx.effect(..., fence=ctx.generation)) and make the resource "
                    "reject tokens older than the newest it has seen; a check done just before the call is not enough.",
}


def _x(text: str) -> str:
    return _XML_BAD.sub("?", clean(text))


def to_junit(report: ConformanceReport, name: str = "crashresume") -> str:
    from . import __author__, __version__
    by_scenario: Dict[str, List[Any]] = {}
    for f in report.findings:
        by_scenario.setdefault(f.scenario, []).append(f)
    scenarios = list(report.scenarios) or sorted(by_scenario)
    suite = ET.Element("testsuite", name=_x(name), tests=str(len(scenarios)),
                       failures=str(len(by_scenario)), errors="0", skipped="0")
    props = ET.SubElement(suite, "properties")
    ET.SubElement(props, "property", name="tool", value=f"crashresume {__version__} by {__author__}")
    for w in report.warnings:
        ET.SubElement(props, "property", name="warning", value=_x(w))
    for sid in scenarios:
        case = ET.SubElement(suite, "testcase", classname=_x(name), name=_x(sid), time="0")
        fs = by_scenario.get(sid, [])
        if fs:
            fail = ET.SubElement(case, "failure", type=_x(fs[0].kind), message=_x(f"{fs[0].kind}: {fs[0].detail}"))
            fail.text = _x("\n".join(f"[{f.kind}] {f.detail}" for f in fs))
    ET.indent(suite)
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(suite, encoding="unicode") + "\n"


def to_sarif(report: ConformanceReport, artifact_uri: str = "crashresume") -> str:
    from . import __author__, __version__
    kinds = sorted({f.kind for f in report.findings})
    rules = [{"id": f"crashresume/{k}", "name": k, "shortDescription": {"text": RULES.get(k, k)},
              "help": {"text": FIXES.get(k, RULES.get(k, k))}} for k in kinds]
    results = []
    for f in report.findings:
        results.append({
            "ruleId": f"crashresume/{f.kind}",
            "level": "error",
            "message": {"text": _x(f"{f.detail} (scenario {f.scenario}, {where(f.when, f.point, f.crash_after)})")},
            "locations": [{"physicalLocation": {"artifactLocation": {"uri": artifact_uri}, "region": {"startLine": 1}}}],
            "partialFingerprints": {"scenario": _x(f"{f.kind}:{f.scenario}")},
        })
    doc = {
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "version": "2.1.0",
        "runs": [{
            "tool": {"driver": {"name": "crashresume", "version": __version__, "organization": __author__,
                                "informationUri": "https://github.com/calasdan-sketch/crashresume", "rules": rules}},
            "results": results,
        }],
    }
    return json.dumps(doc, indent=2, sort_keys=True) + "\n"
