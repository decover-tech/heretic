"""metal for Python: state-machine checkers + the xgcc-style path engine.

A checker is a list of rules, exactly as in metal:

    class NullBelief(SM):
        rules = [
            Rule("start",  Deref(),  dst="derefd"),
            Rule("derefd", IsNone(), err="{v} checked against None after deref at line {t}"),
            ...
        ]

`start` rules create a per-variable instance (metal's `state decl any_pointer v`).
Other rules fire when an instance of `v` is in state `src` and the pattern
binds the same `v`.  Branch patterns take `true=`/`false=` destinations.
`End()` fires at function exit.  `"stop"` drops the instance.

Engine (xgcc, Hallem et al. PLDI'02):
  * depth-first over every CFG path,
  * block-level caching on (node, SM state) -- revisiting a node in a state
    already seen there is cut off, which also bounds loops,
  * killvars: rebinding `v` (or a prefix of it) kills its instance,
  * false-path pruning: branch facts along the path refute impossible edges,
  * kill_paths: sys.exit / os._exit / pytest.fail end the path (in cfg.py).
"""
from __future__ import annotations

import ast
from dataclasses import dataclass
from typing import Callable, Dict, FrozenSet, List, Optional, Tuple

from .cfg import CFG
from .events import Ev, canon_test, cond_events, names_in, node_events

MAX_STATES_PER_FUNCTION = 20000
MAX_TRACE = 400


# ------------------------------------------------------------------ patterns
class Pattern:
    def match(self, ev: Ev) -> Optional[Tuple[str, Optional[bool]]]:
        raise NotImplementedError


class Deref(Pattern):
    def match(self, ev):
        return (ev.key, None) if ev.kind == "deref" else None


class IsNone(Pattern):
    """`v is None` / `v is not None` / `v == None`. Outcome: v is None on this edge."""

    def match(self, ev):
        if ev.kind == "cond" and ev.name == "none":
            return (ev.key, ev.val)
        return None


class NullTest(Pattern):
    """Any null-ish test, including truthiness. Outcome: v *may* be None on this edge."""

    def match(self, ev):
        if ev.kind != "cond":
            return None
        return (ev.key, ev.val if ev.name == "none" else not ev.val)


class CallOn(Pattern):
    """Method call `v.name(...)` with name in `names`; binds v to the receiver."""

    def __init__(self, *names: str):
        self.names = set(names)

    def match(self, ev):
        if ev.kind == "call" and ev.key and ev.name in self.names:
            return (ev.key, None)
        return None


class AssignCall(Pattern):
    """`v = f(...)` or `v = await f(...)` where pred(f) holds; binds v."""

    def __init__(self, pred: Callable[[str], bool]):
        self.pred = pred

    def match(self, ev):
        if ev.kind != "assign" or ev.value is None:
            return None
        v = ev.value.value if isinstance(ev.value, ast.Await) else ev.value
        if isinstance(v, ast.Call):
            from .events import callee_name

            n = callee_name(v)
            if n and self.pred(n):
                return (ev.key, None)
        return None


class Escape(Pattern):
    def match(self, ev):
        return (ev.key, None) if ev.kind == "escape" else None


class End(Pattern):
    def match(self, ev):
        return (ev.key, None) if ev.kind == "end" else None


# ------------------------------------------------------------------ rules
@dataclass
class Rule:
    src: str
    pat: Pattern
    dst: Optional[str] = None  # None = stay
    true: Optional[str] = None
    false: Optional[str] = None
    err: Optional[str] = None
    action: Optional[Callable] = None  # action(ctx, var, data, ev)


Instance = Tuple[str, str, tuple]  # (var, state, data) ; data = (trigger_line, payload)
State = FrozenSet[Instance]


class SM:
    name = "sm"
    kind = "MUST"  # MUST (contradiction) or MAY (statistical)
    rules: List[Rule] = []
    volatile_attrs = True  # dotted vars die when their owner is called/passed
    report_on_raise_exit = False

    def __init__(self):
        self._by_src: Dict[str, List[Rule]] = {}
        for r in self.rules:
            self._by_src.setdefault(r.src, []).append(r)

    loop_local = False  # drop instances born inside a loop when the path re-enters its head

    def finish(self, ctx) -> None:  # per-function hook
        pass


