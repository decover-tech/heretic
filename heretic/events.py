"""Turn a CFG node into the ordered stream of events checkers match on.

This is the "alphabet" of a metal extension.  Events:

    Deref(key, node)             attribute load/store, subscript, iteration on `key`
    Call(name, recv, node, awaited, args)
    Assign(key, value, node)     key rebound (kills state: xgcc `killvars`)
    Escape(key, node)            key passed / returned / stored elsewhere
    Cond(key, kind, val, node)   on a branch edge: kind 'none' (key is None) or 'truthy'

Keys are dotted access paths: `user`, `self.session`, `a.b.c`.  Subscripts
and calls are not keyed (no alias analysis, as in xgcc).
"""
from __future__ import annotations

import ast
from dataclasses import dataclass, field
from typing import List, Optional, Set, Tuple


def key_of(e: ast.AST) -> Optional[str]:
    if isinstance(e, ast.Name):
        return e.id
    if isinstance(e, ast.Attribute):
        k = key_of(e.value)
        return f"{k}.{e.attr}" if k else None
    return None


def callee_name(call: ast.Call) -> Optional[str]:
    f = call.func
    if isinstance(f, ast.Attribute):
        return f.attr
    if isinstance(f, ast.Name):
        return f.id
    return None


@dataclass(frozen=True)
class Ev:
    kind: str  # deref | call | assign | escape | cond
    key: Optional[str]
    node: ast.AST = field(compare=False, hash=False)
    name: Optional[str] = None  # call: callee name; cond: 'none'|'truthy'
    val: Optional[bool] = None  # cond outcome / call awaited flag
    value: Optional[ast.AST] = field(default=None, compare=False, hash=False)

    @property
    def line(self) -> int:
        return getattr(self.node, "lineno", 0)


# ---------------------------------------------------------------- conditions
def classify_test(test: ast.AST) -> Optional[Tuple[str, str, bool]]:
    """Map an atomic branch test to (key, kind, polarity).

    `x is None` -> (x, 'none', True); `x is not None` -> (x, 'none', False);
    `x` / `isinstance(x, T)` -> (x, 'truthy', True).
    """
    if isinstance(test, ast.Compare) and len(test.ops) == 1:
        l, r, op = test.left, test.comparators[0], test.ops[0]
        if isinstance(r, ast.Constant) and r.value is None:
            k = key_of(l)
        elif isinstance(l, ast.Constant) and l.value is None:
            k = key_of(r)
        else:
            return None
        if k is None:
            return None
        if isinstance(op, (ast.Is, ast.Eq)):
            return (k, "none", True)
        if isinstance(op, (ast.IsNot, ast.NotEq)):
            return (k, "none", False)
        return None
    if isinstance(test, ast.NamedExpr):
        k = key_of(test.target)
        return (k, "truthy", True) if k else None
    k = key_of(test)
    if k:
        return (k, "truthy", True)
    if isinstance(test, ast.Call) and callee_name(test) == "isinstance" and test.args:
        k = key_of(test.args[0])
        return (k, "truthy", True) if k else None
    return None


def cond_events(test: ast.AST, taken: bool) -> List[Ev]:
    c = classify_test(test)
    if not c:
        return []
    k, kind, pol = c
    # outcome of the predicate "k is None" (kind none) or "k is truthy"
    return [Ev("cond", k, test, name=kind, val=(taken == pol))]


def canon_test(test: ast.AST) -> Optional[Tuple[str, bool]]:
    """Canonical (text, polarity) for false-path pruning."""
    c = classify_test(test)
    if c:
        k, kind, pol = c
        return (f"{k} {kind}", pol)
    try:
        return (ast.unparse(test), True)
    except Exception:  # pragma: no cover
        return None


def names_in(e: ast.AST) -> Set[str]:
    out = set()
    for n in ast.walk(e):
        k = key_of(n) if isinstance(n, (ast.Name, ast.Attribute)) else None
        if k:
            out.add(k)
    return out


