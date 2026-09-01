# historian

A SQL query engine over git history.

This document describes what historian is, currently. It is edited
whenever a decision changes it, and it is always true of the code as
it stands. Issues name the section they implement.

Why a thing was decided, and what it replaced, belongs in
`_docs/decisions.md` - dated entries, never edited.

§1 and §2 are settled. §3 onward are listed at the bottom and not yet
designed.

---

## §1 Scope and definition of done

### What historian is

A command-line SQL engine that treats a git repository as a database.
It parses SQL, plans it, and executes it by reading git objects on
demand. There is no load step and no materialized copy of the repo.

```
$ historian "SELECT author_name, count(*) FROM blame
             WHERE path LIKE 'src/auth/%' GROUP BY author_name"
```

### Why it exists

Honesty first, because it shapes every decision below.

For queries over commit metadata, historian is not the best tool and
never will be. Dumping `git log` into SQLite is a few hundred lines and
gives faster, more correct answers. Any design that lands historian in
that territory has failed.

historian earns its existence on tables that **cannot be materialized
in advance**: `blame`, and later `diffs`. Blame for one file at one
revision costs a walk of that file's history; blame for every file at
every revision is combinatorial. It can only be computed lazily, for
exactly the paths a query's predicates select. That requires an engine
that decides what to compute — which is the thing being built.

The secondary purpose is explicit: this is a demonstration of
engineering capability. Parser, planner, optimizer, executor, and a
correctness regime that a stranger can verify by cloning the repo and
running one command.

### Non-goals

Declared here and repeated in the README, so scope creep has to argue
with a document:

- Not a general SQL database. No `CREATE`, `INSERT`, `UPDATE`,
  `DELETE`, transactions, indexes, or persistence.
- Not a competitor to DuckDB or SQLite for tabular data.
- Not a git client. It reads history; it never writes to a repo.
- Not multi-repo. One repository per invocation.

### v1 grammar

```
SELECT   [DISTINCT] <expr> [AS alias], ...
FROM     <table> [INNER JOIN <table> ON <cond> | USING (<col>)]
WHERE    <predicate>
GROUP BY <expr>, ...
HAVING   <predicate>
ORDER BY <expr | ordinal> [ASC | DESC], ...
LIMIT    <n> [OFFSET <n>]
```

Expressions: column references, literals, `AND`/`OR`/`NOT`, comparison
operators, `LIKE`, `IN`, `BETWEEN`, `IS NULL`/`IS NOT NULL`, arithmetic,
string concatenation, `CASE`.

Aggregates: `count`, `sum`, `avg`, `min`, `max`.

Scalar functions: a deliberately small set, chosen when the queries
need them rather than up front.

**Semantics follow SQLite exactly**, including three-valued logic with
`NULL`, type affinity, and comparison coercion. Where this specification
and SQLite disagree, SQLite is correct. See "definition of done".

### Explicitly out of scope for v1

Subqueries · CTEs · window functions · `UNION`/`INTERSECT`/`EXCEPT` ·
outer and cross joins · correlated anything · user-defined functions.

Each may become v2. None may quietly become v1.

### Build order

Ordered so that the first shipped milestone does something no other
tool does, rather than doing badly what SQLite does well.

| Phase | Tables | Grammar added | First query it unlocks |
|---|---|---|---|
| 1 | `blame` | SELECT, WHERE, GROUP BY, aggregates, ORDER BY, LIMIT | surviving-line ownership |
| 2 | `commits`, `commit_files`, `refs`, `tree` | date/author/path pushdown, LIMIT pushdown | churn, hotspots, stale files |
| 3 | — | `INNER JOIN`, self-joins | hidden coupling (co-change) |
| 4 | `diffs` | pickaxe pushdown | "every commit that introduced this call" |

Phase 1 is the load-bearing decision. Because `blame` is too expensive
to materialize, predicate pushdown is a feasibility requirement in week
one rather than an optimization added in month three. The scan
interface therefore has its final shape from the first commit, and
every later table inherits it.

