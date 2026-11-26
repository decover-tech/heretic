"""Findings, ranking, stable fingerprints, suppression.

Ranking follows the papers:
  * MAY findings: z(n, e) = (e/n - p0) / sqrt(p0 (1 - p0) / n), p0 = 0.9
    (Engler et al., "Bugs as Deviant Behavior", SOSP'01).  e = sites that obey
    the inferred rule, n = all sites.  High z: the codebase strongly believes
    the rule, so the few deviations are probably bugs.
  * MUST findings: generic ranking (Hallem et al., PLDI'02) -- fewer
    conditionals and shorter distance between the two contradicting events
    rank higher; each conditional costs as much as 10 lines.
  * Fingerprints ignore line numbers so triage survives edits (Coverity's
    "A Few Billion Lines of Code Later" lesson on churn).
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
from dataclasses import asdict, dataclass, field
from typing import List, Optional, Tuple

P0 = 0.9


def z_score(n: int, e: int, p0: float = P0) -> float:
    if n <= 0:
        return float("-inf")
    return (e / n - p0) / math.sqrt(p0 * (1 - p0) / n)


@dataclass
class Finding:
    checker: str
    kind: str  # MUST | MAY | RULE
    path: str
    line: int
    func: str
    message: str
    score: float = 0.0  # higher = more likely a real bug
    evidence: dict = field(default_factory=dict)
    trace: List[int] = field(default_factory=list)
    fingerprint: str = ""

    def finalize(self) -> "Finding":
        norm = re.sub(r"\bline \d+\b|\b\d+\b", "#", self.message)
        h = hashlib.sha1(f"{self.checker}|{self.path}|{self.func}|{norm}".encode()).hexdigest()
        self.fingerprint = h[:12]
        return self


def path_rank(trace: Tuple, trigger_line: int, err_line: int) -> Tuple[float, List[int]]:
    """Generic xgcc ranking: (score, explanatory line trace)."""
    idx = 0
    for i in range(len(trace) - 1, -1, -1):
        if trace[i][0] == trigger_line:
            idx = i
            break
    seg = trace[idx:]
    conds = sum(1 for _, b in seg if b)
    distance = abs(err_line - trigger_line)
    score = -(distance + 10 * conds)
    lines = []
    for ln, _ in seg:
        if not lines or lines[-1] != ln:
            lines.append(ln)
    return float(score), lines[-25:]


def load_suppressions(path: Optional[str]) -> set:
    if not path or not os.path.exists(path):
        return set()
    out = set()
    with open(path) as fh:
        for line in fh:
            line = line.split("#", 1)[0].strip()
            if line:
                out.add(line.split()[0])
    return out


SECTION = {
    "MUST": "MUST beliefs contradicted (code disagrees with itself; ranked by path simplicity)",
    "RULE": "Rule violations (explicit rules; ranked by confidence)",
    "MAY": "MAY beliefs violated (deviations from what most of the codebase does; ranked by z)",
}


def render_text(findings: List[Finding], root: str, top: int, show_trace: bool = True) -> str:
    out = []
    for kind in ("MUST", "RULE", "MAY"):
        fs = [f for f in findings if f.kind == kind]
        if not fs:
            continue
        fs.sort(key=lambda f: (-f.score, f.path, f.line))
        out.append(f"\n== {SECTION[kind]} : {len(fs)} ==\n")
        for f in fs[:top]:
            loc = f"{f.path}:{f.line}"
            out.append(f"[{f.checker}] {loc}  ({f.func})  #{f.fingerprint}")
            out.append(f"    {f.message}")
            if f.evidence:
                ev = ", ".join(f"{k}={v}" for k, v in f.evidence.items() if k != "examples")
                out.append(f"    evidence: {ev}")
                if f.evidence.get("examples"):
                    out.append(f"    obeyed at: {', '.join(f.evidence['examples'][:3])}")
            if show_trace and f.trace:
                out.append(f"    path: lines {' -> '.join(map(str, f.trace))}")
        if len(fs) > top:
            out.append(f"  ... {len(fs) - top} more (use --top)")
    if not out:
        return "No findings.\n"
    return "\n".join(out) + "\n"


def render_json(findings: List[Finding]) -> str:
    return json.dumps([asdict(f) for f in findings], indent=2, default=str)


def render_sarif(findings: List[Finding]) -> str:
    rules = sorted({f.checker for f in findings})
    sarif = {
        "version": "2.1.0",
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "runs": [{
            "tool": {"driver": {"name": "heretic", "rules": [{"id": r} for r in rules]}},
            "results": [{
                "ruleId": f.checker,
                "level": "error" if f.kind == "MUST" else "warning",
                "message": {"text": f.message},
                "partialFingerprints": {"heretic/v1": f.fingerprint},
                "locations": [{"physicalLocation": {
                    "artifactLocation": {"uri": f.path},
                    "region": {"startLine": max(f.line, 1)}}}],
            } for f in findings],
        }],
    }
    return json.dumps(sarif, indent=2)
