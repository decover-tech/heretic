"""Phase 1: whole-program index and function summaries.

xgcc ran a global pass that emitted per-function facts and linked them into a
call graph before checking.  We do the same:

  * every def, whether it is `async`, its declared return annotation,
  * a may-return-None summary, closed under a fixpoint over the call graph
    (`return helper()` inherits helper's summary),
  * ORM models that carry an `org_id` column, and helpers that apply an
    org_id filter (so calling them counts as scoping a query),
  * blocking beliefs: functions somebody pushes through `to_thread` /
    `run_in_executor` (the programmer believes they block).
"""
from __future__ import annotations

import ast
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

from .cfg import CFG, functions
from .events import callee_name

# library calls that return Optional by contract
KNOWN_OPTIONAL = {
    "first", "one_or_none", "scalar_one_or_none", "scalar", "get", "getenv",
    "match", "search", "fullmatch", "find_one", "get_object_or_none", "getattr",
    "next", "pop",
}
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "env", ".tox", "build", "dist",
             ".mypy_cache", ".pytest_cache", "site-packages"}


@dataclass
class FuncInfo:
    path: str
    qual: str
    name: str
    line: int
    is_async: bool
    is_method: bool
    is_generator: bool
    is_stub: bool
    ann: Optional[str]  # 'optional' | 'nonoptional' | 'none' | None
    ann_text: str
    explicit_none: List[int]  # lines of `return` / `return None`
    falls_off: bool
    returns_calls: List[str]  # callee names returned directly
    soft_none: List[int] = field(default_factory=list)  # `return v` where v is assigned None somewhere
    may_none: bool = False
    has_return: bool = False
    decorators: List[str] = field(default_factory=list)


@dataclass
class Index:
    funcs: Dict[str, List[FuncInfo]] = field(default_factory=dict)  # by bare name
    org_models: Set[str] = field(default_factory=set)
    scopers: Set[str] = field(default_factory=set)
    blocking_beliefs: Dict[str, int] = field(default_factory=dict)
    noreturn: frozenset = frozenset()  # names whose every definition always raises
    files: List[str] = field(default_factory=list)

    def defs(self, name: str) -> List[FuncInfo]:
        return self.funcs.get(name, [])


def iter_py_files(root: str, exclude: List[str]) -> List[str]:
    import fnmatch

    out = []
    for d, dirs, files in os.walk(root):
        dirs[:] = [x for x in dirs if x not in SKIP_DIRS and not x.startswith(".")]
        for f in files:
            if not f.endswith(".py"):
                continue
            p = os.path.relpath(os.path.join(d, f), root)
            if any(fnmatch.fnmatch(p, pat) for pat in exclude):
                continue
            out.append(p)
    return sorted(out)


def parse(root: str, rel: str) -> Optional[ast.AST]:
    try:
        with open(os.path.join(root, rel), "rb") as fh:
            return ast.parse(fh.read(), filename=rel)
    except (SyntaxError, ValueError, UnicodeDecodeError, OSError):
        return None


def _ann_kind(a: Optional[ast.AST]) -> Optional[str]:
    if a is None:
        return None
    try:
        t = ast.unparse(a).replace(" ", "")
    except Exception:
        return None
    t = t.strip("'\"")
    raw = ast.unparse(a)  # keep spaces: `X or None` must still read as optional
    if t in ("None", "NoReturn", "Never"):
        return "none"
    import re as _re
    if _re.search(r"\b(None|Any|object)\b", raw) and t not in ("None",):
        return "optional"
    if t in ("Any", "object", "typing.Any") or t.startswith(("Optional[", "typing.Optional[")) or "|None" in t \
            or t.startswith("None|") or ",None]" in t or t.startswith(("Union[", "typing.Union[")) and "None" in t:
        return "optional"
    if t.startswith(("Iterator", "Generator", "AsyncIterator", "AsyncGenerator", "Iterable", "AsyncIterable",
                     "typing.Iterator", "typing.Generator", "typing.AsyncGenerator", "typing.AsyncIterator")):
        return None
    return "nonoptional"


def _is_stub(f) -> bool:
    body = list(f.body)
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(
            body[0].value.value, str):
        body = body[1:]
    if not body:
        return True
    if len(body) == 1:
        s = body[0]
        if isinstance(s, ast.Pass):
            return True
        if isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant) and s.value.value is Ellipsis:
            return True
        if isinstance(s, ast.Raise) and s.exc is not None:
            e = s.exc.func if isinstance(s.exc, ast.Call) else s.exc
            return getattr(e, "id", getattr(e, "attr", "")) == "NotImplementedError"
    return False


