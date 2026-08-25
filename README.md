# historian

A SQL query engine over git history.

```sql
-- Who wrote the code that is still alive in this directory?
-- Not who made the most commits. Who is actually in the file, now.

SELECT author_name, count(*) AS surviving_lines
FROM blame
WHERE path LIKE 'src/auth/%'
GROUP BY author_name
ORDER BY surviving_lines DESC;
```

**Status: in design. There is no code yet.** The specification is
complete and the first milestone is filed as issues. Nothing here runs.

## Why this exists

For queries over commit metadata, historian is not the best tool and
never will be. Dumping `git log` into SQLite is a few hundred lines and
gives faster, more correct answers.

It earns its existence on tables that cannot be materialised in
advance. Blame for one file at one revision costs a walk of that file's
history; blame for every file at every revision is combinatorial. It
can only be computed lazily, for exactly the paths a query's predicates
select — which requires an engine that decides what to compute rather
than a table that already holds the answer.

So `WHERE path LIKE 'src/auth/%'` does not filter four thousand blamed
files down to twelve. It blames twelve.

## How correctness is defined

By SQLite, not by opinion.

Every supported query runs through historian and through SQLite over
the same rows, and the results must be identical. SQLite is the
specification: "correct" means agreeing with the most heavily tested
SQL implementation in existence, including the parts that are easy to
get wrong — `NULL` comparison being neither true nor false, `sum` over
zero rows being `NULL` while `count(*)` is `0`, an integer sorting
before any string regardless of contents.

On top of that, a fuzzer generates random queries within the supported
grammar and diffs both engines. Hand-written tests only cover cases
someone thought of; the bugs that survive are the ones nobody thought
of. Every mismatch it finds is shrunk to the smallest failing query and
committed as a permanent test.

That is what the counts in this README will mean, once there are any.

## Scope

A deliberately small subset of SQL, over a git repository, read-only.

Supported: `SELECT`, `WHERE`, `GROUP BY`, `HAVING`, `ORDER BY`,
`LIMIT`, `DISTINCT`, `INNER JOIN`, and five aggregates.

Not supported, and not planned: subqueries, CTEs, window functions,
`UNION`, outer joins, `INSERT` / `UPDATE` / `DELETE`, transactions.
historian is not a general database and does not write to your
repository.

## Documents

This README describes what exists. It is not the specification.

- [`_docs/spec.md`](_docs/spec.md) — what historian is, in full
- [`_docs/decisions.md`](_docs/decisions.md) — why, dated, append-only
- [`_docs/process.md`](_docs/process.md) — how the work is organised
