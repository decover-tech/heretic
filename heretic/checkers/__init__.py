"""Checker plumbing.

A checker sees each function once (in a worker process), may emit findings
directly (MUST contradictions), and may return a picklable `partial` per file.
Partials from every file are combined in `aggregate`, which is where MAY
beliefs are counted and z-ranked across the whole codebase.
"""
from __future__ import annotations

import ast
from typing import Callable, Dict, List, Optional, Type

from ..cfg import CFG
from ..metal import SM, Ctx, run
from ..project import Index
from ..report import Finding, path_rank


class FuncCtx:
    def __init__(self, path: str, qual: str, fnode, cls: Optional[ast.ClassDef], module: "ModuleInfo",
                 idx: Index, emit: Callable[[Finding], None]):
        self.path, self.qual, self.fnode, self.cls = path, qual, fnode, cls
        self.module, self.idx, self.emit = module, idx, emit
        self._cfg = None
        self._parents = None

    @property
    def cfg(self) -> CFG:
        if self._cfg is None:
            self._cfg = CFG(self.fnode, noreturn=self.idx.noreturn if self.idx else frozenset())
        return self._cfg

    def own_nodes(self):
        from ..project import _own_nodes

        return _own_nodes(self.fnode)

    @property
    def parents(self) -> Dict[int, ast.AST]:
        if self._parents is None:
            p = {}
            for n in self.own_nodes():
                for c in ast.iter_child_nodes(n):
                    p[id(c)] = n
            for s in self.fnode.body:
                p[id(s)] = self.fnode
            self._parents = p
        return self._parents

    def run_sm(self, sm: SM, store: dict) -> None:
        seen = set()

        def report(sm_, line, msg, trigger, trace, var):
            k = (sm_.name, line, msg)
            if k in seen:
                return
            seen.add(k)
            score, lines = path_rank(trace, trigger, line)
            self.emit(Finding(sm_.name, sm_.kind, self.path, line, self.qual, msg, score=score, trace=lines))

        ctx = Ctx(self.path, self.qual, self.fnode, report, store)
        run(sm, self.cfg, ctx)


class ModuleInfo:
    """Per-file facts: module-level defs, imports, class method tables."""

    def __init__(self, tree: ast.AST):
        self.defs: Dict[str, bool] = {}  # name -> is_async (module level)
        self.imports: Dict[str, str] = {}  # local name -> dotted origin
        self.classes: Dict[str, Dict[str, bool]] = {}
        for n in tree.body:
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self.defs[n.name] = isinstance(n, ast.AsyncFunctionDef)
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                for a in n.names:
                    self.imports[a.asname or a.name.split(".")[0]] = a.name if a.asname else a.name.split(".")[0]
            elif isinstance(n, ast.ImportFrom):
                for a in n.names:
                    self.imports[a.asname or a.name] = f"{n.module or ''}.{a.name}"
            elif isinstance(n, ast.ClassDef):
                self.classes[n.name] = {
                    b.name: isinstance(b, ast.AsyncFunctionDef)
                    for b in n.body if isinstance(b, (ast.FunctionDef, ast.AsyncFunctionDef))
                }


class Checker:
    name = "checker"
    kind = "MUST"
    description = ""

    def begin_file(self, path: str, tree: ast.AST, module: ModuleInfo, idx: Index) -> None:
        self.path = path

    def function(self, fx: FuncCtx) -> None:
        pass

    def end_file(self):
        return None

    @classmethod
    def aggregate(cls, partials: list, idx: Index) -> List[Finding]:
        return []


def all_checkers() -> Dict[str, Type[Checker]]:
    from . import (blocking_async, null_belief, org_scope, pairs, resource, ret_check, return_annotation,
                   unawaited)

    cs = [null_belief.NullBelief, unawaited.Unawaited, return_annotation.ReturnAnnotation, resource.Resource,
          blocking_async.BlockingInAsync, ret_check.RetCheck, pairs.Pairs, org_scope.OrgScope]
    return {c.name: c for c in cs}
