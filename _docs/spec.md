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
operators, `LIKE [ESCAPE <expr>]`, `IN`, `BETWEEN`, `IS NULL`/`IS NOT
NULL`, arithmetic, string concatenation, `CASE`.

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
accepts(term: Predicate) -> bool
scan(pushed: list[Predicate]) -> Iterator[Row]
```

The optimizer walks the conjunctive terms of the `WHERE` clause,
offers each to the scan with `accepts()`, and passes along the ones
it accepts, in the order they were offered. A scan whose
`capabilities()` is empty is offered nothing. A `Predicate` is the
bound AST subexpression of one term, its column references resolved
against the scan's own schema; a `PushdownKind` is a label each table
names for itself. `accepts()` answers from the term's shape alone,
with no I/O.

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

Each literal must be a text literal, and a `LIKE` pattern's only
wildcard a single trailing `%` with no `NOT` and no `ESCAPE`; `NOT
IN`, a `NULL` or numeric literal, and a comparison with another column
are "anything else". Candidates are always taken from `ls-tree`'s own
output, never from the literals, so an untracked path is never
blamed. A `LIKE` prefix is matched ASCII-case-insensitively, exactly
as SQLite's `LIKE` is. Rows come in `ls-tree` order, except that
`path IN (...)` blames in the list's own order.

`author_name`, `commit_hash`, `authored_at`, and `line_no` cannot push
down: a line's author is unknown until the file has been blamed.

Implementation: `git ls-tree -rz HEAD --name-only` to enumerate
candidate paths, filtered by pushed path predicates, then
`git blame --line-porcelain` per surviving file, parsed into rows
and streamed. The `-z` form is required: plain `--name-only`
quote-escapes any path containing a space or a double quote, and
`git blame`'s own `filename` header re-quotes the same way with no
unquoted form to parse back out, so `path` always comes from the
enumeration instead. Blame is always at `HEAD` in v1; blaming at
an arbitrary revision is deferred.

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
  atof.py          text -> REAL by SQLite 3.50.4's own algorithm
  sql/lexer.py     text -> tokens
  sql/ast.py       AST node definitions
  sql/parser.py    tokens -> AST
  sql/binder.py    bind(): the resolution order, one function per step
  sql/bound.py     BindError and the bound statement types
  sql/bind_expr.py name resolution and single-expression binding
  sql/bind_clauses.py  per-clause binding: ordinals, LIMIT/OFFSET,
                   GROUP BY, ORDER BY, the select list
  sql/grouped.py   the grouped and DISTINCT narrowing checks
  sql/walk.py      shared expression walks: children, rebuild, shape
                   equality, aggregate detection; BoundColumnRef
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

Only a table column has an affinity. An aggregate call's result
(`count`, `sum`, `avg`, `min`, `max`) and a computed expression -
including a computed `GROUP BY` key such as `line_no + 10` - have
none, even where the planner turns them into a column reference into
`Aggregate`'s output row. That output schema declares such a column's
type as `None`, "no affinity", never one of the three table affinities
of §2, so `HAVING sum(line_no) > 3` compares integers and `count(*) =
'12'` is false. A bare-column `GROUP BY` key keeps its source column's
affinity. See `_docs/decisions.md`, 2026-09-27.

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

TEXT becomes a number in exactly one place, `exec/expression.py`'s
`_scan_number`, for arithmetic's leading-prefix coercion, column
affinity's whole-string coercion and `sum`/`avg`'s classification
alike - the one exception is the integer value `%` reads, below. The
whitespace skipped before a number (and after it, for column affinity's
whole-string coercion) is exactly the six ASCII characters space, `\t`,
`\n`, `\v`, `\f`, `\r` (0x20, 0x09, 0x0A, 0x0B, 0x0C, 0x0D), and never
between a sign and its digits: `'\v12' + 0` is `12`, `'\x1c12' + 0` and
`'\xa012' + 0` are `0`. This is not the lexer's SQL-token whitespace,
which omits `\v`. A plain digit run - no `.`, no exponent - is `INTEGER` only if
it fits int64; otherwise it is `REAL` at that point, before any
operator sees it, and a run too large for a double is `inf`:

```
'9223372036854775807' - 1        9223372036854775806     INTEGER
'9223372036854775808' - 1        9.22337203685478e+18    REAL
'999...9' + 0  (320 nines)       inf                     REAL
```

This is conversion, not comparison, and does not weaken the rule
above. See `_docs/decisions.md`, 2026-09-28.

Text becomes a REAL by SQLite 3.50.4's own algorithm, not by correct
rounding. `sqlite3AtoF` reads about 19 significant digits into a
64-bit integer and scales it by powers of ten in double-double
arithmetic, so for long numerals and large or small exponents its
answer is sometimes one ULP from the correctly rounded double that
Python's `float()` gives. `atof.py`'s `text_to_real` is a port of it,
and it is the only conversion used wherever text becomes a REAL:
`_scan_number` (TEXT operands, affinity, `sum`/`avg`), and the
parser's decimal literals and integer literals past int64. `float()`
is never called on SQL-derived text.

```
'18823239210196293635' * 1.0          0x1.05399454f5f45p+64   (float(): ...f46p+64)
'191794978794.7906036428205' * 1.0    0x1.653ef8ff56532p+37   (float(): ...56533p+37)
'2.4703282292062328e-324' * 1.0       0.0                     (float(): 5e-324)
```

The model is the pinned oracle (SQLite 3.50.4) as measured on macOS
arm64. Other SQLite versions convert differently (3.45.1 on x86_64
used an 80-bit `long double` path), and other platforms' oracles are
not yet measured (#176): the differential and sampling tests that
compare this conversion with the live oracle skip, naming #176, where
the oracle does not reproduce the pinned vectors, and historian still
follows the macOS arm64 model there. See `_docs/decisions.md`,
2026-10-06, issue #134.

`%` reads each operand as an int64, not through `_scan_number`'s
value. A REAL is truncated toward zero and clamped to int64, infinity
included. TEXT is read the way SQLite's `sqlite3Atoi64` reads it:
whitespace, an optional sign, then digits up to the first non-digit -
never a `.` or an exponent - clamped to int64, and `0` if there are no
digits. Whether the result is `REAL` still follows `_scan_number`'s
class for that TEXT, so the value and the class come from different
scans:

```
'1e3' % 7                1.0     REAL   (reads 1, class REAL)
'1.5e2' % 7              1.0     REAL
'1e400' % 3              1.0     REAL   (reads 1, never inf)
('1e400'+0) % 3          1.0     REAL   (inf clamps to int64 max)
'7abc' % 2               1       INTEGER
```

See `_docs/decisions.md`, 2026-09-28, issue #106.

SQLite also has no NaN: `typeof(0.0/0.0)` is `null`, so a NaN can never
be a stored value. A computed NaN reaching `order_key` would violate the
total order, which is a risk for the expression evaluator rather than
for this module.

SQLite does keep the sign of a zero, and unary minus decides it. `-x`
over a computed or TEXT-derived `REAL` is `0 - x`, not a sign flip:
the same for every non-zero value and for the infinities (`inf` and
`-inf` swap), but `+0.0` for both `0.0` and `-0.0`. The one exception
is a `REAL` literal directly under `-`, through any parentheses (which
are not a node), which SQLite folds to a negative literal. Unary `+`
is a node, so it breaks the fold. `INTEGER` negation is unaffected.

```
-(0.0)  -((0.0))  -0.0    -0.0   REAL literal under -: folded
-(line_no * 0.0)          0.0    computed: 0 - x
-(+0.0)                   0.0    + is its own node
-(-0.0)                   0.0    outer - over a computed -0.0
-'0.0'  -'-0.0'           0.0    TEXT scans to REAL: 0 - x
-'0'                      0      INTEGER
```

See `_docs/decisions.md`, 2026-09-29, issue #110.

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

Phase 1 operators: `Scan`, `Filter`, `ConstantGuard`, `Project`,
`Aggregate`, `Sort`, `Limit`, `Distinct`. Phase 3 adds `HashJoin`.
`ConstantGuard` holds the `WHERE` terms that read no column and
decides them before its child is pulled (see "`WHERE` terms with no
column reference" below); a query with no such term has none.

`Limit` with a limit of `0` yields nothing and pulls nothing, whatever
the `OFFSET`: SQLite runs no part of a `LIMIT 0` query, so `WHERE
path LIKE 'a' ESCAPE 'ab' LIMIT 0 OFFSET 1` returns no rows rather
than raise.

`Aggregate` handles both the grouped case and the whole-table case,
which differ in one respect that matters: with no `GROUP BY` and no
rows, the whole-table case still emits exactly one row.

Generators are used inside `rows()`, but the operator is an object
rather than a bare generator function, so the tree can be inspected,
printed by `EXPLAIN`, and asserted on in tests.

### `HAVING` terms that move below the aggregate

SQLite does not run every `HAVING` term once per group. When the query
has a `GROUP BY`, it takes each term of `HAVING` that no group can
disagree on and runs it once per input row, in `WHERE`, before any
grouping. The planner does the same, so the two engines raise the same
errors (today only a `LIKE ... ESCAPE` whose escape is not one
character can raise) and keep the same rows. The rule, measured
against the oracle (`_docs/decisions.md`, 2026-10-06, #141):

- It applies only to a query with a `GROUP BY`. With none, nothing
  moves.
- `HAVING` is split into terms on every `AND`, through nested `AND`s
  and parentheses, left to right. An `OR`, a `NOT (...)`, a
  comparison or anything else that is not an `AND` is one term and is
  never split further.
- A term moves when it contains no aggregate call and every column
  reference in it lies inside a subexpression that matches a `GROUP BY`
  key by shape, or when it contains no column reference at all. A term
  with an aggregate anywhere in it stays in `HAVING`, `OR` branches
  included.
- A term that is an integer literal `0` (`0`, `00`, `(0)`) stays in
  `HAVING`: SQLite does not move a term it knows is always false.
  Every other constant moves, `0.0`, `-0`, `NULL` and `1 > 2`
  included.
- The moved terms run in `HAVING` order, after every term of the
  query's own `WHERE`, and each is evaluated as written, over the
  scan's row. They leave `HAVING`: the planner builds one `Filter`
  holding them, between the `WHERE` `Filter` (or the `Scan`) and the
  `Aggregate`, and a `HAVING` left with no terms gets no `Filter`.
  A moved term is not copied back: it filters rows, so a group
  survives with only the rows that passed, and `count`, `sum` and the
  rest see only those. That differs from filtering the group only when
  the rows of one group differ in a way the key does not (`GROUP BY
  line + 0` puts `'1'` and `'1.0'` in one group, and `HAVING (line +
  0) || 'x' = '1x'` keeps only the first), and SQLite's answer is the
  per-row one.
- A moved term is never offered to the scan. Pushdown negotiation (next
  section) looks only at the query's own `WHERE`, and a moved term is
  not part of it; the planner does this, not the optimizer, so
  `--no-pushdown` moves the same terms.

A moved term with no column reference is a constant: like a constant
`WHERE` term it is decided once, before any row, after every constant
term of the `WHERE` (see "`WHERE` terms with no column reference"
below). The integer literal `0` stays in `HAVING` and is not one of
them, so `GROUP BY path HAVING 0 AND CONSTERR` raises.

### Constant propagation in `WHERE`

SQLite rewrites a `WHERE` before it runs it: a top-level term of the
form `column = constant` is propagated into every other term, which
changes which sub-expressions are evaluated and so which queries raise.
It never changes which rows pass - a row that passes the source already
has that value in that column. The planner does the same rewrite, so
`WHERE NOT (line_no = 5 AND path LIKE 'a' ESCAPE 'ab') AND line_no = 5`
raises in both engines. The rule, measured against the oracle
(`_docs/decisions.md`, 2026-10-07, #142):

- `WHERE` is split into terms on every `AND`, through nested `AND`s and
  parentheses, left to right, as `HAVING` is above. A term is a
  *source* when it is `X = K`, `K = X` or `X IN (K)` with exactly one
  element (SQLite reads that as `X = K`), where `X` is a column
  reference, bare or qualified, and `K` is a literal (`NULL` included)
  or a chain of unary `+`/`-` over a numeric literal, any parentheses
  on either. Nothing else is a source: not `IS`, `<>`, `BETWEEN`,
  `LIKE`, `NOT IN`, a two-element `IN`, `+X = K`, `X + 0 = K`, `X =
  X`, and not a source under `NOT`, under `OR`, or anywhere below the
  top-level `AND`s. A `K` that is any other constant expression
  (`line_no = 2 + 3`) is not handled yet (#179).
- Of two sources for one column, the *last* is used; the earlier one is
  rewritten like any other term. Sources for different columns are all
  used.
- Every occurrence of a source's column in every other term, at any
  depth (under `NOT`, `OR`, in `IN` lists, `BETWEEN`, `LIKE` and its
  `ESCAPE`, arithmetic, `||`, comparisons, `IS`), is replaced by the
  constant; the source used keeps its own column. A term before the
  source is rewritten as well as one after it.
- The replacement holds the value the constant would have if stored in
  the column: the column's affinity is applied to it (`line_no = '05'`,
  `= 5.0` and `= ' 5'` give the INTEGER `5`; a REAL column's `r = 1`
  gives `1.0`; a TEXT column's `line = 5` gives `'5'`; a constant the
  affinity cannot convert, `line_no = 'x'`, stays as it is).
- The replaced operand is still the column as far as affinity goes: as
  an operand of a comparison, `IS`, `IN`'s left side or `BETWEEN` it
  has the column's affinity (`line_no > '4'` with `line_no` replaced
  by `5` is `5 > 4`, `TRUE`). Inside an expression (`line_no + 0`,
  `line_no || ''`) it has none, as the column has none there.
- Only the `WHERE` is rewritten. The select list, `GROUP BY`,
  `HAVING` (whether its terms move below the aggregate or not), `ORDER
  BY` and aggregate arguments keep the column, and a `HAVING` term is
  never a source.
- A comparison whose collation is not `BINARY` is not a source in
  SQLite. v1 has no `COLLATE` and every column is `BINARY`, so there is
  no such case yet.

The planner does this, not the optimizer, so `--no-pushdown` rewrites
the same terms; the `WHERE` `Filter` holds the rewritten terms, and a
`WHERE` with no source is left exactly as bound. A term the rewrite
leaves with no column reference (`line_no > 5` beside `line_no = 5`
is `5 > 5`) is a constant term like any other and is decided once,
before any row (next section): `WHERE path LIKE 'a' ESCAPE 'ab' AND
line_no = 1 AND line_no = 2` returns no rows, and `WHERE path = 'zzz'
AND path LIKE 'a' ESCAPE 'ab'` raises even though no path is `'zzz'`.
The full family of such contradictions is pinned by #180.

### `WHERE` terms with no column reference

SQLite decides a `WHERE` term that reads no column once, before it
reads the first row. The planner does the same, so the two engines
raise the same errors and do the same work. The rule, measured
against the oracle (Python `sqlite3` 3.50.4; `_docs/decisions.md`,
2026-10-09, #171), with `ERR` for `path LIKE 'a' ESCAPE 'ab'` and
`CONSTERR` for `'a' LIKE 'a' ESCAPE 'ab'`:

1. **Terms.** The `WHERE`, after constant propagation, is split into
   terms on every top-level `AND`, nested `AND`s and parentheses
   flattened, left to right, as for pushdown. An `OR`, a `NOT (...)`
   or anything else is one term and is never split: `ERR AND (line_no
   = 1 AND line_no = 2)` has three terms, `ERR AND NOT (line_no = 1
   AND line_no = 2)` two.
2. **Constant term.** A term is constant when nothing in it is a
   column reference, a `*` or a function call. A column that constant
   propagation replaced by its constant is not a column reference
   here: it never reads the row. A constant inside a term that has a
   column is not hoisted: `line_no > 0 OR CONSTERR` is one per-row
   term, raises only for a row where `line_no > 0` is not `TRUE`, and
   raises nothing over zero rows.
3. **Once, before any row, in order.** Every constant term is
   evaluated exactly once, in `WHERE` order, before the first row is
   read and before any other term, wherever it sits among them. Each
   is evaluated as a condition (condition context, the integer-literal
   rule included), so `line_no < 0 AND (1 = 1 OR CONSTERR)` and
   `line_no < 0 AND (CONSTERR OR 1)` raise nothing and `line_no < 0
   AND (CONSTERR AND 1 = 0)` raises. The first constant term that is
   not `TRUE` - `FALSE` or `NULL` - ends it: later constant terms are
   not evaluated, no row is read, and the result is what the query
   returns over zero rows (`count(*)` is `0`, a `GROUP BY` gives no
   rows). A constant term that raises raises whatever the input is,
   empty included. So `WHERE ERR AND 0` returns no rows, `WHERE 1 = 0
   AND CONSTERR` returns no rows, and `WHERE CONSTERR AND 1 = 0`
   raises.
4. **Moved `HAVING` terms.** With a `GROUP BY`, a `HAVING` term that
   moves below the aggregate and has no column is a constant term of
   the same list, after every constant term of the `WHERE`, in
   `HAVING` order. A `HAVING` with no `GROUP BY` moves nothing and
   runs once on the one aggregate row, as before.
5. **Not evaluated when no row is pulled.** `LIMIT 0` evaluates
   nothing: `WHERE CONSTERR LIMIT 0` returns no rows.
6. **Everything per row is unchanged.** The other terms run per row,
   left to right, after the constants. The `Filter` keeps every term,
   constants included - by the time a row reaches it they are all
   `TRUE` - and the terms offered to the scan are the same.

The planner builds one `ConstantGuard` holding the constant terms, in
that order, directly above the `WHERE` `Filter` and the `Filter` of
moved `HAVING` terms (so below `Aggregate`, or below `Sort`, `Project`,
`Distinct` and `Limit` when there is none). It evaluates its terms on
the first pull of its rows, never when the tree is built, so
`--explain` evaluates nothing; a false or `NULL` term means the scan is
never read and does no work. The planner does this, not the optimizer,
so `--no-pushdown` gets it too. The select list, `ORDER BY` and
`GROUP BY` are not touched: `SELECT CONSTERR FROM blame` raises only
when there is a row.

### Expression evaluation

`exec/expression.py` walks an expression against a row and returns a
value. It is a plain function over `(expr, row, schema)` with no git,
no I/O, and no operator dependencies.

Aggregate calls are not evaluated here. The planner splits each
`SELECT` and `HAVING` expression into aggregate calls, computed by the
`Aggregate` operator, and the surrounding scalar expression, computed
here over the aggregate's output row.

Evaluation order follows SQLite, which is observable only when an
operand raises (today, a `LIKE ... ESCAPE` whose escape is not one
character). Operands are evaluated left to right, and where the
evaluation stops depends on where the expression is used:

- **Value context** - a select-list item, an `ORDER BY` or `GROUP BY`
  key, an aggregate argument, and any operand of anything but
  `AND`/`OR`/`NOT` (a comparison, arithmetic, unary `+`/`-`, `||`,
  `IS`, `LIKE`, `IN`, `BETWEEN`): every operand of `AND`, `OR`, `NOT`
  and `BETWEEN` is evaluated.
- **Condition context** - the root of `WHERE` and of `HAVING`, and the
  operands of `AND`/`OR`/`NOT` in condition context: evaluation stops
  once whether the condition is `TRUE` is decided. `AND` stops after a
  `FALSE` left side and `OR` after a `TRUE` one. A `NULL` left side
  counts as `FALSE` at the root and under an even number of `NOT`s, so
  it stops `AND` there, and as `TRUE` under an odd number, where it
  stops `OR`. `x BETWEEN low AND high` is `x >= low AND x <= high` and
  stops the same way; `x NOT BETWEEN ...` is `NOT (x BETWEEN ...)`.
- **An integer-literal operand in condition context** decides an
  `AND`/`OR` before either operand runs. An operand is an *always-true*
  or *always-false* literal when it is an unsigned integer literal that
  fits in 32 bits (`0` to `2147483647`; leading zeros do not count, so
  `01` and `00` are ones), any parentheses around it, or an `AND`/`OR`
  that this rule itself turns into such a literal: nonzero is true,
  zero is false. `x OR <true>` is the literal and `x AND <false>` is
  the literal, so `x` is never evaluated, wherever it sits (`WHERE ERR
  OR 1` and `WHERE NOT (ERR AND 0)` keep every row); `x AND <true>`
  and `x OR <false>` are `x`. Both operands are simplified first, so
  `ERR OR (0 OR 1)` and `(ERR AND 1) OR 1` count. Nothing else is such
  a literal: not `-1`, `+1`, `1.0`, `'1'`, `NULL`, `1 = 1`, `NOT 0`,
  `2147483648` or wider, a column that constant propagation replaced
  by a constant, or a `NOT` around a literal. One condition is one
  `WHERE` term - the `WHERE` is split on its top-level `AND`s first,
  so its terms are not simplified against each other, and `WHERE
  CONSTERR AND 0` still evaluates `CONSTERR` (both are constant terms,
  decided in order before any row, so it raises before the `0` is
  reached) - or the whole `HAVING`, top-level
  `AND`s included: `HAVING max(path) LIKE 'a' ESCAPE 'ab' AND 0` keeps
  no group and raises nothing. A `HAVING` term that moves below the
  aggregate is a `WHERE` term. Value context is never simplified
  (`SELECT ERR OR 1` raises), nor is an operand of anything but
  `AND`/`OR`/`NOT` (`WHERE (ERR OR 1) IS NULL` raises). SQLite's hex
  literals follow the same 32-bit rule (`0x7fffffff` is one,
  `0x80000000` is not); the v1 grammar does not have them yet (#6).
- `IN` stops at the first list element equal to its left side, in
  both contexts. A `NULL` element or left side does not stop it. `IN ()`
  evaluates nothing in condition context and its left side in value
  context.

The evaluator has one entry point per context: `evaluate()` for a
value, `evaluate_condition()` for one condition. `Filter` calls the
second once per `WHERE` term, left to right, until a term is not
`TRUE`, and once on the whole `HAVING`; it holds the predicate as
written, so `--explain` and pushdown see the query's own terms, and
the literal rule above is applied as the condition is evaluated. A
future `CASE WHEN` or `JOIN ... ON` condition is condition context and
uses the second.

### Pushdown negotiation

The optimizer's one job in v1.

1. Split the `WHERE` predicate on `AND` into conjunctive terms. `OR`
   is not split - a disjunction is one term, and pushes down only if a
   scan accepts the whole thing.
2. Offer each term to the scan beneath it, left to right, one
   `accepts(term)` call per term. The terms offered are the terms as
   rewritten by constant propagation (see above), the same objects the
   `Filter` keeps: a term in which a column became a constant is
   offered with the constant in it, and a scan that recognises only a
   column does not accept it. Only the `WHERE` filter directly
   above the scan is negotiated - never `HAVING`, and never the
   `Filter` of `HAVING` terms that moved below the aggregate (see
   above), which sits above it, or directly above the scan when there
   is no `WHERE`, and is marked not negotiable.
3. Pass the accepted terms to the scan as scan arguments, recorded on
   the `Scan` operator so the plan shows them.
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

Four kinds, all of them the user's fault and none of them tracebacks:

- **Parse errors** name the position and what was expected. An
  expression tree taller than SQLite's `SQLITE_MAX_EXPR_DEPTH` (1000),
  its height counted by SQLite's own per-node rules, is a parse error
  with SQLite's own message, `Expression tree is too large (maximum
  depth 1000)`.
- **Binding errors** name the unknown column or table, and list what
  is available.
- **Unsupported grammar** says the feature is not supported and points
  at the non-goals in §1. A query using a window function gets told
  window functions are out of scope, not a syntax error.
- **Aggregate misuse** covers a `FunctionCall` that is structurally
  illegal regardless of what it names or what data it would touch -
  an aggregate call nested inside another aggregate call's arguments,
  an aggregate reached through a select-list alias somewhere other
  than a clause's own direct reference to it, a `HAVING` on a
  non-aggregate query, a bare column that is neither a `GROUP BY`
  key nor inside an aggregate call, or - once `SELECT DISTINCT` is
  present - an `ORDER BY` key, bare column or aggregate call alike,
  that is not itself a select-list item or built purely from one.
  SQLite itself rejects the first four of these at prepare time,
  before running anything, so those are not a historian-only
  invention. The last is: SQLite accepts that query and picks an
  answer from its own unspecified internals, with no documented,
  reproducible rule behind which row wins, so historian rejects the
  shape outright rather than risk copying an undocumented internal
  it cannot verify against (`_docs/decisions.md`, 2026-09-25). Either
  way it is a binding error like any other, raised deterministically
  and never left for `exec/expression.py` to discover at runtime from
  an actual row.

When a statement has more than one binding error, historian reports
the one SQLite reports. SQLite's order of the clauses, measured
against the oracle (`_docs/decisions.md`, 2026-10-02, re-checked
under the pinned oracle in #117): the `FROM` table and the qualifier
of any `x.*` select-list item; `LIMIT` and `OFFSET`, walked as one
expression with `LIMIT` first (below); the select list, left to
right; `HAVING` on a non-aggregate query; `HAVING`; `WHERE`; `ORDER
BY`; `GROUP BY`; then an aggregate call in the `WHERE` of an aggregate
query or in the `ORDER BY` of a non-aggregate one (an aggregate query
has `GROUP BY` or an aggregate call in its select list; in a
non-aggregate query an aggregate call in `WHERE` is reported at
`WHERE`'s turn). Within an `ORDER BY` or `GROUP BY` clause the terms
are taken left to right, and an integer term below 1 or above 65535
is rejected at its own turn; any other out-of-range ordinal comes
after every term's name errors, and in `GROUP BY` an ordinal before an
aggregate key (`_docs/decisions.md`, 2026-10-07, #144). historian's
own rejections of queries SQLite accepts - the bare column that is
neither a `GROUP BY` key nor inside an aggregate, the `SELECT
DISTINCT ... ORDER BY` key, a `LIMIT`/`OFFSET` that is not a literal
integer - come after every error SQLite raises, so they never hide
one.

Inside one expression (`_docs/decisions.md`, 2026-10-07, #144),
SQLite resolves names by walking the tree, a node first and then its
children left to right, with one error slot that each new error
overwrites, so the error reported is the last one recorded. What the
walk does after an error depends on the node it meets:

1. A column reference that resolves: the walk goes on. One that does
   not records `no such column` and ABORTs.
2. A function call records at most one error of its own before its
   arguments: aggregate misuse if it is an aggregate (right name,
   right argument count) where none is allowed, else `no such
   function`, else `wrong number of arguments`. Then it walks its
   arguments left to right, stopping at the first ABORT, and returns
   normally. Inside an aggregate's arguments no aggregate is allowed,
   so a nested one records its misuse where it stands, and so does a
   select-list alias naming one.
3. `x LIKE y [ESCAPE z]` is a call with no error of its own over `y`,
   `x`, `z`, in that order; `x NOT LIKE y` is a `NOT` (rule 6) around
   it.
4. `x IS NULL` and `x IS NOT NULL` walk `x` whatever is recorded, and
   return normally.
5. `x IS y` and `x IS NOT y` with `y` a bare column name resolve `y`
   first, and ABORT if it does not resolve.
6. Every other node ABORTs at once if an error is already recorded;
   otherwise it walks its children and passes an ABORT up.
7. `x IN (e)` and `x NOT IN (e)`, with one element and that element
   constant - no column reference and no function call anywhere in
   it; `LIKE` over constants counts as constant - are SQLite's `x =
   +e` and `NOT (x = +e)`: the node ABORTs like rule 6, then `x` is
   walked, then the `+` ABORTs if an error is recorded, then `e` is
   walked. It matters only when `e` is a node that does not ABORT on
   its own (`IS NULL`, `LIKE`): `(nofn(1) IN (3 IS NULL)) + ghost` is
   `no such function: nofn`, where with two elements, or with a
   column in `e`, the walk goes on to `ghost`.

An ABORT stops at the nearest enclosing function call, `LIKE` or `IS
NULL`, or ends the root: one select-list item, one `WHERE`, `HAVING`,
`GROUP BY` or `ORDER BY` term, or `LIMIT` and `OFFSET` together. In
`LIMIT` and `OFFSET` no column resolves, real or alias, and no
aggregate is allowed. The late errors above (the aggregate call in a
`WHERE` or `ORDER BY` reported after `GROUP BY`, an aggregate `GROUP
BY` key, an out-of-range ordinal) and historian's own rejections are
not recorded during the walk: they are raised after it, if nothing
was recorded. An aggregate-misuse message keeps historian's wording
(#102) and names the clause the call was found in.

Accepted difference: historian implements none of SQLite's built-in
scalar functions (§1), so a call to one - `abs`, `length`, the
two-argument `max`, and so on - is `no such function` to historian
(the two-argument `max`/`min`, a wrong argument count) wherever it
stands, where SQLite accepts the call or reports something else. This
includes `LIMIT abs(2)`, which SQLite accepts and historian rejects
with `no such function: abs`. #183 tells SQLite's built-ins apart
from names SQLite does not have.

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

Five fixtures:

- **tiny** — a handful of commits, two authors, a rename, a deletion,
  and a merge. The default for most tests, small enough to reason
  about by hand.
- **awkward** — built to break things: unicode in paths and author
  names, a space and a quote in a path, an empty file, a binary file,
  a file with no trailing newline, a file deleted and later recreated,
  an empty commit message, and a line of content that looks like git
  porcelain output.
- **large** — generated, hundreds of commits. Benchmarks only, never
  correctness. Built from a seeded PRNG, 300 commits over 4,013
  paths (12 under `src/auth/`), and never built by a plain `uv run
  pytest`: ask for it with `--build-large` or `uv run python -m
  tests.fixtures.build large`. Its `HEAD` is not pinned.
- **casefold** — one commit of paths differing only by letter case:
  ASCII (`src/`, `SRC/`, `Src/`) and non-ASCII (`straße/`, `STRAßE/`,
  `STRASSE/`), plus a path spelled `5`. Exists for `blame`'s `LIKE`
  prefix pushdown, which must fold exactly what SQLite's `LIKE`
  folds. Its paths are written straight into the index, never to
  disk, so a case-insensitive file system cannot merge them.
- **numeric** — two commits by two authors over `long.txt` (120
  lines, 10-99 rewritten by the second author), `mid.txt` (12) and
  `short.txt` (3): 135 blame rows whose `line_no` runs to one, two
  and three digits, so numeric order and text order disagree on a
  bare `line_no` (`max` is 120 as a number, `'99'` as text). The
  builder checks that disagreement through SQLite.

The fixture builder asserts what it built. A fixture that silently
stops containing a merge commit takes a whole class of tests with it.

### The differential harness

For each test query:

1. Scan the table **with pushdown disabled**, giving the complete set
   of rows.
2. Load those rows into an in-memory SQLite table with the same schema.
   "The same schema" means each column keeps its affinity and every
   value is bound exactly as scanned: a table with a `REAL` column is
   a typeless table behind a view of that name selecting `CAST(col AS
   REAL)`, so a scanned `-0.0` keeps its sign and the column still has
   `REAL` affinity, and a scanned NaN fails the load.
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

Two cells match if and only if they have exactly the same Python type
and then, for `REAL`, the same `float.hex()` - bit-identical, so `0.0`
and `-0.0` differ, `inf` and `-inf` differ, and one ULP is a
difference - or, for any other type, are `==`. Never a tolerance, and
never `==` or `repr` for `REAL`. A NaN on either side is a failure of
its own, checked before anything is sorted or compared: SQLite has no
NaN, so one can only be an engine bug.

The multiset sort key for a cell is a pair: `order_key` first, so the
`NULL`, numeric, `TEXT` order is SQLite's, then an exact tie-break
(type name, then the value, a `REAL` by `float.hex()`), so two equal
multisets always sort to the same sequence even where `order_key`
ties `0`, `0.0` and `-0.0`. Under `ORDER BY`, rows are still grouped
into ties by `order_key` alone - `0.0` and `-0.0` genuinely tie there
- and the rows within a tied group are compared as an exact multiset.

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

Prints the operator tree with what the optimizer decided, root first,
one operator per line, children indented two spaces. Every operator is
printed, `Project` included. For
`SELECT author_name, count(*) FROM blame WHERE path LIKE 'src/auth/%'
GROUP BY author_name ORDER BY 2 DESC`:

```
Project (author_name, count(*))
  Sort (count(*) DESC)
    Aggregate (group=[author_name], aggs=[count(*)])
      Filter (path LIKE 'src/auth/%')
        BlameScan (pushed: path LIKE 'src/auth/%' -> 12 of 4013 paths)
