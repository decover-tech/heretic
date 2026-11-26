"""Coroutines that are created and then dropped.

Writing `async def f` declares that f's result must be awaited (or handed to
something that awaits it). A call `f()` whose result is thrown away, or
stored in a variable that is never read, contradicts that declaration: the
body never runs.

Two levels of confidence:

  MUST  the callee is resolved exactly: `self.m()` / `cls.m()` against the
        enclosing class, bare names against the module and project imports.
  MAY   `obj.m()` with an unknown receiver. Matching on the name alone is
        unsafe (`set.discard`, the sync redis `expire`, sync gRPC stubs all
        share names with project coroutines), so the codebase is asked
        instead: if `.m(...)` is awaited at e of n call sites, the dropped
        ones are ranked by z(n, e).
"""
import ast

from . import Checker
from ..report import Finding, z_score

COMMON = {
    "get", "post", "put", "patch", "delete", "head", "close", "send", "run", "execute", "commit", "rollback",
    "start", "stop", "connect", "disconnect", "read", "write", "open", "fetch", "call", "update", "create",
    "save", "load", "flush", "refresh", "query", "publish", "subscribe", "acquire", "release", "wait", "join",
    "cancel", "invoke", "process", "handle", "list", "search", "upload", "download", "add", "remove", "set",
    "init", "setup", "cleanup", "reset", "validate", "parse", "build", "render", "dispatch", "emit", "notify",
    "check", "health", "ping", "stream", "generate", "complete", "embed", "aclose", "shutdown", "lock",
}
ASYNC_STDLIB = {("asyncio", "sleep"), ("asyncio", "wait_for"), ("asyncio", "gather"), ("asyncio", "wait")}


def _is_consumed(call: ast.Call, fx) -> bool:
    p = fx.parents.get(id(call))
    if isinstance(p, ast.Expr):
        return False
    if isinstance(p, (ast.Assign, ast.AnnAssign)):
        tgts = p.targets if isinstance(p, ast.Assign) else [p.target]
        names = {t.id for t in tgts if isinstance(t, ast.Name)}
        if not names or len(names) != len(tgts):
            return True  # stored on an object / unpacked: assume handled
        for n in fx.own_nodes():
            if isinstance(n, ast.Name) and n.id in names and isinstance(n.ctx, ast.Load):
                return True
        return False
    return True


class Unawaited(Checker):
    name = "unawaited-coroutine"
    kind = "MUST"
    description = "Coroutine function called but its coroutine is never awaited."

    def _resolve(self, call: ast.Call, fx):
        """Return (is_async, confidence_score, what) or None if unknown."""
        f = call.func
        if isinstance(f, ast.Name):
            n = f.id
            if n in self.module.defs:
                return self.module.defs[n], 0.0, n
            if n in self.module.imports:
                defs = [d for d in self.idx.defs(n) if not d.is_method]
                if defs and all(d.is_async for d in defs):
                    return True, -2.0, n
            return None
        if not isinstance(f, ast.Attribute):
            return None
        m = f.attr
        recv = f.value
        if isinstance(recv, ast.Name) and recv.id in ("asyncio",) and (recv.id, m) in ASYNC_STDLIB:
            return True, 0.0, f"asyncio.{m}"
        if isinstance(recv, ast.Name) and recv.id in ("self", "cls") and fx.cls is not None:
            meths = self.module.classes.get(fx.cls.name, {})
            if m in meths:
                return meths[m], 0.0, f"{fx.cls.name}.{m}"
        return None  # receiver type unknown: handled statistically in function()/aggregate()

    def begin_file(self, path, tree, module, idx):
        self.path, self.module, self.idx = path, module, idx
        self.awaits = {}  # method name -> [awaited, total] over obj.m(...) calls
        self.candidates = []  # (name, path, line, qual) dropped obj.m(...) coroutines

    def function(self, fx):
        for n in fx.own_nodes():
            if not isinstance(n, ast.Call):
                continue
            p = fx.parents.get(id(n))
            awaited = isinstance(p, ast.Await)
            f = n.func
            if isinstance(f, ast.Attribute) and not (isinstance(f.value, ast.Name) and f.value.id in ("self", "cls")):
                c = self.awaits.setdefault(f.attr, [0, 0])
                c[0] += awaited
                c[1] += 1
            if awaited:
                continue
            r = self._resolve(n, fx)
            if r is None:
                # unknown receiver: a candidate only if the project defines `m` purely as async
                # only inside `async def`: in a sync def nothing can be awaited, so an unknown
                # receiver there is almost always a sync namesake (sync redis, sync gRPC stub)
                if isinstance(fx.fnode, ast.AsyncFunctionDef) and isinstance(f, ast.Attribute) \
                        and not f.attr.startswith("__") and not _is_consumed(n, fx):
                    defs = self.idx.defs(f.attr)
                    if defs and all(d.is_async for d in defs):
                        self.candidates.append((f.attr, fx.path, n.lineno, fx.qual))
                continue
            if not r[0] or _is_consumed(n, fx):
                continue
            _, score, what = r
            fx.emit(Finding(self.name, self.kind, fx.path, n.lineno, fx.qual,
                            f"`{what}` is `async def`, but the coroutine created here is never awaited, "
                            f"so its body never runs.", score=score))

    def end_file(self):
        return (self.awaits, self.candidates)

    @classmethod
    def aggregate(cls, partials, idx):
        """MAY: is `obj.m()` usually awaited across the codebase? (SOSP'01-style belief counting)

        Name-only resolution is unsafe: `set.discard`, sync redis `expire`, a sync gRPC stub
        all share names with project coroutines. So ask the code: if `.m(...)` is awaited at
        e of n call sites, the codebase believes `.m` returns a coroutine; rank the dropped
        ones by z(n, e).
        """
        totals = {}
        cands = []
        for part in partials:
            if not part:
                continue
            aw, cs = part
            for k, (a, t) in aw.items():
                x = totals.setdefault(k, [0, 0])
                x[0] += a
                x[1] += t
            cands.extend(cs)
        out = []
        for name, path, line, qual in cands:
            e, n = totals.get(name, (0, 0))
            if n == 0 or e < 3 or e / n < 0.75:
                continue
            z = z_score(n, e)
            out.append(Finding(cls.name, "MAY", path, line, qual,
                               f"`.{name}(...)` is awaited at {e} of {n} call sites (and every `{name}` defined "
                               f"in the project is `async def`), but here its result is dropped.",
                               score=z, evidence={"awaited": e, "calls": n, "z": round(z, 2)}))
        return out
