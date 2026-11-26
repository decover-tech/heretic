"""Unit tests for the CFG + path engine on small snippets."""
import ast
import textwrap

from heretic.cfg import CFG
from heretic.metal import SM, Ctx, Deref, IsNone, Rule, run
from heretic.report import z_score


def _findings(src, sm_cls):
    tree = ast.parse(textwrap.dedent(src))
    f = tree.body[0]
    out = []
    ctx = Ctx("t.py", f.name, f, lambda sm, line, msg, *a: out.append((line, msg)), {})
    run(sm_cls(), CFG(f), ctx)
    return out


class CheckThenUse(SM):
    name = "t"
    rules = [Rule("start", IsNone(), true="null"), Rule("null", Deref(), dst="stop", err="{v}")]


def test_false_path_pruning():
    # the second test on x is refuted by the first: no path reaches x.a with x None
    src = """
    def f(x, y):
        if x is None:
            y = 1
        if x is not None:
            return x.a
    """
    assert _findings(src, CheckThenUse) == []


def test_short_circuit_lowered_to_branches():
    assert _findings("def f(x):\n    return x is None or x.a\n", CheckThenUse) == []
    assert _findings("def f(x):\n    if x is None and x.a:\n        pass\n", CheckThenUse) == [(2, "x")]


def test_killvars():
    src = """
    def f(x):
        if x is None:
            x = make()
        return x.a
    """
    assert _findings(src, CheckThenUse) == []


def test_noreturn_kills_path():
    src = """
    import sys
    def f(x):
        if x is None:
            sys.exit(1)
        return x.a
    """
    tree = ast.parse(textwrap.dedent(src))
    f = tree.body[1]
    out = []
    run(CheckThenUse(), CFG(f), Ctx("t", "f", f, lambda *a: out.append(a), {}))
    assert out == []


def test_finally_runs_on_return():
    src = """
    def f(lock, x):
        lock.acquire()
        try:
            return x
        finally:
            lock.release()
    """
    from heretic.checkers.resource import ANY, make_sm

    assert _findings(src, lambda: make_sm(ANY)) == []


def test_loop_terminates():
    src = """
    def f(xs):
        n = None
        for x in xs:
            while n is None:
                n = x
        return n
    """
    assert _findings(src, CheckThenUse) == []


def test_z_score_orders_by_evidence():
    assert z_score(100, 99) > z_score(10, 9) > z_score(10, 5)
