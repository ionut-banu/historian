A SQL query engine over git history. See `_docs/spec.md` for what it
is and `_docs/decisions.md` for why. Tasks live as GitHub issues; see
`_docs/process.md` for the per-task workflow.

Commands

- `uv sync` - install dependencies
- `uv run pytest` - the whole suite
- `uv run pytest tests/test_parser.py` - one test file
- `uv run pytest tests/differential` - the SQLite oracle suite
- `uv run python -m historian.fuzz --queries 1000` - the fuzzer
- `uv run historian "SELECT ..."` - run a query against the repo you
  are standing in

Layout

- `src/historian/` - the package (importable as `historian`)
- `tests/` - pytest tests, one file per module under test
- `tests/differential/` - queries run against both historian and
  SQLite, asserting identical results
- `tests/fixtures/` - scripts that build git repositories with known,
  asserted contents

Rules

- Dependencies are added in `pyproject.toml`. Do not add one without
  asking.
- SQL text must never reach `eval` or `exec`, including with a
  restricted globals dict. It is parsed into an AST and evaluated by
  walking it.
- Where historian and SQLite disagree, SQLite is right. This is not a
  guideline, it is the definition of correct. See §1 of the spec.
- The parser, the planner and the executor are plain Python with no
  git and no subprocess imports. Only scan operators touch git, so
  everything above them is testable against in-memory rows.
- Query results are deterministic: the same repository and the same
  query always produce the same rows in the same order.
- Pushdown may only reduce the rows a scan produces to a superset of
  the matching rows. The Filter operator above it is never removed.
- Every new query shape becomes a differential test.
- Keep the operator layer explicit and boring. No metaclasses, no
  dynamic dispatch tricks, no clever descriptors. This code is
  intended to be portable to Rust later, and anything that leans on
  Python's dynamism has to be redesigned rather than translated.

Documents

- `_docs/spec.md` - the only specification, always current
- `_docs/decisions.md` - why things were decided, dated, append-only
- `_docs/process.md` - how work is organized
