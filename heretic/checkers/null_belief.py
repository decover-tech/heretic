"""MUST beliefs about None (Engler et al. SOSP'01, section 4).

Dereferencing `v` means the programmer believes `v` is not None.
Comparing `v` to None means they believe it *might* be None. On one
execution these beliefs cannot both hold:

  check-then-use  `if v is None: ... v.x`  crashes on that path. One path is
                  enough to report it.
  use-then-check  `v.x ... if v is None`   the check is dead, or the earlier use
                  can crash. Reported only when *every* path that reaches the check
                  dereferenced `v` first, with no test in between. A check that is
                  dead on one path but needed on another
                  (`except: v = None` / `else: v.x`, then `if v is None`) is not a
                  contradiction.

A dereference under an earlier `v is not None` test is not a belief that `v` is
non-None. It is that test's consequence, so re-testing later is merely
redundant and not reported. Only real None tests (`is None`, `== None`) count,
not truthiness: an empty list is falsy and still safe to dereference.
"""
from ..metal import SM, Deref, IsNone, Rule
from ..report import Finding, path_rank
from . import Checker


def _rec(outcome):
    def act(ctx, var, data, ev):
        slot = ctx.store.setdefault("checks", {}).setdefault((ev.line, var), {"bad": None, "ok": False})
        if outcome == "ok":
            slot["ok"] = True
        elif slot["bad"] is None:
            slot["bad"] = (data[0], ctx.trace)
    return act


class NullBeliefSM(SM):
    name = "null-belief"
    kind = "MUST"
    loop_local = True  # `if best is None or x > best.v` across iterations is an idiom
    rules = [
        Rule("start", IsNone(), true="null", false="tested", action=_rec("ok")),
        Rule("start", Deref(), dst="derefd"),
        Rule("derefd", IsNone(), dst="stop", action=_rec("bad")),
        Rule("tested", IsNone(), true="null", false="tested", action=_rec("ok")),
        Rule("null", IsNone(), true="null", false="tested", action=_rec("ok")),
        Rule("null", Deref(), dst="stop",
             err="`{v}` is dereferenced on a path where it is None (tested at line {t})."),
    ]


class NullBelief(Checker):
    name = "null-belief"
    kind = "MUST"
    description = "Contradictory None beliefs: check-then-use on a path; use-then-check on every path."

    def function(self, fx):
        store = {}
        fx.run_sm(NullBeliefSM(), store)
        for (line, var), slot in store.get("checks", {}).items():
            if slot["ok"] or slot["bad"] is None:
                continue
            trigger, trace = slot["bad"]
            score, lines = path_rank(trace, trigger, line)
            fx.emit(Finding(self.name, self.kind, fx.path, line, fx.qual,
                            f"`{var}` is compared to None here, but every path to this check already "
                            f"dereferenced it (e.g. line {trigger}). Either the check is dead or the "
                            f"earlier use can raise on None.",
                            score=score, trace=lines))
