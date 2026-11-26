"""Control-flow graphs for Python functions (the xgcc substrate).

One node per statement-level event.  Conditions are lowered the way a
compiler does it: `a and b` becomes two branch nodes, so a checker sees
short-circuit guards as real control flow (this removes a whole class of
false positives Engler et al. attribute to macro / boolean noise).

Edges carry an optional label:
    (test_ast, True|False)   branch outcome
    ("exc",)                 exceptional edge into an except handler
    ("raise",)               abrupt exit
"""
from __future__ import annotations

import ast
from typing import List, Optional, Tuple

# Calls after which the path is dead (xgcc's `kill_paths`: panic(), BUG()).
NORETURN_CALLS = {"exit", "_exit", "abort", "fail", "skip"}  # sys.exit, os._exit, pytest.fail ...


class Node:
    __slots__ = ("id", "kind", "ast", "succs", "line")

    def __init__(self, nid: int, kind: str, node: Optional[ast.AST]):
        self.id = nid
        self.kind = kind
        self.ast = node
        self.succs: List[Tuple["Node", object]] = []
        self.line = getattr(node, "lineno", 0) if node is not None else 0

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<{self.kind}@{self.line}#{self.id}>"


Pending = List[Tuple[Node, object]]


def _const_truth(test: ast.AST) -> Optional[bool]:
    if isinstance(test, ast.Constant):
        return bool(test.value)
    if isinstance(test, ast.Name) and test.id == "TYPE_CHECKING":
        return False
    return None


def is_noreturn_call(stmt: ast.stmt, extra=frozenset()) -> bool:
    """sys.exit / os._exit / pytest.fail, plus project functions that always raise."""
    if not isinstance(stmt, ast.Expr):
        return False
    v = stmt.value.value if isinstance(stmt.value, ast.Await) else stmt.value
    if not isinstance(v, ast.Call):
        return False
    f = v.func
    name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
    return name in NORETURN_CALLS or name in extra


