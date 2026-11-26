"""Declared belief vs. implemented behaviour.

`def f() -> User:` states the belief that f never returns None. A bare
`return`, a `return None`, or a path that falls off the end of the body
contradicts it. Callers that trust the annotation and skip the None check
will crash. (This is cross-checking a function's interface against its body,
like the internal-consistency checks in SOSP'01.)
"""
from typing import List

from ..report import Finding
from . import Checker

SKIP_DECORATORS = {"overload", "abstractmethod", "abstractproperty", "setter", "deleter", "fixture"}
SKIP_NAMES = {"__init__", "__post_init__", "__exit__", "__aexit__", "setUp", "tearDown", "__del__"}


class ReturnAnnotation(Checker):
    name = "return-annotation"
    kind = "MUST"
    description = "Function annotated with a non-Optional return type can return None."

    @classmethod
    def aggregate(cls, partials, idx) -> List[Finding]:
        out = []
        for fis in idx.funcs.values():
            for fi in fis:
                if fi.ann != "nonoptional" or fi.is_stub or fi.is_generator or fi.name in SKIP_NAMES:
                    continue
                if SKIP_DECORATORS & set(fi.decorators):
                    continue
                if fi.path not in idx.files:
                    continue
                for ln in fi.explicit_none:
                    out.append(Finding(cls.name, cls.kind, fi.path, ln, fi.qual,
                                       f"`{fi.qual}` is annotated `-> {fi.ann_text}` but returns None here.",
                                       score=-1.0))
                if fi.falls_off:
                    out.append(Finding(cls.name, cls.kind, fi.path, fi.line, fi.qual,
                                       f"`{fi.qual}` is annotated `-> {fi.ann_text}` but some path falls off "
                                       f"the end of the body and returns None implicitly.", score=-15.0))
        return out