### Definition of done

Done is a number, produced by machines, reproducible by strangers.

**Layer 1 — differential testing against SQLite.** The primary
correctness regime. For a fixture repository, the equivalent data is
extracted into a SQLite database. Every test query runs through both
historian and SQLite, and the results must be identical.

This makes SQLite the specification. "Correct" is not a judgment call,
it is agreement with the most heavily tested SQL implementation in
existence. Every query anyone thinks of becomes a permanent regression
test at no cost.

**Layer 2 — a query fuzzer.** Hand-written differential tests only
cover cases someone thought of, and the surviving bugs are exactly the
ones nobody thought of. A generator produces random queries within the
declared grammar; each is run through both engines and diffed. Random
generation has no blind spots because it has no beliefs.

**Layer 3 — extraction correctness.** Differential testing proves the
SQL is right, not that the git data is right — both engines read the
same extracted rows. So the extraction layer is tested separately,
against `git blame` / `git log` / `git show` output on fixture
repositories built by a script with known, asserted contents.

**v1 is done when:** phases 1–4 are complete, the differential suite is
green, the fuzzer runs a configured budget of generated queries without
a mismatch, and the README states the passing counts.

`sqllogictest` was considered and rejected: its corpus builds its own
tables with `CREATE TABLE` and `INSERT`, which historian will never
support, so running it would mean implementing a storage engine to
satisfy a test harness.

---

## §2 Tables

All tables are read-only and virtual. None is stored; each is produced
by a scan operator that reads git objects when the query runs.

### Types

Three types, matching SQLite's affinities so the oracle comparison is
exact: `TEXT`, `INTEGER`, `REAL`.

Timestamps are `TEXT` in ISO-8601 UTC (`2026-03-14T12:01:22Z`). This is
deliberate: SQLite has no date type, and ISO-8601 sorts and compares
chronologically as a string, so `authored_at > '2026-01-01'` behaves
identically in both engines with no conversion layer.

### The scan capability contract

Every table's scan declares which predicates it can use to reduce work:

```
capabilities() -> set[PushdownKind]
scan(pushed: list[Predicate]) -> Iterator[Row]
```

The planner walks the conjunctive terms of the `WHERE` clause, offers
each to the scan, and passes along the ones it accepts.

**Pushdown may only reduce the input to a superset of the matching
rows, and the `Filter` operator is never removed in v1.**

This refines what the earlier design discussion assumed. Letting a scan
claim a predicate is *exactly* satisfied — and deleting the filter
above it — makes every scan a place where wrong rows can reach the
user, and the differential suite would then be testing extraction and
planning at once. Keeping the filter costs one pass over an
already-small row set and confines pushdown bugs to performance.
Eliminating a provably redundant filter is a v2 optimization with its
own tests.

### `blame` — phase 1

One row per line of code alive at `HEAD`.

| column | type | meaning |
|---|---|---|
| `path` | TEXT | file path at HEAD |
| `line_no` | INTEGER | 1-based line number |
| `line` | TEXT | the line's content |
| `commit_hash` | TEXT | commit that last touched the line |
| `author_name` | TEXT | author of that commit |
| `author_email` | TEXT | |
| `authored_at` | TEXT | ISO-8601 UTC |

Pushdown:

| predicate | effect |
|---|---|
| `path = 'literal'` | blame exactly that file |
| `path IN (...)` | blame those files |
| `path LIKE 'prefix%'` | blame files under the prefix |
| anything else | residual filter |

`author_name`, `commit_hash`, `authored_at`, and `line_no` cannot push
down: a line's author is unknown until the file has been blamed.

Implementation: `git ls-tree -r HEAD --name-only` to enumerate
candidate paths, filtered by pushed path predicates, then
`git blame --line-porcelain` per surviving file, parsed into rows and
streamed. Blame is always at `HEAD` in v1; blaming at an arbitrary
revision is deferred.

Shelling out to `git blame` is deliberate. Rename detection and merge
handling are a month of work in a domain nobody is evaluating, and the
subject of this project is the engine above the scan.

