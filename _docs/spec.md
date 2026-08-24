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

The consequence for tests: a pushdown cannot be verified by its
results, because results are identical with pushdown disabled. It is
verified by asserting on the work done - which paths were blamed, how
many git invocations were made. Scans record this, and tests read it.

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

## Not yet designed

- §4 Test architecture — fixture repos, the SQLite oracle harness, the
  fuzzer's generation grammar
- §5 CLI surface — output formats, `EXPLAIN`, REPL, error reporting
- §6 Milestones and issue breakdown
