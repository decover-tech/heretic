"""Blocking calls inside `async def` (the async analogue of the kernel rule
"don't block with interrupts disabled / holding a spinlock").

Two sources of rules:
  RULE  well-known blocking APIs (time.sleep, requests.*, subprocess.*, ...)
  MAY   inferred blocking functions: wherever code pushes `f` through
        `asyncio.to_thread(f)` / `loop.run_in_executor(_, f)`, the programmer
        believes `f` blocks.  Calling `f` directly inside `async def` elsewhere
        deviates from that belief.
"""
import ast
from typing import List

from ..report import Finding, z_score
from . import Checker
from .unawaited import COMMON

BLOCKING = {
    ("time", "sleep"),
    ("requests", "get"), ("requests", "post"), ("requests", "put"), ("requests", "patch"),
    ("requests", "delete"), ("requests", "head"), ("requests", "request"),
    ("subprocess", "run"), ("subprocess", "call"), ("subprocess", "check_call"), ("subprocess", "check_output"),
    ("os", "system"), ("urllib.request", "urlopen"), ("request", "urlopen"),
}


def _dotted(f):
    parts = []
    while isinstance(f, ast.Attribute):
        parts.append(f.attr)
        f = f.value
    if isinstance(f, ast.Name):
        parts.append(f.id)
        return ".".join(reversed(parts))
    return None


class BlockingInAsync(Checker):
    name = "blocking-in-async"
    kind = "RULE"
    description = "Blocking call (known or inferred from to_thread/run_in_executor use) inside async def."

    def begin_file(self, path, tree, module, idx):
        self.path, self.module, self.idx = path, module, idx
        self.partial = []

    def function(self, fx):
        if not isinstance(fx.fnode, ast.AsyncFunctionDef):
            return
        for n in fx.own_nodes():
            if not isinstance(n, ast.Call):
                continue
            d = _dotted(n.func)
            if not d:
                continue
            head, _, last = d.rpartition(".")
            origin = self.module.imports.get(d.split(".")[0], "")
            resolved = (origin.rsplit(".", 1)[0], last) if "." not in d and origin else (head, last)
            if resolved in BLOCKING or (head, last) in BLOCKING:
                fx.emit(Finding(self.name, "RULE", fx.path, n.lineno, fx.qual,
                                f"`{d}()` blocks the event loop inside `async def {fx.fnode.name}`; "
                                f"use an async client or `await asyncio.to_thread(...)`.", score=0.0))
                continue
            if last in self.idx.blocking_beliefs and last not in COMMON and len(last) > 3:
                defs = self.idx.defs(last)
                if defs and all(x.is_async for x in defs):
                    continue
                self.partial.append((last, fx.path, n.lineno, fx.qual))

    def end_file(self):
        return self.partial

    @classmethod
    def aggregate(cls, partials, idx) -> List[Finding]:
        out = []
        sites = [s for p in partials if p for s in p]
        by = {}
        for s in sites:
            by.setdefault(s[0], []).append(s)
        for name, ss in by.items():
            e = idx.blocking_beliefs[name]
            n = e + len(ss)
            z = z_score(n, e, p0=0.5)
            for (_, path, line, func) in ss:
                out.append(Finding(cls.name, "MAY", path, line, func,
                                   f"`{name}()` is called directly inside async code, but elsewhere it is "
                                   f"offloaded with to_thread/run_in_executor {e} time(s): the codebase "
                                   f"believes it blocks.",
                                   score=z, evidence={"offloaded": e, "direct_in_async": len(ss), "z": round(z, 2)}))
        return out
