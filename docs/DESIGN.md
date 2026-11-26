# Design: from metal/xgcc to Python

Heretic translates the C bug-finding systems from Dawson Engler's group into
Python. This document maps each idea to the code and records where the
translation differs from the original.

## Map

| Paper idea | Where it lives |
|---|---|
| metal state machines: patterns, per-variable instances, `true=`/`false=` branch transitions, `$end_of_path$` (OSDI'00, PASTE'02) | `heretic/metal.py`: `SM`, `Rule`, `Deref`, `IsNone`, `NullTest`, `CallOn`, `AssignCall`, `Escape`, `End` |
| xgcc depth-first path search with block-level caching on (block, state) (PLDI'02) | `metal.run`: the `seen` set over `(node.id, state)` |
| killvars: redefining a variable kills its state | `metal.step`: assign events drop matching instances and dotted descendants |
| synonyms / volatility of field state across calls | `SM.volatile_attrs`: a call or escape through `obj` kills `obj.*` instances |
| false-path pruning (PLDI'02, TSE redundancy paper) | `metal._edge_ok`: branch facts carried along the path, refuted edges skipped, facts killed on reassignment; tiny congruence "None is falsy" |
| `kill_paths` for `panic()` / `BUG()` | `cfg.NORETURN_CALLS`; `assert False`; `raise` |
| Interprocedural summaries | `project.build_index`: may-return-None fixpoint through `return f(...)` |
| MUST beliefs and internal consistency (SOSP'01 section 4) | `checkers/null_belief.py`, `checkers/return_annotation.py`, `checkers/unawaited.py` |
| MAY beliefs and statistical inference: can-fail functions, a→b pairs (SOSP'01 sections 6–7) | `checkers/ret_check.py`, `checkers/pairs.py`, `checkers/org_scope.py`, the inferred half of `checkers/blocking_async.py` |
| z-statistic ranking, p0 = 0.9 (SOSP'01; Z-Ranking SAS'03) | `report.z_score` |
| Generic ranking: shorter distance and fewer conditionals rank higher, one conditional counts as 10 lines (PLDI'02) | `report.path_rank` |
| Latent specifications from names ("lock", "free") | `pairs._looks_paired` |
| System-specific rules (one person writes the check, everyone's code obeys) | `checkers/org_scope.py`, `checkers/blocking_async.py` |
| Stable triage across edits (CACM'10: "churn" and history matching) | `Finding.finalize`: fingerprints ignore line numbers; `.heretic-ignore` |
| Explaining errors (CACM'10) | path traces on MUST findings; counts plus examples of obeying code on MAY findings |

## Where Python differs from C

* **None instead of NULL.** Only `is None` / `== None` create MUST beliefs.
  Truthiness tests (`if not items`) do not, because an empty list is falsy and
  still safe to dereference. Truthiness *does* count as a check in the MAY
  `ret-check` checker, because programmers use it that way.
* **Dereference** covers attribute load or store, subscript, method call
  receiver, and iteration (`for x in v`).
* **Guarded sub-expressions.** `x and x.a`, `x.a if x else d` and
  `x is None or x.a` are lowered or guarded, so the dereference only happens
  under the test.
* **`await` takes the place of interrupt and lock discipline.** The kernel rule
  "don't block with interrupts disabled" becomes "don't block inside
  `async def`", and the "a() must be followed by b()" idea becomes "a
  coroutine must be awaited".
* **Annotations are beliefs too.** `-> User` is a written-down MUST belief, and
  Heretic cross-checks it against the body.
* **No alias analysis.** This matches xgcc. Keys are syntactic access paths
  (`self.client`). This is unsound in both directions, and that is accepted.

## Known limitations

* Exception edges are approximated: from the start of the `try` and from each
  top-level statement in the try body into every handler.
* Method resolution is by name, plus `self`/`cls` resolution within the
  enclosing class. There is no type inference.
* `ret-check` and `pairing` merge unrelated functions that share a name, as
  SOSP'01 did. The z-statistic pushes noisy merged populations down the
  ranking rather than removing them.
* There is a budget of 20,000 states per function per checker. Pathological
  functions are truncated rather than failing the run.

## Adding a checker

1. Subclass `heretic.checkers.Checker`. Implement `function(fx)` to emit
   findings directly. For statistical checkers, also implement `end_file()`
   and `aggregate()`.
2. For path-sensitive checks, write an `SM` and call `fx.run_sm(MySM(), store)`.
3. Register it in `heretic/checkers/__init__.py:all_checkers`.
4. Add planted bugs and known-good look-alikes to `tests/fixtures/demo` and
   update `EXPECTED` in `tests/test_fixtures.py`.
