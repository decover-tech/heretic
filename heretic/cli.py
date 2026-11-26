"""heretic command line."""
from __future__ import annotations

import argparse
import os
import sys
import time
from collections import Counter

from . import __version__
from .checkers import all_checkers
from .report import render_json, render_sarif, render_text
from .runner import analyze

DEFAULT_EXCLUDE = ["*_pb2.py", "*_pb2_grpc.py", "*/alembic/versions/*", "*/migrations/*"]


def main(argv=None) -> int:
    reg = all_checkers()
    ap = argparse.ArgumentParser(
        prog="heretic",
        description="Find bugs as deviant behavior: infer what a Python codebase believes, "
                    "then report the code that contradicts it.")
    ap.add_argument("path", nargs="?", default=".", help="project root to analyze")
    ap.add_argument("-c", "--checkers", default=",".join(reg), help="comma-separated checkers (default: all)")
    ap.add_argument("-x", "--exclude", action="append", default=[],
                    help="glob of paths to skip (repeatable); generated protobuf and migrations are skipped by default")
    ap.add_argument("--no-default-excludes", action="store_true")
    ap.add_argument("-f", "--format", choices=["text", "json", "sarif"], default="text")
    ap.add_argument("-o", "--output", help="write report to a file instead of stdout")
    ap.add_argument("-n", "--top", type=int, default=25, help="findings shown per section in text mode")
    ap.add_argument("-j", "--jobs", type=int, default=0, help="worker processes (default: cpus - 1)")
    ap.add_argument("--ignore-file", help="file of suppressed fingerprints (default: <path>/.heretic-ignore)")
    ap.add_argument("--min-score", type=float, default=None, help="drop MAY findings with z below this")
    ap.add_argument("--no-trace", action="store_true", help="omit the path explanation")
    ap.add_argument("--list-checkers", action="store_true")
    ap.add_argument("-q", "--quiet", action="store_true")
    ap.add_argument("--version", action="version", version=f"heretic {__version__}")
    a = ap.parse_args(argv)

    if a.list_checkers:
        for name, c in reg.items():
            print(f"{name:22} {c.kind:5} {c.description}")
        return 0
    names = [n.strip() for n in a.checkers.split(",") if n.strip()]
    bad = [n for n in names if n not in reg]
    if bad:
        ap.error(f"unknown checker(s): {', '.join(bad)}. Use --list-checkers.")
    if not os.path.isdir(a.path):
        ap.error(f"{a.path} is not a directory")
    exclude = ([] if a.no_default_excludes else DEFAULT_EXCLUDE) + a.exclude
    ignore = a.ignore_file or os.path.join(a.path, ".heretic-ignore")
    log = (lambda m: None) if a.quiet else (lambda m: print(f"heretic: {m}", file=sys.stderr))

    t0 = time.time()
    findings, stats = analyze(a.path, names, exclude, a.jobs, ignore, progress=log)
    if a.min_score is not None:
        findings = [f for f in findings if f.kind != "MAY" or f.score >= a.min_score]

    if a.format == "json":
        body = render_json(findings)
    elif a.format == "sarif":
        body = render_sarif(findings)
    else:
        body = render_text(findings, a.path, a.top, show_trace=not a.no_trace)
    if a.output:
        with open(a.output, "w") as fh:
            fh.write(body)
    else:
        sys.stdout.write(body)

    counts = Counter(f"{f.checker}" for f in findings)
    log(f"{stats['files']} files in {time.time() - t0:.1f}s; "
        + ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
        + (f"; {stats['suppressed']} suppressed" if stats["suppressed"] else "")
        + (f"; {len(stats['unparsable'])} unparsable" if stats["unparsable"] else "")
        + (f"; {len(stats['checker_errors'])} checker errors" if stats["checker_errors"] else ""))
    for e in stats["checker_errors"][:5]:
        log(f"  checker error: {e}")
    return 1 if any(f.kind == "MUST" for f in findings) else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