# ------------------------------------------------------------------ engine
class Ctx:
    """Per-function context handed to rules and reporters."""

    def __init__(self, path: str, func: str, fnode: ast.AST, report: Callable, store: dict):
        self.path = path
        self.func = func
        self.fnode = fnode
        self.report = report  # report(sm, line, msg, trigger_line, trace, var)
        self.store = store  # checker-owned scratch (survives across functions)
        self.trace: Tuple = ()


def _kill_facts(facts, key):
    if not facts:
        return facts
    return tuple(f for f in facts if not any(n == key or n.startswith(key + ".") for n in f[2]))


def _edge_ok(facts, test, val):
    """False-path pruning. Returns (feasible, new_facts)."""
    c = canon_test(test)
    if not c:
        return True, facts
    text, pol = c
    truth = val == pol
    known = {f[0]: f[1] for f in facts}
    if text in known and known[text] != truth:
        return False, facts
    # tiny congruence: None is falsy
    if text.endswith(" truthy") and truth and known.get(text[:-7] + " none") is True:
        return False, facts
    if text.endswith(" none") and truth and known.get(text[:-5] + " truthy") is True:
        return False, facts
    if text in known:
        return True, facts
    return True, facts + ((text, truth, frozenset(names_in(test))),)


def step(sm: SM, st: State, ev: Ev, ctx: Ctx) -> State:
    out = set()
    tracked = set()
    handled = set()  # vars a rule already consumed this event: start rules must not re-fire on them
    for inst in st:
        var, state, data = inst
        fired = False
        for r in sm._by_src.get(state, ()):
            m = r.pat.match(ev)
            if not m or m[0] != var:
                continue
            fired = True
            handled.add(var)
            outcome = m[1]
            dst = r.dst
            if outcome is not None and (r.true or r.false):
                dst = r.true if outcome else r.false
            if r.err:
                ctx.report(sm, ev.line, r.err.format(v=var, t=data[0], p=data[1]), data[0], ctx.trace, var)
            if r.action:
                r.action(ctx, var, data, ev)
            if dst == "stop":
                pass
            elif dst is None:
                out.add(inst)
                tracked.add(var)
            else:
                out.add((var, dst, data))
                tracked.add(var)
            break
        if not fired:
            out.add(inst)
            tracked.add(var)
    # killvars / volatility (after rules so `v.close()` etc. still match)
    if ev.kind == "assign":
        k = ev.key
        out = {i for i in out if not (i[0] == k or i[0].startswith(k + "."))}
        tracked = {i[0] for i in out}
    elif sm.volatile_attrs and ev.kind in ("call", "escape") and ev.key:
        k = ev.key
        out = {i for i in out if not i[0].startswith(k + ".")}
        tracked = {i[0] for i in out}
    # start rules create new instances
    for r in sm._by_src.get("start", ()):
        m = r.pat.match(ev)
        if not m or m[0] in tracked or m[0] in handled:
            continue
        var, outcome = m
        dst = r.dst
        if outcome is not None and (r.true or r.false):
            dst = r.true if outcome else r.false
        if r.err:
            ctx.report(sm, ev.line, r.err.format(v=var, t=ev.line, p=None), ev.line, ctx.trace, var)
        payload = ev.name if ev.kind == "assign" else None
        if ev.kind == "assign" and ev.value is not None:
            from .events import callee_name

            v = ev.value.value if isinstance(ev.value, ast.Await) else ev.value
            payload = callee_name(v) if isinstance(v, ast.Call) else None
        data = (ev.line, payload)
        if r.action:
            r.action(ctx, var, data, ev)
        if dst and dst != "stop":
            out.add((var, dst, data))
            tracked.add(var)
    return frozenset(out)


