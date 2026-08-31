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

2026-08-27 - One worktree per subagent, not just one branch

Issue 2 raced twice in a minute. The orchestrator ran `git checkout
main` while QA was mid-review, and the module under test vanished from
under it. QA then restored its own branch, per its instructions, in the
window between the orchestrator checking which branch it was on and
committing - so a documentation commit landed on the feature branch
instead of main, and `git push origin main` pushed nothing.

Neither party did anything wrong. One working directory shared between
an orchestrator on main and a subagent on a branch is a race, and the
process creates that situation on every single issue.

So each implementing subagent gets its own directory via `git worktree
add`, and the orchestrator never checks out a feature branch. QA also
now records the commit it is reviewing and checks it against what it
was told - which is what caught this one.

2026-08-27 - One token type per keyword, not a shared KEYWORD type

Grooming issue 3 left this open. Both work, so it was decided on which
failure mode each produces.

A shared KEYWORD type makes the parser match on `tok.type is KEYWORD
and tok.text == "SELECT"`, where a mistyped literal silently never
matches and surfaces later as a confusing parse error. One member per
keyword makes the same mistake an AttributeError at import.

The parser will have more keyword match sites than anywhere else in the
codebase, so the safer failure mode is worth thirty enum members. It is
also what SQLite and PostgreSQL do, and it translates to a Rust enum
with exhaustive matching rather than to string comparison.

An is_keyword() helper covers the cases that need "any keyword".

2026-08-27 - Out-of-scope v1 work goes to the "v2 - Backlog" milestone

The PM is told to file work that contradicts the non-goals as a v2
issue, but nothing said where those live, so they would have collected
with no milestone and been invisible. They now go to "v2 - Backlog",
which holds nothing anyone is scheduled to build.

2026-08-31 - Arithmetic producing NaN yields NULL, enforced in
exec/expression.py

SQLite has no NaN - typeof(0.0/0.0) is null - confirmed against
sqlite3. values.py now rejects a NaN that reaches it (issue #15),
but nothing yet stops arithmetic from producing one in the first
place: values.py has no arithmetic, so it cannot be the enforcement
point. exec/expression.py, which does not exist yet (#12, blocked
on #9), owns converting a NaN result of +, -, *, / into NULL before
it ever reaches a Value position.

Recorded now, matching the column-affinity precedent, because the
fuzzer is expected to generate 0.0/0.0-shaped queries early and this
is a mismatch with no owner until #12 lands.

2026-09-01 - Any non-ASCII character can start or continue an
identifier; the lexer never asks if it is a letter

Issue #16 started as a narrower bug: `_read_number` used
`str.isdigit()`, which is `True` for non-ASCII digit-shaped
characters like `²` and Arabic-Indic `١٢٣`, so those could reach
`int()`/`float()` and raise. The obvious fix - restrict the digit
paths to ASCII and leave `_read_identifier` on Python's
`isalpha`/`isalnum` - turned out to be wrong, not just incomplete.
Confirmed against `sqlite3` 3.51.0: `select ™;` and `select café;`
both fail with `no such column`, so SQLite lexed both as
identifiers. But `'™'.isalpha()` and `'™'.isalnum()` are both
`False` in Python, so the "obvious fix" still raises `LexError` on
`™`, on `‽`, and on any other non-ASCII symbol Python does not
classify as a letter or digit.

SQLite's real rule has no such gap: an ASCII digit (`0`-`9`) can
start a number, and literally every other non-quote, non-operator
character - including every character above ASCII - can start an
identifier. It never consults a Unicode property table. Decided to
match this exactly rather than approximate it with Python's
classifiers, so `_read_identifier` now accepts any character for
which `not char.isascii()`, full stop, with no `isalpha`/`isalnum`
call in the non-ASCII path at all.

The cost is real: this makes `²`, `™`, and other symbol characters
lex as identifiers, which reads as nonsense to a human. It is
still the better rule, for three reasons. It is simpler than
asking Python's Unicode database anything, and simpler still in
Rust, where the equivalent is one `is_ascii()` check rather than a
Unicode-aware classifier. It ports directly - the same plain
codepoint comparisons work unchanged. And it means `²`, `café`,
and `™` all fail the same way SQLite fails them: `no such column`
once `sql/binder.py` exists (#9), not merely "some error" from the
lexer today. Per this module's own docstring, an identifier that
resolves to nothing is the binder's error to raise, not the
lexer's - and a symbol character is exactly that case, not a
lexing failure.

Also decided in the same pass: `\f` (form feed) joins `\t`/`\r` as
whitespace, confirmed against SQLite; `\v` (vertical tab) does
not, because SQLite rejects it too.