### `commits` — phase 2

One row per commit reachable from `HEAD`.

| column | type |
|---|---|
| `hash` | TEXT |
| `author_name`, `author_email` | TEXT |
| `authored_at` | TEXT |
| `committer_name`, `committer_email` | TEXT |
| `committed_at` | TEXT |
| `subject` | TEXT |
| `message` | TEXT |
| `parent_count` | INTEGER |

Pushdown:

| predicate | effect |
|---|---|
| `hash = 'literal'` | direct object lookup, one row |
| `authored_at >`/`>=`/`<`/`<=` | `git log --since` / `--until` bounds the walk |
| `author_name`/`author_email` `=` | `git log --author` |
| `LIMIT n`, no `ORDER BY` or `ORDER BY authored_at DESC` | stop the walk after n rows |

The `LIMIT` case is the reason the planner has to see the whole query
rather than each operator in isolation: `git log` streams newest-first,
so a limit can terminate the walk instead of consuming all history.

### `commit_files` — phase 2

One row per file changed per commit.

| column | type |
|---|---|
| `hash` | TEXT |
| `path` | TEXT |
| `old_path` | TEXT, null unless renamed |
| `status` | TEXT: `A`, `M`, `D`, `R` |
| `additions`, `deletions` | INTEGER |

Pushdown: everything `commits` accepts, plus `path` predicates via
`git log -- <pathspec>`.

### `refs` — phase 2

| column | type |
|---|---|
| `name` | TEXT |
| `kind` | TEXT: `branch`, `tag`, `remote` |
| `target_hash` | TEXT |
| `is_head` | INTEGER, 0 or 1 |

Small enough to materialize on every scan. No pushdown.

### `tree` — phase 2

One row per file present at `HEAD`.

| column | type |
|---|---|
| `path` | TEXT |
| `mode` | TEXT |
| `blob_hash` | TEXT |
| `size` | INTEGER |

Pushdown: `path` equality and prefix `LIKE`.

### `diffs` — phase 4

One row per hunk per file per commit.

| column | type |
|---|---|
| `hash` | TEXT |
| `path` | TEXT |
| `hunk_no` | INTEGER |
| `old_start`, `new_start` | INTEGER |
| `added_text` | TEXT, added lines joined |
| `removed_text` | TEXT, removed lines joined |

Pushdown: everything `commit_files` accepts, plus
`added_text LIKE '%needle%'` and `removed_text LIKE '%needle%'` via
`git log -S<needle>`, git's pickaxe search. That one turns a full
history diff — unusable on a real repo — into a targeted walk, and is
the reason `diffs` is viable at all.

---

## §3 Engine architecture

Python 3.11+, `uv`, pytest.

### The pipeline

```
SQL text
  → lexer      tokens
  → parser     AST
  → binder     AST + resolved columns, errors for unknown names
  → planner    operator tree
  → optimizer  operator tree, with predicates pushed into scans
  → execute    rows
```

Only scan operators touch git. Everything above them consumes rows and
is testable against in-memory fixtures with no repository present.

### Modules

```
src/historian/
  values.py        SQL values, comparison, three-valued logic
  sql/lexer.py     text -> tokens
  sql/ast.py       AST node definitions
  sql/parser.py    tokens -> AST
  sql/binder.py    name resolution, unknown column/table errors
  plan/nodes.py    operator tree definitions
  plan/planner.py  AST -> operator tree
  plan/optimizer.py  pushdown negotiation
  exec/operators.py  the operators
  exec/expression.py expression evaluation against a row
  tables/          one scan per table: blame.py, commits.py, ...
  cli.py
```

### Values and three-valued logic

The foundation, and the largest single source of differential
mismatches. It gets its own module and its own tests before anything
depends on it.

A value is `None`, `int`, `float`, or `str`, mirroring SQLite's storage
classes. A predicate evaluates to `TRUE`, `FALSE`, or `NULL`.

Rules, all taken from SQLite rather than invented:

| case | result |
|---|---|
| `NULL = NULL`, `NULL < 1`, any comparison with `NULL` | `NULL` |
| `NULL AND FALSE` | `FALSE` |
| `NULL AND TRUE`, `NULL AND NULL` | `NULL` |
| `NULL OR TRUE` | `TRUE` |
| `NULL OR FALSE`, `NULL OR NULL` | `NULL` |
| `NOT NULL` | `NULL` |
| `x IS NULL`, `x IS y` | never `NULL`; always `TRUE` or `FALSE` |

`WHERE` and `HAVING` keep a row only when the predicate is `TRUE`.
`FALSE` and `NULL` are both rejected, and confusing the two is the
classic bug.

Ordering across types follows SQLite's storage-class order:
`NULL` < numeric < `TEXT`. An integer is always less than any string,
regardless of contents. Text compares bytewise.

**Column affinity is not part of this module**, and the split is easy
to get wrong. Comparing two values is one thing; comparing a *column*
to a literal is another, because SQLite first converts the literal to
the column's declared type:

```
5 = '5'                          FALSE   two literals, no conversion
WHERE line_no = '5'              TRUE    line_no is INTEGER, so '5' becomes 5
```

`values.py` sees only values and cannot know which operand came from a
column or what type it was declared as, so it implements the first line
and never the second. Affinity belongs to `exec/expression.py`, which
has the AST (so it knows which side is a column reference) and the
operator's schema (so it knows the declared type).

This matters more than one odd case suggests: `blame.line_no` is the
only non-`TEXT` column in phase 1, and §4's fuzzer is weighted toward
comparisons between different types - so this is a mismatch it will
generate early and often. Whoever grooms the expression evaluator owns
it, and it needs deciding before that work starts rather than being
discovered by the oracle.

**Numeric comparison is exact, and must never go through `float()`.**
SQLite compares an integer against a real without casting either to the
other:

```
9007199254740993 =  9007199254740992.0     FALSE
9007199254740993 >  9007199254740992.0     TRUE
float(9007199254740993) == 9007199254740992.0   would say TRUE
```

Past 2^53 a double cannot represent consecutive integers, so the cast
loses the distinction and reverses the answer. Python's own `int`/`float`
comparison is exact and agrees with SQLite on all three, so the correct
implementation is to compare the values directly and add nothing. Any
`float()` conversion introduced later for convenience - most plausibly
in the expression evaluator's arithmetic path - breaks this silently, on
inputs no hand-written test would think to try.

SQLite also has no NaN: `typeof(0.0/0.0)` is `null`, so a NaN can never
be a stored value. A computed NaN reaching `order_key` would violate the
total order, which is a risk for the expression evaluator rather than
for this module.

Aggregate edge cases, which differential tests will find immediately:

| case | result |
|---|---|
| `count(*)` over zero rows | `0` |
| `sum`, `avg`, `min`, `max` over zero rows | `NULL` |
| `count(x)` | counts non-`NULL` values only |
| `sum`/`avg`/`min`/`max` with some `NULL`s | `NULL`s ignored |
| `GROUP BY` a column containing `NULL`s | all `NULL`s form one group |
| `DISTINCT` over `NULL`s | `NULL`s are equal to each other |
| `ORDER BY` ascending with `NULL`s | `NULL`s sort first |

### AST

Frozen dataclasses, one per node. Expressions and statements are
separate hierarchies. The AST records source positions so errors can
point at the offending text.

### One plan representation, not two

Textbooks separate a logical plan from a physical plan, because one
logical operation can have several physical implementations. In v1
every logical operation has exactly one, so the split would be pure
ceremony: two parallel type hierarchies and a translation pass that
never makes a decision.

v1 therefore has a single operator tree, built by the planner and
rewritten by the optimizer. The split gets introduced when there is a
real choice to make - the first candidate is joins, once there is both
a hash join and a nested-loop join and something has to pick.

### Operators

Volcano-style iteration. Each operator pulls rows from its children.

