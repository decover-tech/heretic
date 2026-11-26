# Contributing

Bug reports with a minimal snippet are the most useful contribution, and false
positives count too. Every checker change should come with planted bugs and
known-good look-alikes in `tests/fixtures/demo`.

```console
pip install -e '.[dev]'
pytest
heretic heretic        # dogfood: heretic must stay clean on itself
```

Guidelines, taken from the papers this project is based on:

* Prefer fewer, clearer reports to more reports. A finding that is hard to
  explain is treated as a false positive.
* MUST checkers report contradictions only. Anything statistical is a MAY
  checker and is ranked, not thresholded.
* Keep checkers small. If one needs a lot of analysis, demote its findings.
