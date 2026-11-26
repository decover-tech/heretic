"""Tenant scoping: queries on org-owned models must filter by org_id.

This is a system-specific rule of the kind metal was built for: one person
who knows the rule writes the check, and it is enforced on everyone's code.
The rule is also checked as a MAY belief. For each model that has an
`org_id` column, count query sites that scope by org (e) against all sites
(n). Unscoped sites on a model the codebase almost always scopes rank
highest.

A query site counts as scoped when its statement mentions `org_id` (or calls a
project helper that applies an org_id filter), or when another simple statement
in the function that shares a local name with it does. That covers
`filters = [M.org_id == o]; query(M).filter(*filters)` and
`q = query(M); q = q.filter(M.org_id == o)`.
"""
import ast
from typing import List

from ..report import Finding, z_score
from . import Checker

QUERY_FUNCS = {"query", "select", "get", "delete", "update"}


def _model_of(call: ast.Call, models) -> str:
    if not call.args:
        return ""
    a = call.args[0]
    if isinstance(a, ast.Name) and a.id in models:
        return a.id
    if isinstance(a, ast.Attribute) and isinstance(a.value, ast.Name) and a.value.id in models:
        return a.value.id
    return ""


def _mentions_scope(node: ast.AST, scopers) -> bool:
    for n in ast.walk(node):
        if isinstance(n, ast.Attribute) and n.attr == "org_id":
            return True
        if isinstance(n, ast.keyword) and n.arg == "org_id":
            return True
        if isinstance(n, ast.Name) and n.id == "org_id":
            return True
        if isinstance(n, ast.Call):
            f = n.func
            nm = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
            if nm in scopers:
                return True
    return False


MUTATORS = {"append", "extend", "add", "update", "insert", "setdefault"}
IGNORED_NAMES = {"self", "cls", "session", "db"}


def _names(e):
    return {x.id for x in ast.walk(e) if isinstance(x, ast.Name)} - IGNORED_NAMES


def _bound(target):
    return {x.id for x in ast.walk(target) if isinstance(x, ast.Name)}


def provenance(fnode):
    """name -> expressions it was derived from (assign, loop/comprehension iter, x.append(e), with-as)."""
    from ..project import _own_nodes

    defs = {}

    def add(names, expr):
        for n in names:
            defs.setdefault(n, []).append(expr)

    for n in _own_nodes(fnode):
        if isinstance(n, ast.Assign):
            for t in n.targets:
                add(_bound(t), n.value)
        elif isinstance(n, (ast.AnnAssign, ast.AugAssign)) and n.value is not None:
            add(_bound(n.target), n.value)
        elif isinstance(n, (ast.For, ast.AsyncFor, ast.comprehension)):
            add(_bound(n.target), n.iter)
        elif isinstance(n, ast.NamedExpr):
            add(_bound(n.target), n.value)
        elif isinstance(n, (ast.With, ast.AsyncWith)):
            for it in n.items:
                if it.optional_vars is not None:
                    add(_bound(it.optional_vars), it.context_expr)
        elif isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr in MUTATORS \
                and isinstance(n.func.value, ast.Name):
            for a in n.args:
                add({n.func.value.id}, a)
    return defs


def _derived_from_scope(names, defs, scopers, depth=4, seen=None):
    seen = set() if seen is None else seen
    if depth == 0:
        return False
    for nm in names:
        if nm in seen:
            continue
        seen.add(nm)
        for expr in defs.get(nm, ()):
            if _mentions_scope(expr, scopers):
                return True
            if _derived_from_scope(_names(expr), defs, scopers, depth - 1, seen):
                return True
    return False


class OrgScope(Checker):
    name = "org-scope"
    kind = "MAY"
    description = "Query on an org_id-bearing model that never filters by org_id."

    def begin_file(self, path, tree, module, idx):
        self.idx = idx
        self.sites = []

    def function(self, fx):
        models = self.idx.org_models
        if not models:
            return
        stmts = [s for s in fx.own_nodes() if isinstance(s, ast.stmt)]
        defs = None
        a = fx.fnode.args
        params = {x.arg for x in a.args + a.kwonlyargs + getattr(a, "posonlyargs", [])}
        has_param = "org_id" in params
        for n in fx.own_nodes():
            if not isinstance(n, ast.Call):
                continue
            f = n.func
            nm = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
            if nm not in QUERY_FUNCS:
                continue
            model = _model_of(n, models)
            if not model:
                continue
            # enclosing statement
            s = n
            while s is not None and not isinstance(s, ast.stmt):
                s = fx.parents.get(id(s))
            if s is None:
                continue
            scoped = _mentions_scope(s, self.idx.scopers)
            if not scoped:
                # follow local names both ways: `filters = [M.org_id == o]; q.filter(*filters)`
                # and `q = query(M); q = q.filter(M.org_id == o)`
                names = {x.id for x in ast.walk(s) if isinstance(x, ast.Name)} - {"self", "cls", "session"}
                if isinstance(s, (ast.Assign, ast.AnnAssign)):
                    tgts = s.targets if isinstance(s, ast.Assign) else [s.target]
                    names |= {t.id for t in tgts if isinstance(t, ast.Name)}
                names.discard(model)
                for other in stmts:
                    if other is s or isinstance(other, (ast.If, ast.For, ast.While, ast.With, ast.Try)):
                        continue
                    uses = any(isinstance(x, ast.Name) and x.id in names for x in ast.walk(other))
                    if uses and _mentions_scope(other, self.idx.scopers):
                        scoped = True
                        break
            if not scoped:
                # provenance: the ids in this query came out of an org-scoped query
                defs = provenance(fx.fnode) if defs is None else defs
                scoped = _derived_from_scope(_names(s), defs, self.idx.scopers)
            self.sites.append((model, fx.path, n.lineno, fx.qual, scoped, has_param))

    def end_file(self):
        return self.sites

    @classmethod
    def aggregate(cls, partials, idx) -> List[Finding]:
        by = {}
        for p in partials:
            for model, path, line, qual, scoped, has_param in p or ():
                by.setdefault(model, []).append((path, line, qual, scoped, has_param))
        out = []
        for model, sites in by.items():
            n = len(sites)
            e = sum(1 for s in sites if s[3])
            if e == n:
                continue
            z = z_score(n, e)
            examples = [f"{p}:{l}" for p, l, _, sc, _ in sites if sc][:3]
            for path, line, qual, scoped, has_param in sites:
                if scoped:
                    continue
                extra = (" The function takes an `org_id` parameter but never applies it to this query."
                         if has_param else "")
                out.append(Finding(
                    cls.name, cls.kind, path, line, qual,
                    f"Query on `{model}` (which has an org_id column) is not filtered by org_id; "
                    f"{e} of {n} queries on `{model}` are.{extra}",
                    score=z + (3.0 if has_param else 0.0),
                    evidence={"scoped": e, "sites": n, "z": round(z, 2), "org_id_param_unused": has_param,
                              "examples": examples}))
        return out
