"""Two-phase driver.

phase 1 (parallel):  summarize every file -> Index (defs, summaries, models)
phase 2 (parallel):  run each checker over each function; MUST findings come
                     back directly, MAY evidence comes back as partials
phase 3 (serial):    aggregate partials, z-rank, fingerprint, suppress
"""
from __future__ import annotations

import ast
import multiprocessing as mp
import os
import sys
from typing import Dict, List, Optional, Sequence, Tuple

from .project import Index, build_index, iter_py_files, parse, summarize_file
from .report import Finding, load_suppressions

_IDX: Optional[Index] = None
_ROOT = ""
_CHECKERS: Sequence[str] = ()


def _functions_with_class(tree):
    def walk(node, prefix, cls):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                q = f"{prefix}{child.name}"
                yield q, child, cls
                yield from walk(child, q + ".", None)
            elif isinstance(child, ast.ClassDef):
                yield from walk(child, f"{prefix}{child.name}.", child)
            else:
                yield from walk(child, prefix, cls)

    yield from walk(tree, "", None)


def _phase1(rel: str) -> dict:
    return summarize_file(_ROOT, rel)


def _phase2(rel: str) -> Tuple[List[Finding], Dict[str, object], List[str]]:
    from .checkers import FuncCtx, ModuleInfo, all_checkers

    tree = parse(_ROOT, rel)
    if tree is None:
        return [], {}, []
    registry = all_checkers()
    checkers = [registry[n]() for n in _CHECKERS]
    module = ModuleInfo(tree)
    findings: List[Finding] = []
    errors: List[str] = []
    for c in checkers:
        c.begin_file(rel, tree, module, _IDX)
    for qual, fnode, cls in _functions_with_class(tree):
        for c in checkers:
            fx = FuncCtx(rel, qual, fnode, cls, module, _IDX, findings.append)
            try:
                c.function(fx)
            except RecursionError:
                errors.append(f"{rel}:{fnode.lineno} {c.name}: recursion limit")
            except Exception as e:  # a checker bug must not kill the run
                errors.append(f"{rel}:{fnode.lineno} {c.name}: {type(e).__name__}: {e}")
    partials = {c.name: c.end_file() for c in checkers}
    return findings, partials, errors


def _init(root, idx, checkers):
    global _ROOT, _IDX, _CHECKERS
    _ROOT, _IDX, _CHECKERS = root, idx, checkers
    sys.setrecursionlimit(10000)


def analyze(root: str, checkers: Sequence[str], exclude: List[str], jobs: int = 0,
            ignore_file: Optional[str] = None, progress=None) -> Tuple[List[Finding], dict]:
    from .checkers import all_checkers

    root = os.path.abspath(root)
    files = iter_py_files(root, exclude)
    jobs = jobs or max(1, (os.cpu_count() or 2) - 1)
    ctx = mp.get_context("fork") if sys.platform != "win32" else mp.get_context("spawn")
    say = progress or (lambda m: None)

    say(f"phase 1: indexing {len(files)} files")
    _init(root, None, checkers)
    if jobs > 1:
        with ctx.Pool(jobs, initializer=_init, initargs=(root, None, checkers)) as pool:
            summaries = pool.map(_phase1, files, chunksize=16)
    else:
        summaries = [_phase1(f) for f in files]
    idx = build_index(summaries)
    unparsable = [s["path"] for s in summaries if not s.get("ok")]

    say(f"phase 2: checking ({len(idx.funcs)} distinct function names, {len(idx.org_models)} org-scoped models)")
    _init(root, idx, checkers)
    if jobs > 1:
        with ctx.Pool(jobs, initializer=_init, initargs=(root, idx, checkers)) as pool:
            results = pool.map(_phase2, idx.files, chunksize=8)
    else:
        results = [_phase2(f) for f in idx.files]

    say("phase 3: aggregating beliefs")
    registry = all_checkers()
    findings: List[Finding] = []
    errors: List[str] = []
    partials: Dict[str, list] = {n: [] for n in checkers}
    for fs, parts, errs in results:
        findings.extend(fs)
        errors.extend(errs)
        for n, p in parts.items():
            partials[n].append(p)
    for n in checkers:
        findings.extend(registry[n].aggregate(partials[n], idx))

    suppressed = load_suppressions(ignore_file)
    final, seen, n_suppressed = [], set(), 0
    for f in findings:
        f.finalize()
        if (f.fingerprint, f.line) in seen:
            continue  # same report reached by two paths
        seen.add((f.fingerprint, f.line))
        if f.fingerprint in suppressed:
            n_suppressed += 1
            continue
        final.append(f)
    stats = {"files": len(files), "unparsable": unparsable, "checker_errors": errors,
             "suppressed": n_suppressed}
    return final, stats