# ---------------------------------------------------------------- expressions
class _Walker:
    def __init__(self):
        self.out: List[Ev] = []

    def expr(self, e: Optional[ast.AST], guarded: frozenset = frozenset(), awaited: bool = False):
        if e is None:
            return
        t = type(e)
        if t is ast.Attribute:
            self.expr(e.value, guarded)
            k = key_of(e.value)
            if k and k not in guarded:
                self.out.append(Ev("deref", k, e))
            return
        if t is ast.Subscript:
            self.expr(e.value, guarded)
            self.expr(e.slice, guarded)
            k = key_of(e.value)
            if k and k not in guarded:
                self.out.append(Ev("deref", k, e))
            return
        if t is ast.Call:
            self.expr(e.func, guarded)
            for a in e.args:
                self.expr(a, guarded)
                self._escape(a)
            for kw in e.keywords:
                self.expr(kw.value, guarded)
                self._escape(kw.value)
            f = e.func
            recv = key_of(f.value) if isinstance(f, ast.Attribute) else None
            self.out.append(Ev("call", recv, e, name=callee_name(e), val=awaited))
            return
        if t is ast.Await:
            self.expr(e.value, guarded, awaited=True)
            return
        if t is ast.BoolOp:
            g = set(guarded)
            for v in e.values:
                self.expr(v, frozenset(g))
                g |= names_in(v)
            return
        if t is ast.IfExp:
            self.expr(e.test, guarded)
            g = guarded | names_in(e.test)
            self.expr(e.body, g)
            self.expr(e.orelse, g)
            return
        if t is ast.NamedExpr:
            self.expr(e.value, guarded)
            k = key_of(e.target)
            if k:
                self.out.append(Ev("assign", k, e, value=e.value))
            return
        if t in (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp):
            # comprehension bodies have their own scope; only the first iterable
            # is evaluated in ours.
            it = e.generators[0].iter
            self.expr(it, guarded)
            k = key_of(it)
            if k and k not in guarded:
                self.out.append(Ev("deref", k, it))
            return
        if t is ast.Lambda:
            return
        if t in (ast.List, ast.Tuple, ast.Set):
            for x in e.elts:
                self.expr(x, guarded)
                self._escape(x)
            return
        if t is ast.Dict:
            for x in list(e.keys) + list(e.values):
                if x is not None:
                    self.expr(x, guarded)
                    self._escape(x)
            return
        for child in ast.iter_child_nodes(e):
            if isinstance(child, ast.expr):
                self.expr(child, guarded)

    def _escape(self, e):
        k = key_of(e)
        if k:
            self.out.append(Ev("escape", k, e))

    def target(self, t: ast.AST, value: Optional[ast.AST], node: ast.AST):
        if isinstance(t, (ast.Tuple, ast.List)):
            for x in t.elts:
                self.target(x, None, node)
            return
        if isinstance(t, ast.Starred):
            self.target(t.value, None, node)
            return
        if isinstance(t, (ast.Attribute, ast.Subscript)):
            self.expr(t.value)
            k = key_of(t.value)
            if k:
                self.out.append(Ev("deref", k, t))
            if isinstance(t, ast.Subscript):
                self.expr(t.slice)
        k = key_of(t)
        if k:
            self.out.append(Ev("assign", k, node, value=value))


def node_events(n) -> List[Ev]:
    """Events produced by executing CFG node `n` (before any edge condition)."""
    w = _Walker()
    s = n.ast
    kind = n.kind
    if kind in ("entry", "exit", "try", "loophead", "withexit"):
        return []
    if kind == "branch":
        w.expr(s)
        return w.out
    if kind == "for":
        w.expr(s.iter)
        k = key_of(s.iter)
        if k:
            w.out.append(Ev("deref", k, s.iter))
        return w.out
    if kind == "forbind":
        w.target(s.target, None, s)
        return w.out
    if kind == "with":
        for item in s.items:
            w.expr(item.context_expr)
            if item.optional_vars is not None:
                w.target(item.optional_vars, item.context_expr, s)
        return w.out
    if kind == "except":
        if s.name:
            w.out.append(Ev("assign", s.name, s))
        return w.out
    if kind == "def":
        w.out.append(Ev("assign", s.name, s))
        return w.out
    t = type(s)
    if t is ast.Assign:
        w.expr(s.value)
        if len(s.targets) == 1 and key_of(s.value) and key_of(s.targets[0]) is None:
            pass
        for tg in s.targets:
            w.target(tg, s.value, s)
        bare = key_of(s.value)
        if bare:
            w.out.append(Ev("escape", bare, s))
        return w.out
    if t is ast.AnnAssign:
        if s.value is not None:
            w.expr(s.value)
            w.target(s.target, s.value, s)
        return w.out
    if t is ast.AugAssign:
        w.expr(s.value)
        k = key_of(s.target)
        if isinstance(s.target, (ast.Attribute, ast.Subscript)):
            w.expr(s.target.value)
            kv = key_of(s.target.value)
            if kv:
                w.out.append(Ev("deref", kv, s.target))
        if k:
            w.out.append(Ev("assign", k, s, value=None))
        return w.out
    if t is ast.Return:
        w.expr(s.value)
        if s.value is not None:
            w._escape(s.value)
        return w.out
    if t is ast.Delete:
        for tg in s.targets:
            if isinstance(tg, (ast.Attribute, ast.Subscript)):
                w.expr(tg.value)
            k = key_of(tg)
            if k:
                w.out.append(Ev("assign", k, s))
        return w.out
    if t in (ast.Import, ast.ImportFrom):
        for a in s.names:
            w.out.append(Ev("assign", (a.asname or a.name).split(".")[0], s))
        return w.out
    if t is ast.Raise:
        w.expr(s.exc)
        w.expr(s.cause)
        return w.out
    if t in (ast.Global, ast.Nonlocal, ast.Pass, ast.Break, ast.Continue):
        return []
    if t is ast.Expr:
        v = s.value
        if isinstance(v, (ast.Yield, ast.YieldFrom)) and v.value is not None:
            w.expr(v.value)
            w._escape(v.value)
            return w.out
        w.expr(v)
        return w.out
    for child in ast.iter_child_nodes(s):
        if isinstance(child, ast.expr):
            w.expr(child)
    return w.out