```python
class Operator:
    schema: Schema                       # column names and types
    def rows(self) -> Iterator[Row]: ...
```

A `Row` is a tuple of values. The schema lives on the operator, not in
the row, so rows stay cheap and column references resolve to integer
offsets at bind time rather than by name at runtime.

Phase 1 operators: `Scan`, `Filter`, `Project`, `Aggregate`, `Sort`,
`Limit`, `Distinct`. Phase 3 adds `HashJoin`.

`Aggregate` handles both the grouped case and the whole-table case,
which differ in one respect that matters: with no `GROUP BY` and no
rows, the whole-table case still emits exactly one row.

Generators are used inside `rows()`, but the operator is an object
rather than a bare generator function, so the tree can be inspected,
printed by `EXPLAIN`, and asserted on in tests.

### Expression evaluation

`exec/expression.py` walks an expression against a row and returns a
value. It is a plain function over `(expr, row, schema)` with no git,
no I/O, and no operator dependencies.

Aggregate calls are not evaluated here. The planner splits each
`SELECT` and `HAVING` expression into aggregate calls, computed by the
`Aggregate` operator, and the surrounding scalar expression, computed
here over the aggregate's output row.

### Pushdown negotiation

The optimizer's one job in v1.

1. Split the `WHERE` predicate on `AND` into conjunctive terms. `OR`
   is not split - a disjunction is one term, and pushes down only if a
   scan accepts the whole thing.
2. Offer each term to the scan beneath it. The scan returns the terms
   it can use.
3. Pass the accepted terms to the scan as scan arguments.
4. Leave the `Filter` in place, unchanged, with every term still in it.

Step 4 is deliberate and is the subject of a decision entry. A scan
that claims a term is exactly satisfied lets the filter above it be
deleted, and then every scan becomes a place where wrong rows reach
the user. Keeping the filter costs one pass over an already-reduced
row set, and confines any pushdown bug to performance rather than
correctness.

The consequence for tests is that pushdown has two distinct failure
modes and they need different tests. Pushdown that returns the *wrong*
rows is caught by the differential suite, provided the SQLite side is
loaded from an unfiltered scan - see §4. Pushdown that returns the
right rows while doing all the work is invisible to any assertion on
results, and is caught only by asserting on the work done: which paths
were blamed, how many git invocations were made. Scans record this,
and tests read it.

`LIMIT` pushdown is the same negotiation applied to a different part
of the query, and the reason the optimizer sees the whole tree rather
than one node at a time: `git log` streams newest-first, so `LIMIT n`
with no `ORDER BY`, or with `ORDER BY authored_at DESC`, can stop the
walk instead of consuming all history.

### Errors

Three kinds, all of them the user's fault and none of them tracebacks:

- **Parse errors** name the position and what was expected.
- **Binding errors** name the unknown column or table, and list what
  is available.
- **Unsupported grammar** says the feature is not supported and points
  at the non-goals in §1. A query using a window function gets told
  window functions are out of scope, not a syntax error.

Do not invent runtime type errors. SQLite is permissive - comparing a
string to an integer is a valid comparison with a defined answer, not
a failure. Every error historian raises that SQLite does not is a
differential mismatch.

### Determinism and row order

SQLite does not guarantee row order without `ORDER BY`. historian does:
the same repository and query always produce the same rows in the same
order, because reproducibility matters more here than the freedom to
reorder.

This makes the two engines legitimately disagree on order for queries
without `ORDER BY`, so the differential harness compares results as
sorted multisets unless the query has an `ORDER BY`, in which case
order is compared exactly.

---

## §4 Test architecture

Four layers, each answering a question the others cannot.

| layer | question | oracle |
|---|---|---|
| unit | does this function do what it says? | assertions |
| extraction | does the git data match the repository? | `git` itself |
| differential | is the SQL correct? | SQLite |
| pushdown | did the scan actually avoid the work? | work counters |

### Fixture repositories

Built by `tests/fixtures/build.py`, never committed as binaries.

