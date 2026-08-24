# historian — design

A SQL query engine over git history.

Status: in progress. §1 and §2 are settled. §3 onward are listed at the
bottom and not yet designed.

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

## Open decisions

Resolved before §3.

- **Implementation language.** Not chosen. Python is familiar and fast
  to build in; a compiled language gives a single binary and makes the
  pushdown benchmarks more convincing. Reuse from `rulesmith` is not a
  factor — SQL's three-valued logic and coercion rules mean the
  expression evaluator would be rewritten regardless.

## Not yet designed

- §3 Engine architecture — lexer, parser, AST, logical plan, physical
  plan, operator interfaces
- §4 The optimizer — pushdown negotiation, predicate splitting, join
  ordering
- §5 Test architecture — fixture repos, the SQLite oracle harness, the
  fuzzer's generation grammar
- §6 CLI surface — output formats, REPL, error reporting
- §7 Milestones and issue breakdown
