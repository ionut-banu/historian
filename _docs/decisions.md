Decisions made while building. Newest last, one short entry each.

If a decision contradicts `_docs/spec.md`, edit the spec in the same
commit that records the decision here.

---

2026-08-24 - SQLite is the oracle, not sqllogictest

The sqllogictest corpus builds its own tables with CREATE TABLE and
INSERT, which historian will never support. Running it would mean
building a storage engine to satisfy a test harness. Differential
testing against SQLite gives the same authority over the grammar we
actually support, on our own data, with no adapter.

2026-08-24 - Pushdown may only narrow to a superset; the filter stays

A scan that claims a predicate is exactly satisfied lets the filter
above it be removed, and every scan becomes a place where wrong rows
reach the user. Keeping the filter costs one pass over an already
small row set and confines pushdown bugs to performance. Removing
provably redundant filters is a v2 optimisation with its own tests.

2026-08-24 - blame is built first, before commits

Building commits first means two months of being a worse SQLite, and
teaches the scan the wrong interface: read everything, filter in
memory. blame cannot be materialised, so pushdown is a feasibility
requirement from the first commit and every later table inherits the
right scan shape.

2026-08-24 - blame shells out to git blame --line-porcelain

Rename detection and merge handling are a month of work in a domain
nobody is evaluating. The subject of this project is the engine above
the scan.

2026-08-24 - The spec lives in _docs/spec.md, undated

It was first written as a dated design document, which made it a
historical record - and historical records are never edited, while the
source of truth has to change whenever the project does. Splitting by
lifecycle instead: _docs/spec.md is living and always true,
_docs/decisions.md is append-only and never edited. The original path
also carried the name of the tooling used to write it, which is not
something this repo should know about.

2026-08-24 - Python, not Rust

Rust was the better language on the merits - gitoxide, single binary,
enums that fit AST work. It was rejected because the author has not
written Rust, and the process depends on the author being able to judge
what the subagents produce. A QA loop whose reviewer cannot read the
code is theatre, and a project whose value is being able to explain it
cannot be written in a language its author cannot read.

Speed did not enter into it. The cost is dominated by git subprocesses,
and pushdown is a ~300x win in any language.

A Rust port is a good v2, once the differential suite exists to prove
the port correct. Until then the operator layer stays explicit and free
of Python dynamism so it can be translated rather than redesigned.

2026-08-24 - One plan representation in v1, not logical plus physical

Every logical operation in v1 has exactly one physical implementation,
so the split would be two parallel hierarchies and a translation pass
that never makes a choice. It gets introduced when joins offer a real
choice between hash and nested-loop.

2026-08-24 - historian guarantees row order, SQLite does not

SQLite may return rows in any order without ORDER BY. historian fixes
an order, because reproducible output is worth more here than the
freedom to reorder. The differential harness therefore compares sorted
multisets unless the query has an ORDER BY.

2026-08-24 - The SQLite side of a differential test is loaded from an
unfiltered scan

The obvious harness feeds SQLite from the same scan historian runs. It
is wrong: a pushdown bug that drops rows removes them from both sides,
the engines agree, and the bug is invisible. Loading SQLite from a scan
with pushdown disabled lets historian be as clever as it likes, and any
cleverness that changes the answer is caught.

This is also why §3's earlier claim - that pushdown cannot be verified
by results - was too strong. Wrong pushdown is caught by results.
Absent pushdown is not, and needs the work-done assertions.

2026-08-24 - Fuzzer findings are shrunk, then become differential tests

An unshrunk mismatch is a twelve-clause query nobody can act on, and a
mismatch that vanishes on the next run is not worth reporting. So the
fuzzer is seeded and reproducible, the shrinker reduces a failure to
the smallest query that still fails, and the result is committed as a
permanent differential test. The fuzzer feeds the suite rather than
sitting beside it.

2026-08-24 - Output format does not depend on whether stdout is a tty

Tools that print a table interactively and CSV when piped break scripts
that were developed interactively, in a way that is hard to see. Format
is whatever --format says, defaulting to table in both cases. Only
colour and paging look at the terminal.

2026-08-24 - --explain is a flag, not an EXPLAIN keyword

EXPLAIN as SQL would add grammar that §1 does not declare, and the
grammar is the thing scope discipline depends on. A flag costs nothing
and keeps the parser exactly as specified.

2026-08-27 - Column affinity lives in the expression evaluator, not in
values.py

Grooming issue 2 turned up a gap: section 1 requires SQLite's type
affinity, but no part of the design owned it. Confirmed against
sqlite3 that `5 = '5'` is FALSE while `WHERE line_no = '5'` is TRUE
against an INTEGER column - SQLite converts the literal to the
column's declared type first.

values.py cannot do this. It sees two values and has no way to know
which came from a column or how that column was declared. So it
implements value-to-value comparison only, and affinity goes to
exec/expression.py, which has both the AST and the schema.

Recorded now rather than when the expression evaluator is built,
because blame.line_no is phase 1's only non-TEXT column and the
fuzzer is weighted toward cross-type comparisons - it would have
found this as a mismatch with no owner.

2026-08-27 - Numeric comparison is exact; no float() anywhere

Implementing issue 2 turned up that SQLite compares integers against
reals exactly rather than casting. 9007199254740993 = 9007199254740992.0
is FALSE and the > is TRUE; past 2^53 a double cannot hold consecutive
integers, so a cast reverses the answer.

Python's int/float comparison is already exact and agrees, so the fix
is to add nothing. Recorded because the danger is not in values.py,
where there is now a test pinning it, but in the expression evaluator's
arithmetic path, where a float() conversion would look like a tidy-up
and would break comparison on inputs no hand-written test would try.

Also from the same session: SQLite has no NaN - typeof(0.0/0.0) is
null - and typeof(true) is integer, which independently confirms that
excluding bool from Value is required rather than merely tidy.