class CFG:
    def __init__(self, func: ast.AST, noreturn=frozenset()):
        self.func = func
        self.noreturn = noreturn
        self.nodes: List[Node] = []
        self.entry = self._new("entry", func)
        self.exit = self._new("exit", func)
        self.entry.line = self.exit.line = 0  # synthetic nodes: keep them out of traces
        self._loops: List[Tuple[Node, Pending]] = []  # (continue target, break list)
        self._handlers: List[List[Node]] = []  # innermost last
        self._finally: List[List[ast.stmt]] = []
        body = func.body if not isinstance(func, ast.Lambda) else []
        end = self._stmts(body, [(self.entry, None)])
        self.falls_off_end = bool(end)
        self._link(end, self.exit)

    # -- helpers -----------------------------------------------------------
    def _new(self, kind: str, node: Optional[ast.AST]) -> Node:
        n = Node(len(self.nodes), kind, node)
        self.nodes.append(n)
        return n

    @staticmethod
    def _link(pend: Pending, target: Node) -> None:
        for n, lab in pend:
            n.succs.append((target, lab))

    def _simple(self, kind: str, s: ast.AST, pend: Pending) -> Pending:
        n = self._new(kind, s)
        self._link(pend, n)
        return [(n, None)]

    # -- conditions --------------------------------------------------------
    def cond(self, test: ast.AST, pend: Pending) -> Tuple[Pending, Pending]:
        if isinstance(test, ast.BoolOp):
            if isinstance(test.op, ast.And):
                tp, fps = pend, []
                for v in test.values:
                    tp, fp = self.cond(v, tp)
                    fps += fp
                return tp, fps
            fp, tps = pend, []
            for v in test.values:
                tp, fp = self.cond(v, fp)
                tps += tp
            return tps, fp
        if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
            tp, fp = self.cond(test.operand, pend)
            return fp, tp
        c = _const_truth(test)
        if c is not None:
            return (pend, []) if c else ([], pend)
        b = self._new("branch", test)
        self._link(pend, b)
        return [(b, (test, True))], [(b, (test, False))]

    # -- statements --------------------------------------------------------
    def _stmts(self, body: List[ast.stmt], pend: Pending) -> Pending:
        for s in body:
            if not pend:
                break  # unreachable code
            pend = self._stmt(s, pend)
        return pend

    def _abrupt_exit(self, node: Node, label) -> None:
        """Route a return/raise through enclosing finally blocks, then exit."""
        pend: Pending = [(node, label)]
        saved = self._finally
        for fin in reversed(saved):
            self._finally = []  # a return inside finally-copy must not recurse
            pend = self._stmts(fin, pend)
        self._finally = saved
        self._link(pend, self.exit)

    def _stmt(self, s: ast.stmt, pend: Pending) -> Pending:
        t = type(s)
        if t is ast.If:
            tp, fp = self.cond(s.test, pend)
            a = self._stmts(s.body, tp)
            b = self._stmts(s.orelse, fp) if s.orelse else fp
            return a + b
        if t is ast.While:
            head = self._new("loophead", s)
            self._link(pend, head)
            tp, fp = self.cond(s.test, [(head, None)])
            brk: Pending = []
            self._loops.append((head, brk))
            self._link(self._stmts(s.body, tp), head)
            self._loops.pop()
            after = self._stmts(s.orelse, fp) if s.orelse else fp
            return after + brk
        if t in (ast.For, ast.AsyncFor):
            head = self._new("for", s)  # evaluates/iterates s.iter
            self._link(pend, head)
            bind = self._new("forbind", s)  # binds s.target
            head.succs.append((bind, None))
            brk = []
            self._loops.append((head, brk))
            self._link(self._stmts(s.body, [(bind, None)]), head)
            self._loops.pop()
            done: Pending = [(head, None)]
            after = self._stmts(s.orelse, done) if s.orelse else done
            return after + brk
        if t is ast.Return:
            n = self._new("return", s)
            self._link(pend, n)
            self._abrupt_exit(n, None)
            return []
        if t is ast.Raise:
            n = self._new("raise", s)
            self._link(pend, n)
            if self._handlers:
                for h in self._handlers[-1]:
                    n.succs.append((h, ("exc",)))
            else:
                self._abrupt_exit(n, ("raise",))
            return []
        if t is ast.Break:
            n = self._new("stmt", s)
            self._link(pend, n)
            if self._loops:
                self._loops[-1][1].append((n, None))
            return []
        if t is ast.Continue:
            n = self._new("stmt", s)
            self._link(pend, n)
            if self._loops:
                n.succs.append((self._loops[-1][0], None))
            return []
        if t is ast.Try or t.__name__ == "TryStar":
            return self._try(s, pend)
        if t in (ast.With, ast.AsyncWith):
            p = self._simple("with", s, pend)
            p = self._stmts(s.body, p)
            return self._simple("withexit", s, p) if p else p
        if t is ast.Assert:
            tp, fp = self.cond(s.test, pend)
            for n, lab in fp:
                n.succs.append((self.exit, ("raise",)))
            return tp
        if t is getattr(ast, "Match", None):
            subj = self._simple("stmt", ast.Expr(value=s.subject, lineno=s.lineno, col_offset=0), pend)
            out: Pending = []
            exhaustive = False
            for case in s.cases:
                out += self._stmts(case.body, list(subj))
                if isinstance(case.pattern, ast.MatchAs) and case.pattern.pattern is None and case.guard is None:
                    exhaustive = True
            return out if exhaustive else out + subj
        if t in (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef):
            return self._simple("def", s, pend)
        p = self._simple("stmt", s, pend)
        if is_noreturn_call(s, self.noreturn):
            for n, _ in p:
                n.succs.append((self.exit, ("raise",)))
            return []
        return p

    def _try(self, s, pend: Pending) -> Pending:
        tnode = self._new("try", s)
        self._link(pend, tnode)
        hentries = [self._new("except", h) for h in s.handlers]
        if s.finalbody:
            self._finally.append(s.finalbody)
        if hentries:
            self._handlers.append(hentries)
        # exceptional edges from the try entry and from every top-level body stmt
        body_end: Pending = [(tnode, None)]
        for stmt in s.body:
            if not body_end:
                break
            first = len(self.nodes)
            body_end = self._stmt(stmt, body_end)
            if first < len(self.nodes):
                for h in hentries:
                    self.nodes[first].succs.append((h, ("exc",)))
        if hentries:
            self._handlers.pop()
        for h in hentries:
            tnode.succs.append((h, ("exc",)))
        hend: Pending = []
        for h, hn in zip(s.handlers, hentries):
            hend += self._stmts(h.body, [(hn, None)])
        if s.finalbody:
            self._finally.pop()
        out = (self._stmts(s.orelse, body_end) if s.orelse else body_end) + hend
        if s.finalbody:
            out = self._stmts(s.finalbody, out)
        return out


def functions(tree: ast.AST):
    """Yield (qualname, FunctionDef) for every function, including methods and nested defs."""

    def walk(node, prefix):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                q = f"{prefix}{child.name}"
                yield q, child
                yield from walk(child, q + ".")
            elif isinstance(child, ast.ClassDef):
                yield from walk(child, f"{prefix}{child.name}.")
            else:
                yield from walk(child, prefix)

    yield from walk(tree, "")
