"""Inferring "can return None" functions (SOSP'01 section 6, "routines that can fail").

For every call `v = f(...)`, the path engine records whether `v` is
None-tested before it is first dereferenced (checked), or dereferenced with
no test (unchecked). `f(...).attr` counts as unchecked.

Across the codebase, each callee `f` gets n = sites and e = checked sites,
and the unchecked sites are ranked by z(n, e). If 9 of 10 callers check,
the tenth caller is probably the bug.

Interprocedural summaries prune noise. If `f` is defined in the project and
no definition of it can return None, the checks are just defensive coding, and f is
skipped. If `f` provably returns None somewhere, the finding says where.
"""
import ast
from typing import List

from ..metal import SM, AssignCall, Deref, NullTest, Rule
from ..project import KNOWN_OPTIONAL
from ..report import Finding, z_score
from . import Checker

NEVER_NONE = {
    "str", "int", "float", "bool", "len", "list", "dict", "set", "tuple", "sorted", "range", "enumerate", "zip",
    "isinstance", "format", "join", "split", "strip", "lower", "upper", "replace", "items", "keys", "values",
    "copy", "deepcopy", "uuid4", "now", "utcnow", "time", "dumps", "loads", "encode", "decode", "Session",
    "sessionmaker", "query", "filter", "filter_by", "where", "order_by", "limit", "offset", "select", "all",
    "scalars", "execute", "join", "outerjoin", "options", "group_by", "count", "getLogger", "defaultdict",
    "model_dump", "dict", "json", "to_dict", "result", "fetchall", "max", "min", "sum", "abs", "round",
    "re", "compile", "Path", "open", "super", "type", "map", "filter", "iter", "reversed", "any", "bytes",
}


DEFAULT_ARG_POS = {"get": 1, "getattr": 2, "next": 1, "pop": 1, "getenv": 1}


def site_callee(call):
    """Callee name if this call can plausibly return None, else None."""
    if not isinstance(call, ast.Call):
        return None
    f = call.func
    name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
    if not name or name in NEVER_NONE or name[:1].isupper():
        return None
    pos = DEFAULT_ARG_POS.get(name)
    if pos is not None and (len(call.args) > pos or any(k.arg == "default" for k in call.keywords)):
        return None
    return name


class _AssignSite(AssignCall):
    def __init__(self):
        super().__init__(lambda n: True)

    def match(self, ev):
        m = super().match(ev)
        if not m:
            return None
        v = ev.value.value if isinstance(ev.value, ast.Await) else ev.value
        return m if site_callee(v) else None


def _mark(outcome):
    def act(ctx, var, data, ev):
        line, callee = data
        if not callee:
            return
        ctx.store.setdefault("sites", {}).setdefault((ctx.path, line, callee, ctx.func), set()).add(
            (outcome, ev.line))
    return act


class RetSM(SM):
    name = "ret-check"
    kind = "MAY"
    rules = [
        Rule("start", _AssignSite(), dst="fresh"),
        Rule("fresh", NullTest(), dst="stop", action=_mark("c")),
        Rule("fresh", Deref(), dst="stop", action=_mark("u")),
    ]


class RetCheck(Checker):
    name = "ret-check"
    kind = "MAY"
    description = "Return value used without the None check that most other callers perform."

    def begin_file(self, path, tree, module, idx):
        self.store = {}
        self.direct = []

    def function(self, fx):
        fx.run_sm(RetSM(), self.store)
        for n in fx.own_nodes():
            if isinstance(n, (ast.Attribute, ast.Subscript)):
                v = n.value.value if isinstance(n.value, ast.Await) else n.value
                name = site_callee(v)
                if name:
                    self.direct.append((fx.path, n.lineno, name, fx.qual))

    def end_file(self):
        sites = {k: {o for o, _ in v} for k, v in self.store.get("sites", {}).items()}
        out = [(p, l, c, f, "u" if "u" in o else "c") for (p, l, c, f), o in sites.items()]
        out += [(p, l, c, f, "u") for (p, l, c, f) in self.direct]
        return out

    @classmethod
    def aggregate(cls, partials, idx) -> List[Finding]:
        by = {}
        for part in partials:
            for p, l, c, f, o in part or ():
                by.setdefault(c, []).append((p, l, f, o))
        out = []
        for callee, sites in by.items():
            n = len(sites)
            e = sum(1 for s in sites if s[3] == "c")
            if e < 3 or e == n or e / n < 0.6:
                continue
            defs = idx.defs(callee)
            if defs and not any(d.may_none for d in defs):
                continue  # implementation never returns None: the checks are defensive
            proof = ""
            for d in defs:
                if d.explicit_none:
                    proof = f" `{d.qual}` returns None at {d.path}:{d.explicit_none[0]}."
                    break
                if d.ann == "optional":
                    proof = f" `{d.qual}` is annotated `{d.ann_text}`."
                    break
            if not defs and callee in KNOWN_OPTIONAL:
                proof = f" `{callee}()` returns None by library contract."
            z = z_score(n, e)
            bonus = 1.0 if proof else 0.0
            examples = [f"{p}:{l}" for p, l, _, o in sites if o == "c"][:3]
            for p, l, f, o in sites:
                if o != "u":
                    continue
                out.append(Finding(cls.name, cls.kind, p, l, f,
                                   f"Result of `{callee}()` is used without a None check, but {e} of {n} "
                                   f"call sites check it.{proof}",
                                   score=z + bonus, evidence={"checked": e, "sites": n, "z": round(z, 2),
                                                              "examples": examples}))
        return out
