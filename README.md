# Heretic

**Find bugs as deviant behavior.** Heretic reads a Python codebase, infers what
the code *believes* (this value can be None, this call must be awaited, this
model is always filtered by `org_id`), and reports the code that contradicts
those beliefs.

It is a Python reimplementation of the approach from Dawson Engler's group at
Stanford: the metal/xgcc checking system, *Bugs as Deviant Behavior*,
and z-ranking. That work found thousands of bugs in Linux and BSD and became
Coverity. The original tools only handled C. Heretic carries the same ideas to
Python, with no annotations and no configuration.

```console
$ pip install pyheretic
$ heretic tests/fixtures/demo
== MUST beliefs contradicted (code disagrees with itself; ranked by path simplicity) : 9 ==

[null-belief] app/null_cases.py:3  (use_then_check)  #55682f7c293f
    `user` is compared to None here, but every path to this check already dereferenced it
    (e.g. line 2). Either the check is dead or the earlier use can raise on None.
    path: lines 2 -> 3

== MAY beliefs violated (deviations from what most of the codebase does; ranked by z) : 7 ==

[ret-check] app/callers.py:26  (d)  #495512c4fb81
    Result of `lookup_user()` is used without a None check, but 4 of 6 call sites check it.
    `lookup_user` returns None at app/lookup.py:6.
    evidence: checked=4, sites=6, z=-1.91
    obeyed at: app/callers.py:5, app/callers.py:12, app/callers.py:19
```

## The idea

Most bug finders need you to write down the rules. Engler's observation was
that **the code already states the rules, many times over**, and a mistake is a
place where the code disagrees with itself.

* **MUST beliefs** are implied directly by the code. `user.name` means the
  author believes `user` is not None. `if user is None` means they believe it
  might be. If both appear on one path, one of them is wrong. Heretic reports
  this without needing to know which one.
* **MAY beliefs** are habits that might be coincidence. If 41 of 43 callers of
  `lookup_user()` check the result for None, the other 2 are probably bugs. If
  2 of 43 check, the checks are probably just defensive. Heretic counts
  obeyed sites (e) against all sites (n) and ranks deviations by Engler's
  z-statistic, `z = (e/n − p0) / √(p0(1−p0)/n)` with p0 = 0.9. It does not use
  a threshold. Read from the top and stop when the reports stop being bugs.

## Checkers

| checker | kind | what contradicts what |
|---|---|---|
| `null-belief` | MUST | `v` compared to None, then dereferenced on the None path; or `v` dereferenced on *every* path before a later `is None` check |
| `unawaited-coroutine` | MUST | `async def f` called, and the coroutine is dropped without being awaited |
| `return-annotation` | MUST | `-> User` declared, but a path returns None or falls off the end |
| `lock-release` | MUST | `acquire()` without `release()` on some path that returns normally |
| `blocking-in-async` | RULE / MAY | `time.sleep`, `requests.*`, `subprocess.*` inside `async def`; also functions that other code offloads with `to_thread` / `run_in_executor` but this code calls directly |
| `ret-check` | MAY | return value used without the None check most other callers do |
| `pairing` | MAY | `a()` called without the `b()` that nearly every other caller of `a()` follows it with |
| `org-scope` | MAY | query on a model with an `org_id` column that is not filtered by `org_id` |

`heretic --list-checkers` prints the list.

## How it works

```
 phase 1  index     every def: async?, return annotation, may-return-None summary
                    (interprocedural fixpoint), ORM models, org-scoping helpers,
                    functions offloaded to threads
 phase 2  check     per function, per checker:
                      state-machine checkers run on the xgcc-style path engine
                      statistical checkers record evidence (obeyed / violated)
 phase 3  rank      aggregate evidence across the codebase, z-rank MAY findings,
                    generic-rank MUST findings, fingerprint, suppress
```

The path engine follows xgcc (Hallem et al., PLDI 2002):

