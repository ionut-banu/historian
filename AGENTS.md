A SQL query engine over git history. See `_docs/spec.md` for what it
is and `_docs/decisions.md` for why. Tasks live as GitHub issues; see
`_docs/process.md` for the per-task workflow.

Commands

- `uv sync` - install dependencies
- The first `uv run` downloads the pinned managed Python (`.python-version`);
  a run on any other SQLite aborts at start (`tests/conftest.py`)
- `uv run pytest` - the whole suite
- `uv run pytest tests/test_parser.py` - one test file
- `uv run historian "SELECT ..."` - run a query against the repo you
  are standing in
- `uv run pytest tests/differential` - the differential suite:
  historian against SQLite over the same rows; run during
  development and as the check that nothing regressed
- `uv run python tests/oracle.py "<setup SQL>" "<query SQL>"` - ask
  the oracle (Python's bundled `sqlite3` module) an ad hoc question;
  setup is optional - see `_docs/process.md`, "The oracle"
- `uv run python -m tests.fixtures.build large` - build the opt-in
  `large` benchmark fixture (`uv run pytest --build-large` too)
- `HISTORIAN_SWEEP_OPERATORS=3 uv run pytest
  tests/differential/test_evaluation_order.py` - the full 3-operator
  evaluation-order sweep (about 3.5 minutes); run it at milestone
  boundaries and when #117 lands

Layout

- `src/historian/` - the package (importable as `historian`)
- `src/historian/catalog.py` - the table catalog: the one module that
  imports each table directly and builds the `SCHEMAS`/
  `SCAN_FACTORIES` views the binder and planner consume. Imported
  only by `cli.py` and by tests that want the real catalog.
- `src/historian/ascii.py` - the two ASCII-only text predicates
  (`is_ascii_digit`, `ascii_fold`) SQLite uses in place of Python's
  Unicode-aware rules. Imports nothing beyond the stdlib, so every
  layer - lexer, binder, expression evaluator - can import it without
  creating a cycle or pulling in git/subprocess.
- `src/historian/sql/walk.py` - the shared expression-tree walks: a
  node's children, rebuilding a node around new children, shape
  equality, `contains_aggregate`, the aggregate-query predicate, and
  `BoundColumnRef`. Adding a field to an expression node is one edit
  here, and `tests/test_walk.py` fails until it is made. Imports only
  the stdlib, `historian.ascii`, `sql/ast.py` and `sql/lexer.py`.
- `src/historian/sql/binder.py` - `bind()` and one plain function per
  step of the documented resolution order; re-exports the public
  names. Imports the four modules below, which are layered, each
  importing only those before it:
  - `sql/bound.py` - `BindError` and the bound statement types
  - `sql/bind_expr.py` - name resolution and single-expression binding
  - `sql/bind_clauses.py` - per-clause binding: ordinals, LIMIT/OFFSET,
    GROUP BY, ORDER BY, the select list
  - `sql/grouped.py` - the grouped and DISTINCT narrowing checks
- `tests/` - pytest tests, one file per module under test
- `tests/extraction/` - tests checking the git-backed tables against
  `git` itself, e.g. `test_blame.py`
- `tests/differential/` - historian against SQLite: the harness in
  `conftest.py` and the hand-written differential tests
- `tests/pushdown/` - work-done tests for scan pushdown: which paths
  were blamed and how many git invocations were made
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
- `_docs/task-template.md` - the format a groomed issue body must
  be in
- `_docs/team/pm.md` - the PM role: grooms a task before anyone
  implements it
- `_docs/team/software-engineer.md` - the engineer role: implements
  one groomed task at a time
- `_docs/team/qa-engineer.md` - the QA role: checks finished work
  against the issue that specified it
- `_docs/team/reviewer.md` - the reviewer role: reads a milestone
  of code and says what is wrong with it

Anyone working one of the four roles above - PM, engineer, QA,
reviewer - reads its own role file before doing anything else.