def flag_aliases(func: ast.AST) -> Dict[str, ast.AST]:
    """Names assigned exactly once from a condition (`ok = x is not None and y`).

    Branching on the name then carries the facts of the expression, a small
    piece of xgcc's value tracking that removes "flag variable" false positives.
    """
    count: Dict[str, int] = {}
    val: Dict[str, ast.AST] = {}
    stack = list(getattr(func, "body", []))
    while stack:
        n = stack.pop()
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            continue
        targets = []
        if isinstance(n, ast.Assign):
            targets = n.targets
        elif isinstance(n, (ast.AugAssign, ast.AnnAssign)):
            targets = [n.target]
        elif isinstance(n, (ast.For, ast.AsyncFor, ast.comprehension)):
            targets = [n.target]
        elif isinstance(n, ast.NamedExpr):
            targets = [n.target]
        for t in targets:
            for x in ast.walk(t):
                if isinstance(x, ast.Name):
                    count[x.id] = count.get(x.id, 0) + 1
        if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name) \
                and isinstance(n.value, (ast.BoolOp, ast.Compare)):
            val[n.targets[0].id] = n.value
        stack.extend(ast.iter_child_nodes(n))
    return {k: v for k, v in val.items() if count.get(k) == 1}


def expand_test(test: ast.AST, val: bool, aliases) -> List[Tuple[ast.AST, bool]]:
    """Atomic (test, outcome) facts implied by taking a branch."""
    if isinstance(test, ast.Name) and test.id in aliases:
        return [(test, val)] + expand_test(aliases[test.id], val, {})
    if isinstance(test, ast.BoolOp):
        if isinstance(test.op, ast.And) and val:
            return [f for v in test.values for f in expand_test(v, True, aliases)]
        if isinstance(test.op, ast.Or) and not val:
            return [f for v in test.values for f in expand_test(v, False, aliases)]
        return []
    if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
        return expand_test(test.operand, not val, aliases)
    return [(test, val)]


def closure_rebound(func: ast.AST) -> frozenset:
    """Names that nested defs rebind via `nonlocal`: any call may change them."""
    out = set()
    for n in ast.walk(func):
        if n is not func and isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for x in ast.walk(n):
                if isinstance(x, ast.Nonlocal):
                    out.update(x.names)
    return frozenset(out)


def run(sm: SM, cfg: CFG, ctx: Ctx) -> None:
    ev_cache: Dict[int, List[Ev]] = {}
    aliases = flag_aliases(cfg.func)
    rebound = closure_rebound(cfg.func)
    seen = set()
    # stack item: (node, state, facts, trace, via_raise)
    stack = [(cfg.entry, frozenset(), (), (), False)]
    budget = MAX_STATES_PER_FUNCTION
    while stack and budget > 0:
        node, st, facts, trace, via_raise = stack.pop()
        key = (node.id, st)
        if key in seen:
            continue
        seen.add(key)
        budget -= 1
        if node.line:
            trace = (trace + ((node.line, node.kind == "branch"),))[-MAX_TRACE:]
        ctx.trace = trace
        if sm.loop_local and node.kind in ("loophead", "for") and st:
            lo, hi = node.ast.lineno, getattr(node.ast, "end_lineno", node.ast.lineno)
            st = frozenset(i for i in st if not (lo < i[2][0] <= hi))
        evs = ev_cache.get(node.id)
        if evs is None:
            evs = ev_cache[node.id] = node_events(node)
        for ev in evs:
            st = step(sm, st, ev, ctx)
            if ev.kind == "assign":
                facts = _kill_facts(facts, ev.key)
            elif ev.kind == "call" and rebound and st:
                st = frozenset(i for i in st if i[0].split(".")[0] not in rebound)
                for r in rebound:
                    facts = _kill_facts(facts, r)
        if node is cfg.exit:
            if via_raise and not sm.report_on_raise_exit:
                continue
            for var, _, _ in list(st):
                # report at the last statement on the path (the return that leaks)
                end_line = trace[-1][0] if trace else getattr(cfg.func, "end_lineno", 0)
                end = Ev("end", var, ast.Pass(lineno=end_line))
                st = step(sm, st, end, ctx)
            continue
        for succ, label in node.succs:
            nst, nfacts = st, facts
            raise_edge = isinstance(label, tuple) and label and label[0] == "raise"
            if isinstance(label, tuple) and len(label) == 2 and isinstance(label[0], ast.AST):
                test, val = label
                ok = True
                for t, v in expand_test(test, val, aliases):
                    ok, nfacts = _edge_ok(nfacts, t, v)
                    if not ok:
                        break
                    for ev in cond_events(t, v):
                        nst = step(sm, nst, ev, ctx)
                if not ok:
                    continue
            stack.append((succ, nst, nfacts, trace, raise_edge))
