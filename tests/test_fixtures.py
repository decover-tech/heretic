"""End-to-end: every planted bug in tests/fixtures/demo is found, and nothing else."""
import os

import pytest

from heretic.checkers import all_checkers
from heretic.runner import analyze

DEMO = os.path.join(os.path.dirname(__file__), "fixtures", "demo")

EXPECTED = {
    ("null-belief", "app/null_cases.py", 3),
    ("null-belief", "app/null_cases.py", 10),
    ("null-belief", "app/null_cases.py", 100),
    ("unawaited-coroutine", "app/async_cases.py", 20),
    ("unawaited-coroutine", "app/async_cases.py", 21),
    ("unawaited-coroutine", "app/async_cases.py", 22),
    ("unawaited-coroutine", "app/async_cases.py", 74),
    ("blocking-in-async", "app/async_cases.py", 33),
    ("blocking-in-async", "app/async_cases.py", 34),
    ("blocking-in-async", "app/async_cases.py", 35),
    ("return-annotation", "app/annotations.py", 10),
    ("return-annotation", "app/annotations.py", 14),
    ("lock-release", "app/locks.py", 9),
    ("org-scope", "app/repo.py", 38),
    ("org-scope", "app/repo.py", 59),
    ("ret-check", "app/callers.py", 26),
    ("ret-check", "app/callers.py", 31),
    ("pairing", "app/spans.py", 43),
    # MAY, by design: pairing sees acquire() without release(); it cannot know the
    # signal semaphore is released by another thread (lock-release, a MUST checker, stays quiet)
    ("pairing", "app/locks.py", 44),
}


@pytest.fixture(scope="module")
def findings():
    fs, stats = analyze(DEMO, list(all_checkers()), [], jobs=1)
    assert not stats["checker_errors"], stats["checker_errors"]
    return fs


def test_all_planted_bugs_found(findings):
    got = {(f.checker, f.path, f.line) for f in findings}
    missing = EXPECTED - got
    assert not missing, f"missed: {sorted(missing)}"


def test_no_false_positives(findings):
    got = {(f.checker, f.path, f.line) for f in findings}
    extra = got - EXPECTED
    assert not extra, f"unexpected: {sorted(extra)}"


def test_fingerprints_stable_and_unique(findings):
    fps = [f.fingerprint for f in findings]
    assert all(len(x) == 12 for x in fps)
    assert len(set(fps)) == len(fps)


def test_parallel_matches_serial():
    a, _ = analyze(DEMO, list(all_checkers()), [], jobs=1)
    b, _ = analyze(DEMO, list(all_checkers()), [], jobs=2)
    assert sorted(f.fingerprint for f in a) == sorted(f.fingerprint for f in b)


def test_suppression(tmp_path, findings):
    ign = tmp_path / "ignore"
    ign.write_text(findings[0].fingerprint + "  # triaged: intentional\n")
    fs, stats = analyze(DEMO, list(all_checkers()), [], jobs=1, ignore_file=str(ign))
    assert stats["suppressed"] == 1
    assert findings[0].fingerprint not in {f.fingerprint for f in fs}