**They must be byte-identical on every machine and every run.** Git
hashes derive from author, committer, timestamps and tree - but also
from the ambient git configuration a build inherits. The six `GIT_*`
variables below are necessary but not sufficient: on a real
contributor machine, `core.autocrlf` alone was observed to change a
blob's - and therefore a commit's - hash, and `init.defaultBranch`
changes the branch name, independent of these six variables entirely.

```
GIT_AUTHOR_NAME, GIT_AUTHOR_EMAIL, GIT_AUTHOR_DATE
GIT_COMMITTER_NAME, GIT_COMMITTER_EMAIL, GIT_COMMITTER_DATE
```

So the builder isolates itself from all ambient git configuration
rather than overriding known settings one at a time - a list of
settings to override can always be missing an entry, while an
environment that inherits nothing is closed by construction. It sets
`GIT_CONFIG_GLOBAL` and `GIT_CONFIG_SYSTEM` to `/dev/null` and
`GIT_CONFIG_NOSYSTEM=1` before any git command runs (the third is
required in addition to the second on at least one real platform:
Apple's Command Line Tools git reads its own hardcoded system-scope
config regardless of `GIT_CONFIG_SYSTEM`), always passes an explicit
branch name to `git init` rather than relying on `init.defaultBranch`,
and sets `core.autocrlf`, `core.fileMode`, `core.symlinks`,
`core.ignoreCase`, `commit.gpgsign`, and `core.safecrlf` explicitly on
the fixture repository as defense in depth. Without this, hashes and
branch names differ per contributor machine, hash assertions are
flaky, and no reported failure reproduces on anyone else's machine.

Three fixtures:

- **tiny** — a handful of commits, two authors, a rename, a deletion,
  and a merge. The default for most tests, small enough to reason
  about by hand.
- **awkward** — built to break things: unicode in paths and author
  names, a space and a quote in a path, an empty file, a binary file,
  a file with no trailing newline, a file deleted and later recreated,
  an empty commit message, and a line of content that looks like git
  porcelain output.
- **large** — generated, hundreds of commits. Benchmarks only, never
  correctness.

The fixture builder asserts what it built. A fixture that silently
stops containing a merge commit takes a whole class of tests with it.

### The differential harness

For each test query:

1. Scan the table **with pushdown disabled**, giving the complete set
   of rows.
2. Load those rows into an in-memory SQLite table with the same schema.
3. Run the query through SQLite.
4. Run the query through historian, with pushdown enabled.
5. Compare.

**Step 1 is the load-bearing one.** The obvious implementation feeds
SQLite from the same scan historian uses, which is wrong: a pushdown
bug that wrongly drops rows would remove them from both sides, both
engines would agree, and the bug would be invisible. Loading SQLite
from an unfiltered scan means historian is free to be clever and any
cleverness that changes the answer is caught.

Comparison follows §3: sorted multisets unless the query has an
`ORDER BY`, exact order when it does.

This layer tests the SQL engine, not the extraction — both sides read
the same extracted rows, so a wrong `authored_at` is wrong in both.
That is what the extraction layer is for.

### The extraction layer

Asserts that the git tables report what git reports, on fixtures whose
contents are known. `blame` rows for a file are checked against
`git blame --line-porcelain` for that file; `commits` against
`git log`; `commit_files` against `git show --numstat`.

Deliberately boring and deliberately separate. Nothing here involves
SQL.

### The fuzzer

`uv run python -m historian.fuzz --queries N --seed S`

**It generates ASTs and renders them to SQL, rather than generating
text.** Every query is therefore well-formed by construction, and any
mismatch is a semantic bug rather than a parser accident.

**Weighted toward what breaks.** Uniform generation over the grammar
produces `SELECT path FROM blame LIMIT 1` forever and finds nothing.
The generator biases toward:

- `NULL` literals, and columns known to contain `NULL`
- comparisons between different types — integer against text
- predicates that match zero rows, and `LIMIT 0`
- aggregates over zero rows, and over groups containing `NULL`
- `NOT` wrapped around something that can be `NULL`
- `OR`, which never pushes down, exercising the residual path
- one pushable predicate `AND` one that is not, in the same `WHERE`
- `GROUP BY` and `DISTINCT` on columns containing `NULL`

**Seeded and reproducible.** A reported mismatch names its seed, and
that seed reproduces it exactly. A failure QA cannot hand to the
engineer in reproducible form is not worth reporting.

**Shrinking, before reporting.** A raw mismatch is a twelve-clause
query nobody can read. The shrinker removes clauses, simplifies
expressions, and replaces literals with simpler ones, keeping any
reduction that still mismatches. What gets reported is the smallest
query that still fails.

**Every shrunk mismatch becomes a permanent differential test**, in
`tests/differential/test_regressions.py`, with the seed that found it
in a comment. This is the flywheel: the fuzzer finds a bug, shrinking
makes it legible, it becomes a test that can never regress, and the
differential count goes up. QA's count going up is the process working
as designed.

### The pushdown layer

Differential testing catches pushdown that returns the **wrong** rows.
It cannot catch pushdown that returns the right rows while doing all
the work, because the results are identical either way. Both failures
are real and they need different tests.

Scans record what they did — which paths were blamed, how many git
invocations were made — and pushdown tests assert on that record:

```
blame with WHERE path = 'src/a.py'       blamed exactly ['src/a.py']
blame with WHERE path LIKE 'src/%'       blamed only paths under src/
blame with WHERE author_name = 'Ana'     blamed everything, correctly
commits with LIMIT 5, no ORDER BY        walked at most 5 commits
```

The third case matters as much as the first: a predicate that cannot
push down must still produce correct results, and the test that proves
it is the one asserting the scan did *not* try to be clever.

The record is exposed on the scan object, not through global state, so
tests observe it without a mechanism that could alter behaviour.

### Layout

```
tests/
  test_values.py          three-valued logic, comparison, coercion
  test_lexer.py
  test_parser.py
  test_planner.py
  test_optimizer.py
  test_operators.py       against in-memory rows, no repository
  extraction/
    test_blame.py         vs git blame --line-porcelain
    test_commits.py       vs git log
  differential/
    conftest.py           the harness
    test_blame.py         hand-written cases
    test_regressions.py   shrunk fuzzer findings, seed in a comment
  pushdown/
    test_blame_pushdown.py
  fixtures/
    build.py
```

---

## §5 The command line

```
historian [OPTIONS] [QUERY]

  -C, --repo PATH     repository to query (default: current directory)
  -f, --file PATH     read the query from a file
      --format FMT    table | csv | tsv | json   (default: table)
      --explain       print the plan, do not run it
      --stats         print the work done, after the results
      --no-pushdown   disable pushdown
```

With no query and a terminal attached, it starts a REPL.

### Formats

`table` is aligned and human-facing. `csv` is RFC 4180. `json` is an
array of objects. `tsv` is for pasting elsewhere.

**The format never depends on whether stdout is a terminal.** Tools
that switch format when piped break scripts that were developed
interactively. Only colour and paging depend on the terminal.

`NULL` renders as the word `NULL` in `table`, dimmed when colour is
available; as an empty field in `csv` and `tsv`; as `null` in `json`.
The ambiguity between `NULL` and the four-character string `'NULL'` is
accepted in `table` and resolved by `--format json` when it matters.

Results go to stdout, everything else to stderr, so piping works.

### `--explain`

Prints the operator tree with what the optimizer decided:

```
Sort (count(*) DESC)
  Aggregate (group=[author_name], aggs=[count(*)])
    Filter (path LIKE 'src/auth/%')
      BlameScan (pushed: path LIKE 'src/auth/%' -> 12 of 4013 paths)
```

It is a debugging tool, a test surface, and the clearest single
demonstration of what the project does. The `Filter` still appearing
above a scan that already pushed the same predicate is correct and
expected - see §3.

A flag rather than an `EXPLAIN` keyword, so the grammar stays exactly
what §1 declares.