def _own_nodes(f):
    """Walk a function body without descending into nested defs/lambdas/classes."""
    nested = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)
    stack = [s for s in f.body if not isinstance(s, nested)]
    while stack:
        n = stack.pop()
        yield n
        for c in ast.iter_child_nodes(n):
            if isinstance(c, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
                continue
            stack.append(c)


def _decorator_names(f) -> List[str]:
    out = []
    for d in f.decorator_list:
        d = d.func if isinstance(d, ast.Call) else d
        if isinstance(d, ast.Attribute):
            out.append(d.attr)
        elif isinstance(d, ast.Name):
            out.append(d.id)
    return out


def summarize_file(root: str, rel: str) -> dict:
    """Runs in a worker. Returns picklable facts for one file."""
    tree = parse(root, rel)
    if tree is None:
        return {"path": rel, "ok": False}
    funcs: List[FuncInfo] = []
    method_ids = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for b in node.body:
                if isinstance(b, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    method_ids.add(id(b))
    for qual, f in functions(tree):
        is_gen = False
        has_return = False
        explicit_none, returns_calls, none_vars, ret_names = [], [], set(), []
        for n in _own_nodes(f):
            if isinstance(n, (ast.Yield, ast.YieldFrom)):
                is_gen = True
            elif isinstance(n, ast.Return):
                has_return = True
                v = n.value
                if v is None or (isinstance(v, ast.Constant) and v.value is None):
                    explicit_none.append(n.lineno)
                else:
                    if isinstance(v, ast.Await):
                        v = v.value
                    if isinstance(v, ast.Call) and callee_name(v):
                        returns_calls.append(callee_name(v))
                    elif isinstance(v, ast.IfExp) and any(
                            isinstance(x, ast.Constant) and x.value is None for x in (v.body, v.orelse)):
                        explicit_none.append(n.lineno)
                    elif isinstance(v, ast.Name):
                        ret_names.append((v.id, n.lineno))
            elif isinstance(n, (ast.Assign, ast.AnnAssign)):
                val = n.value
                if isinstance(val, ast.Constant) and val.value is None:
                    tgts = n.targets if isinstance(n, ast.Assign) else [n.target]
                    for t in tgts:
                        if isinstance(t, ast.Name):
                            none_vars.add(t.id)
        soft_none = sorted(line for name, line in ret_names if name in none_vars)
        try:
            falls = CFG(f).falls_off_end
        except RecursionError:
            falls = False
        funcs.append(FuncInfo(
            path=rel, qual=qual, name=f.name, line=f.lineno,
            is_async=isinstance(f, ast.AsyncFunctionDef), is_method=id(f) in method_ids,
            is_generator=is_gen, is_stub=_is_stub(f), ann=_ann_kind(f.returns),
            ann_text=ast.unparse(f.returns) if f.returns is not None else "",
            explicit_none=sorted(set(explicit_none)), falls_off=falls, returns_calls=returns_calls,
            decorators=_decorator_names(f), has_return=has_return, soft_none=soft_none,
        ))

    org_models, scopers, blocking = set(), set(), {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for b in node.body:
                tgt = None
                if isinstance(b, ast.Assign) and len(b.targets) == 1:
                    tgt = b.targets[0]
                elif isinstance(b, ast.AnnAssign):
                    tgt = b.target
                if isinstance(tgt, ast.Name) and tgt.id == "org_id":
                    v = b.value
                    if isinstance(v, ast.Call) and callee_name(v) in ("Column", "mapped_column"):
                        org_models.add(node.name)
                    elif isinstance(b, ast.AnnAssign) and "Mapped" in ast.unparse(b.annotation):
                        org_models.add(node.name)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            src_has_org = False
            has_filter = False
            for n in _own_nodes(node):
                if isinstance(n, ast.Attribute) and n.attr == "org_id":
                    src_has_org = True
                if isinstance(n, ast.keyword) and n.arg == "org_id":
                    src_has_org = True
                if isinstance(n, ast.Call) and callee_name(n) in ("filter", "where", "filter_by"):
                    has_filter = True
            if src_has_org and has_filter:
                scopers.add(node.name)
        elif isinstance(node, ast.Call):
            n = callee_name(node)
            target = None
            if n == "to_thread" and node.args:
                target = node.args[0]
            elif n == "run_in_executor" and len(node.args) >= 2:
                target = node.args[1]
            elif n in ("run_sync", "sync_to_async") and node.args:
                target = node.args[0]
            if target is not None:
                t = target
                if isinstance(target, ast.Call) and callee_name(target) == "partial" and target.args:
                    t = target.args[0]
                tn = t.attr if isinstance(t, ast.Attribute) else getattr(t, "id", None)
                if tn:
                    blocking[tn] = blocking.get(tn, 0) + 1
    return {"path": rel, "ok": True, "funcs": funcs, "org_models": org_models, "scopers": scopers,
            "blocking": blocking}


def build_index(results: List[dict]) -> Index:
    idx = Index()
    for r in results:
        if not r.get("ok"):
            continue
        idx.files.append(r["path"])
        for fi in r["funcs"]:
            idx.funcs.setdefault(fi.name, []).append(fi)
        idx.org_models |= r["org_models"]
        idx.scopers |= r["scopers"]
        for k, v in r["blocking"].items():
            idx.blocking_beliefs[k] = idx.blocking_beliefs.get(k, 0) + v
    # kill_paths: functions that never return normally (every path raises)
    idx.noreturn = frozenset(
        name for name, fis in idx.funcs.items()
        if fis and all(not f.is_generator and not f.is_stub and not f.falls_off and not f.has_return
                       for f in fis)
    )
    # interprocedural fixpoint for may_none
    for fis in idx.funcs.values():
        for fi in fis:
            fi.may_none = (not fi.is_generator and not fi.is_stub
                           and (bool(fi.explicit_none or fi.soft_none) or fi.falls_off)) or fi.ann == "optional"
    changed = True
    rounds = 0
    while changed and rounds < 20:
        changed = False
        rounds += 1
        for fis in idx.funcs.values():
            for fi in fis:
                if fi.may_none or fi.is_generator or fi.is_stub:
                    continue
                for c in fi.returns_calls:
                    if c in KNOWN_OPTIONAL or any(g.may_none and g.ann != "nonoptional" for g in idx.defs(c)):
                        fi.may_none = True
                        changed = True
                        break
    return idx