```

A query with a `WHERE` term that reads no column (§3) has one more
line, `ConstantGuard (<terms>)`, between the operators it sits
between: its terms joined by ` AND `, spelled as the `Filter` line
spells them, a propagated constant printed as its value. `--explain`
never evaluates it. For `SELECT path FROM blame WHERE path = 'a' AND
1 = 0`:

```
Project (path)
  ConstantGuard (1 = 0)
    Filter (path = 'a' AND 1 = 0)
      BlameScan (pushed: path = 'a' -> 1 of 4013 paths)
```

The scan line is `<Name> (pushed: <terms> -> <n> of <total> paths)`:
the pushed terms joined by `, ` (or `none`), the paths tracked at
`HEAD`, and how many of them the scan would blame for those terms.
Knowing that takes one `git ls-tree` and never a `git blame`. The plan
goes to stdout, since it is what was asked for. `--explain` with
`--stats` prints the plan only: nothing ran, so there is no work to
report.

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
proves pushdown works in a test should be visible to a user. They go
to stderr after the results, so stdout is identical with and without
`--stats`, and only after a query that succeeded. The counts are what
the scan actually did: `LIMIT 0` never reads the scan and prints
`0 paths blamed, 0 skipped`, and so does a `WHERE` with a constant
term that is not `TRUE` (`WHERE 1 = 0`, §3).

### `--no-pushdown`

Runs the query with nothing pushed into the scan: the optimizer step
is skipped, so every scan does all of its work, as if no `WHERE` term
could be pushed. The rows are the same as without the flag, and the
`Filter` is unchanged. It is the same meaning of "pushdown disabled"
that §4's differential harness uses. With `--explain` the scan line
reads `pushed: none`, and with `--stats` the counts show every path
blamed. It changes nothing about errors or exit codes.

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
repository could not be read, `4` an internal error - a bug in
historian, not a mistake in the query, such as a stack overflow.

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