### `--stats`

```
12 paths blamed, 4013 skipped
13 git invocations
0.31s
```

The same counters the pushdown tests assert on, printed. Whatever
proves pushdown works in a test should be visible to a user.

### Errors

Never a traceback. Position, cause, and what would have been valid:

```
error: no such column: authr_name

  SELECT authr_name FROM blame
         ^

  blame has: path, line_no, line, commit_hash, author_name,
             author_email, authored_at
```

Unsupported grammar says so plainly rather than reporting a syntax
error, and points at §1:

```
error: window functions are not supported
  historian implements a subset of SQL. See the non-goals in
  _docs/spec.md §1.
```

Exit codes: `0` success, `1` bad query, `2` bad usage, `3` the
repository could not be read.

### REPL

Deliberately small. Multi-line input until a semicolon, history,
`.tables`, `.schema <table>`, `.quit`. No completion, no paging, no
configuration.

---

## §6 Milestones

Six milestones. Each one ends with something that can be run and
shown, because a milestone that cannot be demonstrated cannot be
verified either.

Issues are filed from this list and groomed by the PM before
implementation, per `_docs/process.md`. Each names the section it
implements.

### M1 — Foundations

No demo. The parts everything else sits on.

1. Project skeleton with a passing test — `uv`, pytest, `src/historian/`
2. SQL values, comparison, three-valued logic — §3
3. Lexer — §3

Issue 2 is the one to slow down on. Every rule in §3's tables gets a
test, including the ones that look obvious. It is the single largest
source of differential mismatches later.

### M2 — The first query

Ends with: `historian "SELECT path, author_name FROM blame WHERE path = 'src/a.py'"`

4. AST and parser for `SELECT` / `FROM` / `WHERE` — §3
5. Binder: name resolution, unknown column and table errors — §3
6. Deterministic fixture repositories — §4
7. `blame` scan without pushdown, and its extraction tests — §2, §4
8. Operators: `Scan`, `Filter`, `Project`, over in-memory rows — §3
9. Planner and a CLI that runs a query end to end — §3, §5

### M3 — The query that justifies the project

Ends with: surviving-line ownership, the thing no other tool does off
the shelf.

```sql
SELECT author_name, count(*) FROM blame
WHERE path LIKE 'src/auth/%' GROUP BY author_name ORDER BY 2 DESC
```

10. The differential harness, loading SQLite from an unfiltered scan — §4
11. `Aggregate`: `count`, `sum`, `avg`, `min`, `max`, `GROUP BY`, `HAVING` — §3
12. `ORDER BY`, `LIMIT`, `OFFSET`, `DISTINCT` — §3

### M4 — Pushdown

Ends with: the same query, `--stats` showing 12 paths blamed instead
of 4,013, and a timing difference anyone can reproduce.

13. Scan capability negotiation and predicate splitting — §3
14. `path` pushdown into the `blame` scan, with work-done tests — §2, §4
15. `--explain` and `--stats` — §5

### M5 — The fuzzer

Ends with: a mismatch found, shrunk, and committed as a regression
test. Finding one is the milestone; a clean run means the generator is
too timid.

16. Query generator, AST-first and weighted toward `NULL` — §4
17. Shrinker, and the regression test workflow — §4

### M6 — Finish phase 1

18. Output formats and error presentation — §5
19. REPL — §5
20. README, with the differential and fuzz counts — §1

### After M6

These six milestones deliver **phase 1 of §1 only** — the `blame`
table, single-table queries, and pushdown. They are not v1. Phases 2
to 4 of §1's build order still remain: `commits`, `commit_files`,
`refs` and `tree`, then `INNER JOIN` and the co-change query, then
`diffs` with pickaxe pushdown. v1 is done when all four phases are,
per §1.

They get their own milestones, planned after M6 rather than now. The
engine will have been contradicted by the fuzzer several times by
then, and planning phase 2 today would mean planning it from
expectations rather than from what the oracle has taught us.
