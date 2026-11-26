"""Paired acquire/release on every path (metal's lock checker, OSDI'00).

`v.acquire()` must be followed by `v.release()` on every path that returns
normally. Handing `v` to someone else (returning it or passing it on)
transfers the obligation, and `with v:` never triggers the checker.

Three idioms are not lock pairing and are skipped:
  * try-locks: `if lock.acquire(): ...` / `if not await lock.acquire(): return`
    are only tracked when the result of acquire() is discarded;
  * signal semaphores: if this function never calls `v.release()`, `v` is
    released by another thread or task (RacerX: "signal-wait semaphores are
    rarely paired");
  * callbacks: if `v` is captured by a lambda or nested def, the release has
    been handed off (`fut.add_done_callback(lambda _: v.release())`).
"""
import ast

from ..events import key_of
from ..metal import SM, CallOn, End, Escape, Rule
from . import Checker


class _TrackedAcquire(CallOn):
    def __init__(self, allowed_ids):
        super().__init__("acquire")
        self.allowed = allowed_ids

    def match(self, ev):
        m = super().match(ev)
        if m and id(ev.node) in self.allowed:
            return m
        return None


def make_sm(allowed_ids):
    class ResourceSM(SM):
        name = "lock-release"
        kind = "MUST"
        rules = [
            Rule("start", _TrackedAcquire(allowed_ids), dst="held"),
            Rule("held", CallOn("release"), dst="stop"),
            Rule("held", Escape(), dst="stop"),
            Rule("held", End(), dst="stop",
                 err="`{v}` is acquired at line {t} but not released on this path (use `with` or try/finally)."),
        ]

    return ResourceSM()


class _Any(set):
    def __contains__(self, item):
        return True


ANY = _Any()  # make_sm(ANY): track every acquire() (used by unit tests)


class Resource(Checker):
    name = "lock-release"
    kind = "MUST"
    description = "acquire() without release() on some normal-return path."

    def function(self, fx):
        acquires, released, captured = [], set(), set()
        for n in fx.own_nodes():
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute):
                k = key_of(n.func.value)
                if n.func.attr == "acquire" and k:
                    acquires.append((n, k))
                elif n.func.attr == "release" and k:
                    released.add(k)
        for n in ast.walk(fx.fnode):
            if n is fx.fnode:
                continue
            if isinstance(n, (ast.Lambda, ast.FunctionDef, ast.AsyncFunctionDef)):
                for x in ast.walk(n):
                    if isinstance(x, (ast.Name, ast.Attribute)):
                        k = key_of(x)
                        if k:
                            captured.add(k)
        allowed = set()
        for call, k in acquires:
            if k not in released or k in captured:
                continue
            if call.args or any(kw.arg in ("blocking", "timeout") for kw in call.keywords):
                continue
            p = fx.parents.get(id(call))
            if isinstance(p, ast.Await):
                p = fx.parents.get(id(p))
            if not isinstance(p, ast.Expr):
                continue  # result used: a try-lock, not an unconditional acquire
            allowed.add(id(call))
        if allowed:
            fx.run_sm(make_sm(allowed), {})
