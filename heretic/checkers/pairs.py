"""Inferred a() -> b() pairing (SOSP'01 section 7; RacerX lock-pair inference).

When `a()` is followed by `b()` in almost every function that calls `a()`,
the codebase believes `a` must be paired with `b`. A function that calls
`a` without `b` deviates from that belief.

Pairs are mined from per-function call sequences, then filtered:
  support      at least MIN_SUPPORT functions obey the pairing,
  confidence   P(b after a | a) is high,
  lift         b is not simply called everywhere (logger.info and similar),
and ranked by z(n, e). Names that look like an open/close pair
(acquire/release, start/stop, begin/commit, ...) get a latent-specification
bonus, the way the papers used names containing "lock" and "free".
"""
import ast
import re
from typing import List

from ..report import Finding, z_score
from . import Checker

MIN_SUPPORT = 5
MIN_CONF = 0.8
MIN_LIFT = 3.0
STOP = {
    "append", "extend", "get", "info", "debug", "warning", "error", "exception", "critical", "format", "join",
    "len", "isinstance", "str", "int", "float", "bool", "dict", "list", "set", "tuple", "items", "keys", "values",
    "update", "add", "print", "super", "range", "enumerate", "zip", "sorted", "min", "max", "sum", "any", "all",
    "getattr", "setattr", "hasattr", "strip", "split", "lower", "upper", "replace", "startswith", "endswith",
    "encode", "decode", "dumps", "loads", "now", "utcnow", "uuid4", "isoformat", "filter", "first", "query",
    "order_by", "limit", "where", "select", "execute", "scalars", "scalar", "one", "one_or_none", "count",
    "model_dump", "model_validate", "copy", "pop", "type", "id", "map", "next", "iter", "open", "time",
    "assert_called_once", "assert_called_once_with", "assert_called_with", "assert_not_called", "MagicMock",
    "patch", "Mock", "AsyncMock", "HTTPException", "ValueError", "TypeError", "RuntimeError", "KeyError",
    "Exception", "Field", "Depends", "Query", "Column", "String", "Integer", "isoformat", "fromisoformat",
    "info_with_context", "error_with_context", "warning_with_context", "debug_with_context", "set_attribute",
    "filter_by", "options", "join", "all", "where", "in_", "desc", "asc", "and_", "or_", "func", "label",
}
OPENERS = r"(acquire|lock|open|start|begin|enter|connect|subscribe|register|push|inc|incr|setup|create_session|mark_.*start)"
CLOSERS = r"(release|unlock|close|stop|end|finish|commit|rollback|exit|disconnect|unsubscribe|unregister|pop|dec|decr|teardown|cleanup)"


def _looks_paired(a: str, b: str) -> bool:
    return bool(re.search(OPENERS, a)) and bool(re.search(CLOSERS, b))


class Pairs(Checker):
    name = "pairing"
    kind = "MAY"
    description = "Function calls a() without the b() that nearly every other a() caller pairs it with."

    def begin_file(self, path, tree, module, idx):
        self.seqs = []

    def function(self, fx):
        calls = []
        for n in fx.own_nodes():
            if isinstance(n, ast.Call):
                f = n.func
                name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
                if name and name not in STOP and not name.startswith("assert") and not name.startswith("_"):
                    calls.append((n.lineno, n.col_offset, name))
        if not calls:
            return
        calls.sort()
        self.seqs.append((fx.path, fx.qual, [(l, nm) for l, _, nm in calls]))

    def end_file(self):
        return self.seqs

    @classmethod
    def aggregate(cls, partials, idx) -> List[Finding]:
        funcs = [s for p in partials for s in (p or ())]
        F = len(funcs)
        if F == 0:
            return []
        fa, fab, first_line = {}, {}, {}
        for i, (_, _, seq) in enumerate(funcs):
            names = [n for _, n in seq]
            seen_first = {}
            for pos, n in enumerate(names):
                seen_first.setdefault(n, pos)
            for a, pa in seen_first.items():
                fa[a] = fa.get(a, 0) + 1
                after = set(names[pa + 1:])
                for b in after:
                    if b != a:
                        fab[(a, b)] = fab.get((a, b), 0) + 1
        out = []
        for (a, b), e in fab.items():
            if e < MIN_SUPPORT:
                continue
            n = fa[a]
            conf = e / n
            if conf < MIN_CONF or e == n:
                continue
            lift = conf / (fa.get(b, 0) / F)
            if lift < MIN_LIFT:
                continue
            z = z_score(n, e)
            hint = _looks_paired(a, b)
            score = z + (2.0 if hint else 0.0)
            for path, qual, seq in funcs:
                names = [nm for _, nm in seq]
                if a in names and b not in names:
                    line = next(l for l, nm in seq if nm == a)
                    out.append(Finding(
                        cls.name, cls.kind, path, line, qual,
                        f"`{a}()` is called here but never followed by `{b}()`; {e} of {n} functions that "
                        f"call `{a}()` follow it with `{b}()`.",
                        score=score, evidence={"obeyed": e, "callers": n, "z": round(z, 2),
                                               "lift": round(lift, 1), "name_hint": hint}))
        return out