* **Per-function CFGs.** `a and b` is lowered to two branches, so short-circuit
  guards are real control flow. `try/finally` runs `finally` on early returns.
  `sys.exit()` and similar calls end the path, like `panic()` did in the kernel.
* **Depth-first path search with block-level caching.** Each (node, checker
  state) pair is visited once, which also bounds loops.
* **killvars.** Rebinding `v` (or a prefix such as `self` for `self.client`)
  discards what was known about it.
* **False-path pruning.** A branch outcome that contradicts an earlier test on
  the same path is never explored.

Checkers are written in a small Python version of the **metal** state-machine
language:

```python
class NullBeliefSM(SM):
    name = "null-belief"
    rules = [
        Rule("start",  IsNone(), true="null"),
        Rule("start",  Deref(),  dst="derefd"),
        Rule("derefd", IsNone(), dst="stop", err="`{v}` compared to None after deref at line {t}"),
        Rule("null",   Deref(),  dst="stop", err="`{v}` dereferenced while None (tested at line {t})"),
    ]
```

See [docs/DESIGN.md](docs/DESIGN.md) for the mapping from each paper to the code
and the decisions made along the way.

## Using it

```console
heretic .                              # analyze the current project
heretic . -c null-belief,ret-check     # only some checkers
heretic . -x 'tests/*' -x 'scripts/*'  # skip paths (protobuf stubs and migrations are skipped by default)
heretic . -n 100                       # show more findings per section
heretic . -f sarif -o heretic.sarif    # for GitHub code scanning
heretic . -f json                      # machine-readable output
```

**Triage.** Every finding has a fingerprint (`#495512c4fb81`) that does not
change when lines move. To suppress false positives, add their fingerprints to
`.heretic-ignore` at the project root, one per line, with optional `# reason`
comments.

**Exit code.** 1 if any MUST finding remains, otherwise 0, so it can gate CI
on contradictions while MAY findings stay advisory.

## Lessons carried over from the papers

* **Unsound on purpose.** Heretic is a bug finder, not a verifier. It skips
  cases it cannot model, such as aliasing and dynamic dispatch, so that the
  reports it does produce are worth reading.
* **Rank, don't threshold.** Users read from the top and stop after a few
  false positives ("Z-Ranking", SAS 2003), so ordering matters more than
  filtering.
* **Explain every report.** Each MUST finding comes with the path that leads to
  it. Each MAY finding comes with its counts and examples of code that follows
  the rule ("A Few Billion Lines of Code Later", CACM 2010).
* **No bug is too foolish to check for.** The checkers are short. Adding one is
  cheap.

## Development

```console
pip install -e '.[dev]'
pytest
```

`tests/fixtures/demo` holds a small project with planted bugs and known-good
look-alikes. The test suite requires that every planted bug is found and
nothing else is reported.

## References

* D. Engler, B. Chelf, A. Chou, S. Hallem. *Checking System Rules Using System-Specific, Programmer-Written Compiler Extensions.* OSDI 2000.
* D. Engler, D. Chen, S. Hallem, A. Chou, B. Chelf. *Bugs as Deviant Behavior: A General Approach to Inferring Errors in Systems Code.* SOSP 2001.
* S. Hallem, B. Chelf, Y. Xie, D. Engler. *A System and Language for Building System-Specific, Static Analyses.* PLDI 2002.
* T. Kremenek, D. Engler. *Z-Ranking: Using Statistical Analysis to Counter the Impact of Static Analysis Approximations.* SAS 2003.
* D. Engler, K. Ashcraft. *RacerX: Effective, Static Detection of Race Conditions and Deadlocks.* SOSP 2003.
* Y. Xie, D. Engler. *Using Redundancies to Find Errors.* IEEE TSE.
* A. Bessey et al. *A Few Billion Lines of Code Later: Using Static Analysis to Find Bugs in the Real World.* CACM 2010.

Papers: <https://web.stanford.edu/~engler/>

## License

MIT
