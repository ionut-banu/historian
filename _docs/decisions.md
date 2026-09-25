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

2026-09-01 - An INTEGER literal past int64 max becomes a float,
enforced in sql/parser.py

Issue #17: SQLite's integers are int64
(-9223372036854775808..9223372036854775807). Confirmed against
sqlite3 3.51.0:

    select 9223372036854775807, typeof(9223372036854775807);
    9223372036854775807|integer
    select 9223372036854775808, typeof(9223372036854775808);
    9.22337203685478e+18|real
    select 9223372036854775808 = 9223372036854775809;
    1

A decimal literal that overflows becomes a REAL, and every literal
past the boundary that rounds to the same double compares equal to
every other one that does. Python's int() never overflows, so
without an explicit check historian would keep such literals
distinct and disagree with SQLite. sql/parser.py's literal
construction now parses an INTEGER token's digit text with int();
if the result exceeds 9223372036854775807, it constructs float()
from the same text instead. The lexer never emits a signed INTEGER
token - a leading `-` is always its own MINUS token - so the rule
is one-directional and there is no negative bound to check here.

This is unrelated to the 2026-08-27 "numeric comparison is exact;
no float() anywhere" decision, and the two must not be conflated.
That decision governs comparison in values.py, which stays exact
and is pinned by a test at the 2^53 case - nothing here adds a
float() call there. What is decided here is literal construction
in sql/parser.py, which happens before any value ever reaches
values.py.

Not implemented: SQLite special-cases a unary minus written
directly against the int64-min literal, so
typeof(-9223372036854775808) is integer even though the unsigned
digit sequence alone (9223372036854775808) overflows to REAL.
Checked whether Part A's grammar can observe the gap between that
and the naive path (negate an overflowed float) - it cannot: 2^63
is exactly representable as a double, so -9223372036854775808.0
compares exactly equal to the true int -9223372036854775808 under
Python's (and SQLite's) exact int/float comparison, and this
grammar has no typeof() and no arithmetic to tell them apart by.
They diverge only once arithmetic exists (-9223372036854775808 + 1
would round incorrectly starting from the float), which has no
home until exec/expression.py (#12). Recorded now, matching the
NaN-yields-NULL precedent above, so whoever builds arithmetic knows
to revisit it rather than rediscover it.

2026-09-01 - Deeply nested expressions raise ParseError, not
RecursionError; two limits, not one

Issue #8 round 1 (QA FAIL): `SELECT` + 89 `(` + `1` + 89 `)` +
`FROM blame` parsed; 90 raised a raw, unhandled RecursionError -
the exact traceback spec §5 forbids. Root cause: `_parse_expr`
descends through nine more precedence methods to `_parse_primary`,
which recurses into `_parse_expr` again for a parenthesised
group's contents, so one level of `(...)` nesting costs several
Python stack frames, not one.

Measured before changing anything, with `sys.getrecursionlimit()`
at its default of 1000. Frames consumed per level of nesting, by
instrumenting `_parse_primary` and reading the call-stack depth at
each visit: bare parens 12, function-call arguments 15, IN-list
values 6, a NOT chain 1, a unary +/- chain 1. These numbers explain
the observed crash points exactly (1000 / 12 = 89.9, matching the
89-parses/90-crashes boundary) and are not expected to be stable
across Python versions or builds - the fix does not depend on
their staying exact, only on having measured rather than guessed
them.

`sqlite3 3.51.0` accepts 90 levels of bare parens and returns 1
(AGENTS.md: "where historian and SQLite disagree, SQLite is
right"), so a depth limit below 90 would itself be a new
disagreement, not a fix. Checked what SQLite itself does at
depth, by binary search against a live sqlite3 process:

    bare parens:            93 ok, 94 fails
    nested function calls:  31 ok, 32 fails
    nested IN-lists:        31 ok, 32 fails

All three fail the same way: "Error: in prepare, parser stack
overflow" - SQLite's own LALR parser stack, not its documented
`SQLITE_MAX_EXPR_DEPTH` (confirmed present at its default of 1000
via `PRAGMA compile_options`, but never reached for any of these
three shapes on this build).

Two consequences. First, `sys.setrecursionlimit` is not a fix -
it relocates the cliff and risks a hard interpreter crash in place
of a catchable exception. Second, catching RecursionError and
re-raising as ParseError is a patch on the symptom: the real limit
would still be however much stack Python's own machinery happened
to have left, which depends on where `parse()` was called from -
breaking the determinism AGENTS.md requires ("the same repository
and the same query always produce the same rows"). The fix has to
count depth explicitly and check it before recursing, the way
SQLite counts `SQLITE_MAX_EXPR_DEPTH` rather than relying on its
own C call stack.

A single counter does not work in either direction. A run of `(`,
`NOT`, or unary `+`/`-` is pure repetition with no semantic content
of its own - `(((x)))` is exactly `x` - so `sql/parser.py` now
parses each such run with an explicit loop instead of recursing
once per token: `_parse_primary`, `_parse_not`, and `_parse_unary`.
A loop costs one iteration per token, not one Python stack frame,
so these three forms are bounded by a generous limit,
`_MAX_NESTING_DEPTH = 1000`, matching SQLite's own documented
default rather than its build-specific parser-stack quirk - this
is the number the issue's guidance pointed at directly, and it
clears all three measured SQLite boundaries above with large
margin.

Genuine recursion remains where a *new* sub-expression is parsed
from within another one: a parenthesised group's contents, a value
inside an `IN (...)` list, or a function-call argument. These
still go through `_parse_expr` calling itself and really do cost
Python stack frames, so they are bounded separately by
`_MAX_RECURSION_DEPTH = 50`, sized from the measured 15
frames/level worst case (function-call arguments) with several
times the margin measured as already used before `parse()` is even
called (31 frames, observed under pytest) - and still clears
SQLite's own 31-level boundary for these two forms. Collapsing
this to one limit does not work: low enough to be safe for genuine
recursion is too low to accept plain deeply-parenthesised input
SQLite itself accepts, and high enough to match SQLite's declared
1000 would let genuine recursion exhaust Python's real call stack
before the counter ever fires.

Not a rewrite of the precedence-climbing structure that passed QA
in round 1 - only these four call sites change, and `(expr)`
continues to produce no AST node of its own, so the loop-based
paren handling is behaviourally identical to the recursive version
it replaces.

2026-09-01 - Fixture builds isolate from all ambient git
configuration, not just the six GIT_* variables

Issue #10 grooming found that spec §4's six GIT_AUTHOR_*/
GIT_COMMITTER_* variables are necessary but not sufficient for
byte-identical fixtures. Confirmed on this machine (git 2.50.1,
Apple Git-155), with all six identical either way:
`core.autocrlf=input` versus `core.autocrlf=false` produces
different blob hashes for the same content, and a global
`core.excludesFile` with a `*~` pattern silently drops a matching
fixture file from `git add -A` with no error at all - no warning,
no nonzero exit, just a fixture with one fewer file than intended.
`commit.gpgsign=true` with no working signing key makes the commit
fail outright, and `core.fileMode=false` changes what mode gets
recorded at add time, not only what a later diff reports.

`init.defaultBranch` is the sharpest case. Pointing
`GIT_CONFIG_SYSTEM` at `/dev/null` alone does not suppress it on
this platform, because Apple's Command Line Tools git ships its
own hardcoded system-scope gitconfig, one directory below its
install root, that `GIT_CONFIG_SYSTEM` does not point git away
from - `GIT_CONFIG_NOSYSTEM=1` is required in addition. Verified
directly: `GIT_CONFIG_SYSTEM=/dev/null` alone still resolves
`init.defaultBranch` to "main" from Apple's file; adding
`GIT_CONFIG_NOSYSTEM=1` falls back to git's real compiled-in
default, "master".

Considered enumerating and overriding every setting found to
matter - the six env vars plus the four above - and rejected it,
because a list like that is exactly the shape of thing that is
missing an entry: the platform-specific system config above was
found by testing, not by reading documentation, and a different
contributor's machine can have another one nobody has hit yet. The
fixture builder instead inherits nothing: `GIT_CONFIG_GLOBAL` and
`GIT_CONFIG_SYSTEM` set to `/dev/null`, `GIT_CONFIG_NOSYSTEM=1`,
every other ambient `GIT_*` environment variable stripped before
any git subprocess runs, an explicit branch name passed to every
`git init` rather than relying on any default, and the settings
above still set explicitly as local repo config immediately
afterward - defense in depth on top of the environment-level
isolation, not instead of it. Proven, not just argued: the same
fixture built under a hostile ambient config (autocrlf, gpgsign,
excludesFile and defaultBranch all set adversarially) produces an
identical HEAD, tree and full object set to a build with no
ambient config present at all.

spec.md §4 is edited in this commit. Its "so all of them are set
explicitly," following the six variables, read as a complete
recipe and was not one; per _docs/process.md, a spec edit that
turns out wrong lands in the same commit as the decision that
found it wrong. tiny and awkward now also pin their HEAD hash as
an assertion, so a determinism regression fails immediately rather
than waiting to be noticed downstream. `large` (spec §4's third
fixture) is out of scope here - split into issue #27, since it
gates no correctness test and needs its own build and caching
decisions.

2026-09-01 - Git bytes are decoded as UTF-8 with errors="replace",
not surrogateescape

Settled while grooming #11, answering the question #20 asked.
Confirmed directly:

    surrogateescape  ->  UnicodeEncodeError: surrogates not allowed
    replace          ->  inserts, round-trips equal

surrogateescape produces a Python str that Python's own sqlite3
module cannot insert at all. Since §1 makes SQLite the definition
of correct, and M3's differential harness loads every scanned row
into SQLite, that choice would have made the oracle itself
unusable - the failure would have surfaced in M3 as a harness
crash, not as a decoding bug, long after #11's context was gone.
`errors="replace"` inserts cleanly and round-trips equal, and is
tables/blame.py's `_decode`, used for both `path` and a blamed
`line`.

Consequence for values.py: `errors="replace"` can never produce a
lone surrogate, which is what makes that module's UTF-8-ordering
assumption true. Not a general fact about Unicode - false for a
lone surrogate, which is exactly what #20 asked about - but a
narrow one about every string this decode call can ever produce.
tests/test_values.py's docstring on
test_text_comparison_is_bytewise_for_non_ascii was corrected in
this same branch to state the narrow claim instead of the general
one.

Known gap, recorded rather than fixed: neither `tiny` nor
`awkward` exercises non-UTF-8 bytes in a *path*, only in file
content (`awkward`'s binary.bin). It is the same decode call
either way, so this is a coverage remark, not an open design
question.

2026-09-01 - Negating the int64-min literal is fixed
structurally in exec/expression.py, not completely - sql/ast.py
has no field to tell it apart from an equal-valued REAL literal

Closes the gap the 2026-09-01 "An INTEGER literal past int64
max becomes a float" entry left open for #12. `sql/parser.py`
parses the bare digit sequence "9223372036854775808" (no
decimal point) as the float 9223372036854775808.0, since it
overflows int64 as an INTEGER token - so `-9223372036854775808`
written directly in source parses to `UnaryOp(NEG,
Literal(9223372036854775808.0))`. Confirmed against sqlite3
3.51.0 that naively negating that float loses the point once
arithmetic follows: `-9223372036854775808.0 + 1.0` rounds right
back to the same double (the ULP at 2**63 is 2048), while real
SQLite keeps `-9223372036854775808 + 1` an exact integer,
-9223372036854775807.

exec/expression.py now special-cases exactly this AST shape -
`UnaryOp(NEG, Literal(v))` where `v == 2**63` - and returns the
exact int64 minimum directly, bypassing the general negate-
then-bound path. This also fixes the division-overflow example
named in #12's own criteria (`-9223372036854775808 / -1`),
though that one happened to already agree either way and so
did not itself prove the fix necessary - the `+ 1` case above
is what does.

The fix is necessarily incomplete, and this records why rather
than hiding it: `sql/ast.py`'s `Literal` carries only a `Value`,
with no field saying whether it came from an INTEGER token that
overflowed or a REAL token spelled with an explicit decimal
point. Confirmed directly against sqlite3: `-9223372036854775808.0`
(explicit ".0") stays REAL under negation - real SQLite does not
special-case it - but exec/expression.py cannot tell the two
spellings apart once parsed, since `sql/parser.py` (out of scope
for #12) already erases the distinction into the identical float
value by the time either reaches this module. The structural
check therefore also mis-types the rare ".0"-spelled case as
INTEGER instead of REAL. Chose to fix the far more common
bare-digit spelling - what a hand-written query or the fuzzer
would actually produce - at that cost, rather than leave both
spellings broken waiting on a `Literal` field that belongs to a
different issue (`sql/ast.py` is off-limits for #12).

2026-09-15 - An IN/NOT IN list has no affinity of its own, ever;
BETWEEN's bounds keep theirs

Found by the M2 milestone review (#47), not by a test: `_eval_in`
called the same `_evaluate_affinity_pair` helper as `=`, `IS`, and
`_eval_between`, applying column affinity to each list element the
same way `=`'s right operand gets it. SQLite's rule for IN is
narrower than that: the right-hand side of IN/NOT IN with a list has
no affinity at all, full stop, regardless of what kind of expression
the element itself is - not "usually," not "unless the element is
itself a bare column."

Confirmed against sqlite3 3.51.0, t(n INTEGER, s TEXT, r REAL), row
(5, '5', 5.0):

    select '5' in (n);   -> 0
    select '5' in (r);   -> 0
    select 5 in (s);     -> 0

Each was 1 under the old code: a bare-column element converted the
literal to the column's own declared type before comparing, treating
the element exactly like `=`'s right operand. This reached the
shipped CLI - `'1' IN (line_no)` returned 42 rows instead of 0.

The trap is that BETWEEN looks structurally identical in the same
file - it walks the same shape of operand pair through the same
shared helper - and is not governed by the same rule. Each BETWEEN
bound is an independent right-hand operand, symmetric with `=`, and
keeps its own affinity. Confirmed for the identical operand shape,
same row (n = 1): `'1' BETWEEN n AND n` -> 1, while `'1' IN (n)` ->
0 for that same row. A fix that unifies the two operators onto one
code path reintroduces this bug in the other direction.

The fix adds `right_has_affinity` to `_evaluate_affinity_pair`, an
explicit keyword forcing the list side's affinity to None; only
`_eval_in` passes `right_has_affinity=False`, and `_eval_between` is
untouched. Recorded here on the precedent of the 2026-08-27 "column
affinity lives in the expression evaluator" entry - the same category
of mismatch, an operator implemented by analogy to `=` where SQLite's
actual rule diverges, now proven to reach a real query rather than
staying hypothetical.

2026-09-16 - tests/extraction/ and tests/differential/ get an
__init__.py each, to stop a same-basename test_blame.py collision

Issue #59 gives tests/differential/ its own test_blame.py, and §4's
layout already names tests/extraction/test_blame.py (#11) next to
it. Neither directory had an __init__.py, and pyproject.toml sets no
pytest import mode, so under pytest's default "prepend" mode a test
module's name comes from its bare basename - both files became
module "test_blame" and the whole suite failed to collect:

    import file mismatch: imported module 'test_blame' has this
    __file__ attribute: .../tests/differential/test_blame.py which
    is not the same as the test file we want to collect:
    .../tests/extraction/test_blame.py

Three ways out were on the table: add __init__.py to both
directories, set --import-mode=importlib globally, or rename a
file. importlib mode was tried and rejected first, not assumed -
it stops pytest from prepending anything to sys.path, which breaks
tests/conftest.py's existing `from fixtures.build import ...`
outright (ModuleNotFoundError), and fixing that reaches into a file
this issue has no reason to touch. Renaming a file was rejected for
losing §4's layout symmetry between the two directories.

__init__.py has direct precedent already - tests/fixtures/ has had
one from the start - and §4 names two more files landing in these
same two directories later: tests/pushdown/test_blame_pushdown.py
(M4) and tests/differential/test_regressions.py (M5). A future
basename collision between any of these is closed by the same fix,
not reopened.

One consequence, found by running it rather than by predicting it:
once a directory has an __init__.py, pytest imports its conftest.py
under a dotted name (`differential.conftest`) rather than the bare
`conftest`, and - because `tests/` itself has no __init__.py of its
own - a bare `from conftest import ...` inside tests/differential/
test_blame.py silently resolves to the unrelated top-level
tests/conftest.py instead of raising, once that module is already
in sys.modules from collecting anything else first. It fails
loudly enough in practice (ImportError: cannot import name
'assert_rows_match' from 'conftest') that it was caught immediately,
but it would not fail loudly for two conftest.py files that
happened to share a name. Fixed by importing it as
`from differential.conftest import ...`, its real dotted name now
that the package exists.

2026-09-18 - The two #38 coercion helpers are named coerce_to_value
and coerce_to_bool3

Issue #38 settled that these live as two caller-side functions in
exec/expression.py but left their names open. Named for the
direction they coerce *into*, matching the two type aliases
values.py already exports: `coerce_to_value` (Bool3 -> Value, called
by Project) and `coerce_to_bool3` (Value -> Bool3, called by
Filter, ahead of values.is_true).

Also recorded here since #38 asked for it explicitly: with
`WHERE line_no` now legal, tests/test_operators.py's old
TypeError-pinning test for Filter's three-valued-logic invariant
(added after #34's QA FAIL) is replaced by a predicate where
SQLite's leading-prefix truthiness and Python's own truthiness
disagree - `WHERE '0abc'`. As a bare Python string it is truthy
(nonempty), so a Filter that fell back to bare `if evaluate(...):`
on the raw Value would keep every row; the coercion reads it as
numeric `0`, correctly dropping all of them. This is the same kind
of predicate #34's original QA finding was about - one where a
wrong implementation and a right one visibly disagree - reapplied
to the new shape of the gap #38 closes.

2026-09-18 - coerce_to_bool3 also called from inside evaluate()'s
own And/Or/Not branches, not only by Filter

#38's first round called coerce_to_bool3 exactly twice - once from
Filter, once (coerce_to_value) from Project - on the reasoning,
stated in the issue's own Design recommendation section, that the
Value/Bool3 ambiguity "only ever exists at exactly two points: the
root of a WHERE/HAVING predicate, and each select-list item's
root." QA's round-1 review found that premise false against
sqlite3 3.51.0: SQLite applies the identical leading-prefix
truthiness independently to *each operand* of AND, OR and NOT, not
only at those two roots - confirmed with plain arithmetic and no
comparison anywhere in the query (`select (3-3) and 1;` -> `0`;
`select not(3-3);` -> `1`). A value-shaped operand nested under
AND/OR/NOT reached values.and3/or3/not3 raw and raised TypeError.

Fixed by having evaluate()'s own And/Or/Not branches wrap each
operand's result in coerce_to_bool3 before calling
values.and3/or3/not3 - still no `position` parameter on evaluate(),
per the issue's own constraint. And/Or/Not's operands are predicate
positions unconditionally, a property of the node evaluate() is
already dispatching on when it reaches that branch, not something a
caller has to pass in. coerce_to_bool3 itself needed no change -
only a second call site, one recursion level deeper than before.

Audited the rest of evaluate() for the same hole while in there, in
both directions:

- Every other predicate-shaped node (Is, Like, In, Between) already
  builds its Bool3 result from values.py's own comparison functions
  (values.eq/ge/le/is_/is_not), never from a raw evaluate() result
  fed straight to a Bool3-only function - so none of them had this
  gap, and none needed a change.
- The reverse direction - a Bool3-shaped node (a comparison,
  And/Or/Not, Is, Like, In, Between) nested where a Value is
  required - is a real, separate gap, confirmed live: `SELECT
  (1 = 1) = 1 FROM blame`, `SELECT (1 = 1) || 'x' FROM blame`,
  `... WHERE path LIKE (1 = 1)`, `... WHERE line_no IN (1 = 1, 2)`
  and `... WHERE line_no BETWEEN (1 = 1) AND 5` each raise
  TypeError from values.py's or exec/expression.py's own bool
  guards, where sqlite3 returns real answers. Arithmetic (+ - * /
  and unary +/-) happens not to hit this, only because Python's
  `bool` already behaves as 0/1 under its own arithmetic operators -
  an accident of the host language, not a coercion this codebase
  chose. This is not #38's hole reopened in a new place; it is a
  materially larger change (every Value-consuming node in the
  grammar, not three), it was not named in #38 or its QA FAIL, and
  fixing it here would be exactly the scope creep AGENTS.md and the
  software-engineer role warn against. Left unfixed, reported on
  the issue for a follow-up to pick up.

2026-09-18 - WHERE's alias fallback is one resolution function
with a precedence-direction parameter, not a WHERE-specific helper

Issue #32. `sql/binder.py` gains `_resolve_name(ref, ctx,
select_items, alias_first)`: for an unqualified `ColumnRef` that
fails ordinary schema lookup, it also tries the select list's own
aliases (first-occurrence-wins on a duplicate, matched via the
existing `_same_name` ASCII fold) and splices in that item's
already-bound expression - `BoundSelectItem.expr` - in place of the
reference, via the same `dataclasses.replace` rebuild `_bind_expr`
already used for every other node type. Confirmed against sqlite3
3.51.0 that a real column always wins over a same-named alias in
WHERE (`select b as a from t where a = 1` returns the real column's
row), so `bind()` passes `alias_first=False` for WHERE. Also
confirmed ORDER BY is the one clause where this is reversed - the
alias wins there - so the direction is a parameter rather than
hardcoded, for #60 (GROUP BY, HAVING - column-first, same as WHERE)
and #61 (ORDER BY - alias-first) to call without redeciding the
rule. `_bind_expr` grew matching `select_items`/`alias_fallback`/
`alias_first` parameters, defaulted off, threaded through every
recursive call so the fallback reaches a ColumnRef at any depth in
WHERE's tree, not only at the top; `_bind_select_item` still calls
`_bind_expr` with the defaults, so select-list items continue to
not see each other's aliases (unchanged, per #32's own finding 3).
A table-qualified reference (`t.x`) never falls back to an alias,
confirmed against sqlite3 - aliases have no table qualifier - so a
qualified ColumnRef skips straight to schema-only resolution as
before. No new AST node; `_docs/spec.md` needed no change since it
already left this as a binder-level SQLite-conformance rule rather
than a separately specified grammar feature.

`tests/test_binder.py`'s and `tests/differential/test_blame.py`'s
prior pinning tests for the old "conservatively rejected" behaviour
are rewritten to assert the new, correct resolution rather than
deleted - the gap they documented is what this issue closes.

2026-09-18 - Exit code 4 is an internal-error backstop; §3's
"never a traceback" applied to a case it didn't name

Issue #49. `cli.py`'s `main` caught exactly four query-error
types plus `(OSError, RuntimeError)` for an unreadable repository,
with no fallback - any other exception, including a bug in
historian itself (most recently #63's `TypeError` from `SELECT
(1=1) = 1 FROM blame`), reached the user as a Python traceback.
Spec §3 and §5 both say "never a traceback" without carving out
an exception for this case; it is a gap in the enumeration, not a
conflict with it, since an internal invariant failure is not a
parse error, a binding error, or a rejection of known-unsupported
grammar. `main` gains two more `except` clauses, added to the
chain rather than rewriting what was already there: `except
BrokenPipeError`, ordered first, returning 0 with nothing written
- confirmed `issubclass(BrokenPipeError, OSError)` is `True`, so
without its own clause a broken pipe (stdout closed under a `|
head`) would be misreported as "could not read repository" by the
existing `(OSError, RuntimeError)` clause; and `except Exception`,
ordered last, printing a fixed message that never repeats `str
(exc)`, the exception's class name, or a traceback, returning a
new exit code, `4`. Confirmed `issubclass(KeyboardInterrupt,
Exception)` is `False`, so `except Exception` (never `except
BaseException`) leaves `KeyboardInterrupt` to propagate
uncaught, same as today. The guarded region widens to include
rendering and writing the result (`_render_table`,
`sys.stdout.write`), previously outside the `try` entirely and
therefore unprotected even from the four original exception
types - a bug in rendering, or a closed pipe on the write, could
only ever be caught by moving the write inside the same `try`.
Tested by injection rather than by a real bug or a real `| head`:
a test-local exception class, never one of historian's own,
raised from a monkeypatched `tree.rows()` or `_render_table`, so
the test doesn't depend on which internal bugs exist at any given
moment (in particular, not on #63's two live reproductions, which
are unrelated fixes). A real `| head` against a small fixture
repository does not reproduce the bug at all - the output fits in
the pipe buffer and the process exits 0 before the pipe closes -
so no integration test attempts one. `_docs/spec.md` §5's
exit-codes line is edited in the same commit to list `4`, per
process.md's rule that a decision contradicting the spec edits
the spec alongside it; here the spec was silent rather than
wrong, but the same rule was followed to keep the exit-codes line
complete rather than leaving the fourth code undocumented.
2026-09-18 - AS stays mandatory before a select-list alias;
SQLite's bare form is not adopted

Issue #25 (a re-grooming; the previous PM correctly flagged
the tension but left it open). Decision: `SELECT path p FROM
blame` keeps raising `ParseError`. `_docs/spec.md` §1's
grammar line, `<expr> [AS alias]`, already says this and is
unchanged - only the reasoning was missing.

This is a syntactic narrowing, the same category as rejecting
`INSERT` or a subquery, not a semantic disagreement with
SQLite about what a query means. AGENTS.md's "where historian
and SQLite disagree, SQLite is right" and spec §1's "semantics
follow SQLite exactly" are the oracle for three-valued logic,
coercion and NULL handling - they were never a mandate to
accept every surface SQLite accepts, and reading them that way
would also require `INSERT` and subqueries, which §1's
non-goals rule out deliberately. So the oracle does not settle
this by itself; two confirmed findings tip it toward keeping
`AS` mandatory:

1. The dropped-comma ambiguity is real, not hypothetical.
   Confirmed against sqlite3 3.51.0: `create table t(a
   integer, b integer); insert into t values(1,99); select a
   b from t;` returns one row, `99`, headed `b` - no error.
   `a`'s value (`1`) never appears and nothing is reported. A
   user who meant `SELECT a, b` and dropped the comma is
   silently handed `SELECT a AS b` instead. historian's
   mandatory `AS` turns that exact typo into a `ParseError`
   rather than a silent wrong answer - a real safety property
   the bare form would trade away.
2. SQLite's own bare-alias keyword list is arbitrary from
   historian's point of view. Checked all 30 of historian's
   lexer keywords (`src/historian/sql/lexer.py`'s
   `KEYWORD_TYPES`) as a bare alias against sqlite3: 25 are
   rejected (`select`, `distinct`, `as`, `from`, `inner`,
   `join`, `on`, `using`, `where`, `group`, `having`, `order`,
   `limit`, `and`, `or`, `not`, `like`, `in`, `between`, `is`,
   `null`, `case`, `when`, `then`, `else`) and exactly 5 are
   accepted (`by`, `asc`, `desc`, `offset`, `end`) - and
   `where` still errors even written explicitly as `select
   path as where from t`. That split is SQLite's own internal
   reserved-word table, not a rule derivable from historian's
   grammar; matching it would mean hardcoding a 5-keyword
   allowlist with no organic justification here.

The fuzzer (spec §4, not yet built) cannot arbitrate either:
once it exists it only ever emits what its grammar declares,
so it will only ever generate `<expr> AS alias` regardless of
which way this is decided. Neither side gets fuzzer coverage
for the bare form either way, so that cost/benefit is a wash.

Net: mandatory `AS` keeps a verified footgun closed and avoids
importing an arbitrary slice of SQLite's reserved-word table
for no reason a reader of this grammar could reconstruct.

Changed alongside this: the parser's `expected FROM, found
identifier 'p'` message for the bare-alias/dropped-comma shape
now says so explicitly - `expected ',' or FROM, found
identifier 'p' - a select-list alias requires AS before it` -
so the rejection points at what to add instead of just naming
the token it did not expect.

2026-09-19 - A digit run glued to an identifier character is
one bad token, `LexError`, not two good ones (issue #22).
`sqlite3` 3.51.0 rejects `3abc`, `1²`, `3café`, `0y`, `3from`
and `1select` as "unrecognized token"; historian's lexer used
to split each into `INTEGER` then `IDENTIFIER`, which let
`SELECT 3abc FROM blame` reach #25's bare-alias `ParseError`
and be told to add `AS` - a fix SQLite also rejects. The rule:
`_read_number` now checks the character immediately after a
completed digit run against `_is_identifier_start`, and raises
if it matches.

Excluded from that check: `e`, `E`, `x`, `X` and `_`. The first
four are scientific-notation and hex-integer markers (`1e10`,
`0x1f`) - real SQLite literals `_read_number` does not parse
yet, deferred to #6; rejecting them here would be a step
backward; treating a suffix as a marker without knowing where
it ends would need most of #6's own analysis. `_` was added
during this issue's grooming: `sqlite3` 3.51.0 accepts `_` as a
digit-group separator (`3_1` -> `31`, `1_000_000` -> `1000000`,
added upstream in 3.46.0), a feature named nowhere in this
repo before now and not implemented here - filed as #70. `_`
satisfies `_is_identifier_start`, so the unqualified version of
this rule would have turned `3_1` from "masked by #25" into
"rejects input SQLite accepts" - a regression the whole point
of this fix was to avoid introducing. Excluding these five
leaves `3e`, `3x`, `3_` and `3_abc` exactly as they were before
this issue (still wrong against SQLite, still not this issue's
problem to fix) rather than fixing them into a different wrong
answer.

The message - `"3abc is not a valid token: a number cannot be
directly followed by an identifier character, at line L,
column C"` - is historian's own wording, matching the
convention the lexer's other two multi-character-literal
messages already use rather than echoing `sqlite3`'s generic
"unrecognized token". It deliberately contains neither "AS"
nor "alias", so it can never be mistaken for #25's message even
though both are triggered by a digit run followed by letters -
confirmed by grep, and by the two call sites staying different
exception types (`LexError` in `cli.py`'s lexer branch,
`ParseError` in its parser branch). The position it carries is
the start of the digit run, matching where `sqlite3`'s own
`^--- error here` caret lands and the position convention the
lexer's other `LexError`s already use.

No change to `sql/parser.py`: #25's bare-alias branch fires on
any `IDENTIFIER` immediately after a select-list expression,
and still does, unchanged, for every shape that isn't a digit
run glued to an identifier (`SELECT path p FROM blame`, the
dropped-comma case). `tokenize("3abc")` now raises before
`parse()` is ever called, so that one input shape simply stops
arriving at the parser's branch at all - confirmed by running
both, not assumed from reading the branch condition.
2026-09-19 - a bare column mixed with an aggregate, no GROUP
BY, is a BindError - not SQLite's arbitrary row

Issue #60. `SELECT path, count(*) FROM blame` (no `GROUP BY`)
raises `BindError` in historian. Confirmed against `sqlite3
3.51.0` that this is legal there: `select a, count(*) from t`
(three rows, `a` = 1,1,2) returns one row, `a=1` - sqlite3's own
documentation calls this "an arbitrarily chosen row of the
group." historian does not adopt that behaviour.

This is not the usual "where historian and SQLite disagree,
SQLite is right" case (AGENTS.md; spec §1's "semantics follow
SQLite exactly"). That rule arbitrates a *disagreement about
what a query means*. Here there is nothing to arbitrate:
SQLite's own choice of row is undocumented and implementation-
defined, so there is no rule to copy in the first place - the
same category #25's AS-mandatory decision used ("SQLite is
permissive but the permissiveness has no principled shape to
copy").

Two findings make this more than a stylistic preference:

1. Adopting SQLite's behaviour would contradict AGENTS.md's own
   determinism rule: "the same repository and the same query
   always produce the same rows in the same order." A query
   whose answer depends on whichever row an engine happened to
   visit last cannot satisfy that guarantee - historian could
   not adopt SQLite's behaviour even if SQLite's own choice
   were documented and stable, because determinism is a
   property historian promises independently of what SQLite
   does.
2. It would also degrade the oracle rather than merely fail to
   help it. §4 compares historian against SQLite over the same
   rows. If historian picked one arbitrary row and SQLite
   picked a different arbitrary row, the differential harness
   would report a mismatch that is not a bug - and the natural
   response to a red differential test is to keep changing
   historian until it agrees, which is not possible here since
   neither engine's choice is principled. That is a false
   signal the suite has no way to tell apart from a real one.
   Rejecting the query at bind time removes the shape from the
   comparison entirely, rather than leaving a permanent,
   unfixable source of noise in it.

Net: this is the one case so far where matching SQLite is
incompatible with a rule the project already holds (determinism)
and where matching it would actively harm the machinery that
checks everything else (the oracle). Implemented in
`sql/binder.py`, after the whole select list is bound: when any
select-list item contains an aggregate call anywhere, every
item is walked for a bare column reference sitting outside every
aggregate call's own arguments, and the first one found raises,
naming the column. `count(path)` is unaffected - `path` there is
inside the aggregate's own argument, not bare. `GROUP BY` (#69)
will extend this rule rather than replace it: a bare column that
*is* one of the grouping keys becomes legal again once grouping
exists, but that is out of this decision's scope.

2026-09-24 - follow-on to 2026-09-19: the grouped-but-not-a-key
narrowing, an aggregate can never be a GROUP BY key, and HAVING
gets the same narrowing as the select list

Issue #69. Three additions to the 2026-09-19 entry above, not a
new decision from scratch - the same reasoning transfers rather
than being re-derived.

First, the narrowing now also covers "grouped but not a group
key". `select a, b, count(*) from t group by a` is legal in
sqlite3 (`create table t(a,b); insert into t values
(1,5),(1,6),(2,7),(2,8),(2,9);` gives `1|5|2`, `2|7|3` - `b`
takes some row's value per group, undocumented which, confirmed
live against sqlite3 3.51.0). historian raises BindError instead:
a select-list expression must be an aggregate call, a GROUP BY
key, or built purely from GROUP BY keys (an expression whose
every bare column matches some key, e.g. `a + 1` when `a` is a
key). The risk is identical to 2026-09-19's own case, just
triggered by GROUP BY grammar rather than a bare aggregate: a
non-key, non-aggregate column's value within a group is still
whichever row sqlite3 happened to visit last, so both findings
that entry gives (breaks AGENTS.md's determinism guarantee;
would feed the oracle two independently-arbitrary answers and
manufacture an unfixable false differential mismatch) apply here
without modification.

Second, an aggregate call can never be a GROUP BY key, however it
is named - direct, via a select-list alias, or by ordinal.
Confirmed live against sqlite3 3.51.0 during this issue's
dispatch, correcting an earlier grooming draft that had ordinal
resolution to an aggregate as "ludicrous but legal":

    sqlite> create table t(a,b); insert into t values(1,5),(1,6),(2,7);
    sqlite> select a from t group by count(*);
    Parse error: aggregate functions are not allowed in the GROUP BY clause
    sqlite> select count(*) as c from t group by c;
    Parse error: aggregate functions are not allowed in the GROUP BY clause
    sqlite> select b, count(*) from t group by 2;
    Error: in prepare, aggregate functions are not allowed in the GROUP BY clause

Unlike the two narrowings above, this is not a case of
historian refusing something sqlite3 permits - sqlite3 rejects
all three routes itself, identically. historian's `BindError`
for each is simply the same rule sqlite3 already enforces,
implemented once in `sql/binder.py` and applied uniformly
regardless of how the aggregate call is reached. An ordinal
resolving to a non-aggregate expression remains legal, and an
out-of-range ordinal (`select b from t group by 3` -> "1st GROUP
BY term out of range") is its own, separate BindError.

Third, orchestrator review of the first pass caught that this
narrowing had not been extended to HAVING, and that the gap is a
silent wrong answer, not merely an omission: `select count(*)
from t having path = 'x'` returns `3` in sqlite3 (evaluating the
bare `path` against an arbitrary row of the query's one implicit
group), and `select a, count(*) from t group by a having
path = 'z'` returns `2|1` the same way. Before this fix historian
ran both to completion and returned 0 rows - not an error, not
sqlite3's answer, just wrong, because `HAVING`'s own `Filter`
evaluated `path` against `Aggregate`'s output row, which has no
such column. The fix applies exactly the reasoning above to
HAVING: a bare column reference in HAVING must be a GROUP BY key
(matched by shape) or sit inside an aggregate call's own
arguments, whether or not GROUP BY is present - with no GROUP BY
there are no keys, so every bare column outside an aggregate is
rejected. `HAVING count(*) > 1` and `HAVING sum(x) > 3` (column
inside an aggregate's arguments) stay legal, as does referencing
a select-list alias of a key or an aggregate (resolved through
the same `#32` alias-fallback function, `alias_first=False`, that
GROUP BY already uses) and an expression key matched by shape
(`GROUP BY a + 1 HAVING a + 1 > 2`, confirmed legal against
sqlite3 before implementing).

2026-09-24 - HAVING on a non-aggregate query is a BindError

Issue #69, caught in QA/orchestrator review of the first pass
(the draft had HAVING with no GROUP BY and no aggregate call
anywhere fall through as an ordinary Filter - never recorded here
as a deliberate decision, only implemented, and wrong). Confirmed
live against sqlite3 3.51.0:

    sqlite> select path from t having path = 'x';
    Error: in prepare, HAVING clause on a non-aggregate query

historian now raises BindError for the same shape. Whether the
query is an aggregate query is decided by GROUP BY's presence or
an aggregate call in the select list alone - confirmed
`select count(*) from t having 1` succeeds (aggregate only in
the select list, HAVING's own predicate has none) while
`select path from t having count(*) > 1` still raises the
identical error (an aggregate call written in HAVING itself does
not by itself make the query aggregate). Implemented in
`sql/binder.py`, checked once after HAVING is bound.

2026-09-24 - ORDER BY resolves alias-first, the reverse of every
other clause, and gets the same grouped narrowing HAVING has

Issue #61. `sql/binder.py`'s `_resolve_name` (issue #32) has
carried an `alias_first` parameter since #32 landed, but nothing
called it with `True` until now - #32 deliberately scoped its own
testing to `alias_first=False` only, since no clause could
construct the other direction yet. Confirmed live against
sqlite3 3.51.0, independently of #32's own grooming:

    sqlite> create table t(a integer, b integer);
    sqlite> insert into t values(1,20),(2,10);
    sqlite> select a as real_a, b as a from t order by a;
    2|10
    1|20

Ordering by the alias `a` (= column `b`) puts `(2,10)` first;
ordering by the real column `a` would put `(1,20)` first - the
two disagree, which is what makes the data discriminating. Every
other clause with alias fallback (WHERE, GROUP BY, HAVING) has
the real column win; ORDER BY is the one exception. Confirmed by
mutation on this issue's own branch before implementing: with
`alias_first=True` reachable nowhere, a discriminating test
written against the intended behaviour failed exactly as
expected (`AttributeError`, no ORDER BY support at all, then a
wrong-offset assertion failure once grammar/binding existed but
the call site was still wired `False`); flipping the call site to
`True` made it pass, and flipping it back to `False` after
implementation reproduced the original failure - see `tests/
test_binder.py`'s `test_order_by_alias_wins_over_real_column_of_
the_same_name` and its own docstring for the mutation record.

Separately, ORDER BY gets the same "grouped but not a key"
narrowing the 2026-09-19/2026-09-24 entries above already give
the select list and HAVING, for the identical reason: once a
query aggregates (GROUP BY present, or an aggregate call in the
select list), a bare ORDER BY column that is neither an aggregate
call nor a GROUP BY key (nor built purely from GROUP BY keys) has
no principled value to sort by - confirmed live, `select k,
count(*) from g group by k order by v` succeeds in sqlite3 and
sorts by an arbitrary row's `v` per group, exactly the shape the
select list and HAVING already refuse. historian raises
`BindError` instead, reusing `_split_for_grouped_check` unchanged.
An aggregate call in ORDER BY is otherwise legal only once the
query already aggregates - confirmed live, `select p from u order
by count(*)` (no GROUP BY, no select-list aggregate) is "misuse
of aggregate: count()" in sqlite3, the same rejection WHERE gets,
while `select count(*) from u order by count(*)` succeeds. An
ordinal pointing at an aggregate is legal in ORDER BY, unlike in
GROUP BY - confirmed live, `select k, count(*) from g group by k
order by 2 desc` succeeds in sqlite3 while the identically-shaped
`GROUP BY 2` pointing at an aggregate is rejected.

2026-09-24 - the differential harness's `ORDER BY` comparison is
tie-tolerant, and the key it groups by is supplied, never inferred

Issue #61. `AGENTS.md` guarantees historian's own row order is
deterministic even among rows that tie on every ORDER BY key, but
SQLite makes no such promise - a naive positional comparison in
the differential harness would then report a false mismatch
whenever the two engines break an identical tie differently,
which neither engine's own contract calls a bug (the same
"oracle gives a false signal" shape #60 hit, per the orchestrator's
comment on this issue).

The grooming's original design grouped tied rows by their ORDER
BY key tuple, read back out of the output row. The orchestrator's
own correction caught the hole in that: the key is not always in
the output at all - `select p from u order by n` is legal SQL,
and the result rows carry only `p`, not `n`. `assert_rows_match`
(`tests/differential/conftest.py`) therefore takes the key's
position(s) as an explicit, caller-supplied parameter rather than
inferring it from the rows or parsing the query to recover it -
a second SQL front end in the oracle is exactly the complexity
the harness exists to avoid:

- `ordered=True, key_positions=(<output column>, ...)`: the key
  is selected. Rows are split into consecutive runs by their
  values at those positions; the sequence of distinct key tuples
  must match exactly, in position, and rows within a corresponding
  tied group compare as a multiset.
- `ordered=True, key_positions=None`: the key is not selected, so
  the harness cannot see ties at all. The calling test must make
  the case tie-free by construction and prove it on the SQLite
  side - `tests/differential/test_blame.py`'s `test_order_by_
  aggregate_not_in_the_select_list` checks `count(*)` is distinct
  across `author_name`'s groups before trusting exact-order
  comparison, so a fixture change that later introduces a tie
  fails loudly as a broken test rather than as a false historian
  bug.

historian's own tie order is deterministic by construction, not
merely as an aspiration: `Scan`/`Filter`/`Aggregate` already never
reorder rows, so the row order reaching `Sort` is already fixed
for a given repository and query, and `Sort`'s own stable,
per-key sort (applied per `values.py`'s existing multi-key
contract) preserves that original relative order among ties
rather than needing separate bookkeeping - `tests/test_operators.
py`'s `test_sort_is_stable_among_rows_tied_on_every_key` and
`tests/differential/test_blame.py`'s `test_order_by_same_query_
twice_gives_identical_order` both check this directly, not
through the oracle.

2026-09-24 - ordinal detection widened to any nesting of unary
+/- (and parentheses) around an integer literal, in both GROUP BY
and ORDER BY - amends #69

Issue #61, orchestrator review after this issue's first pass
landed. `#69`'s original `GROUP BY` ordinal check (`_bind_group_
by_item`) recognised only a bare integer `Literal`, and this
issue's own `ORDER BY` ordinal check (`_bind_order_by_item`)
copied that shape plus one level of unary unwrapping - both too
narrow. Confirmed live against sqlite3 3.51.0: *any* nesting of
unary `+`/`-` around an integer literal is an ordinal in both
clauses, not just zero or one levels:

    sqlite> create table u(p,n); insert into u values('x',3),('y',1),('z',2);
    sqlite> select p from u order by +(+1);   -- ordinal 1
    sqlite> select p from u order by -(-1);   -- ordinal 1
    sqlite> select p from u order by -(-(1)); -- ordinal 1
    sqlite> select p from u order by - -1;    -- ordinal 1 (no parens at all)
    sqlite> select p, count(*) from u group by +1;     -- ordinal 1
    sqlite> select p, count(*) from u group by -(-1);  -- ordinal 1
    sqlite> select p, count(*) from u group by +(+1);  -- ordinal 1

A binary operator anywhere in the tree is never an ordinal, in
either clause - confirmed `GROUP BY 1+0` and `ORDER BY 1+0` are
both a constant expression, not ordinal 1 (the latter leaves rows
in scan order rather than resorting - a stable sort over a key
that ties on every row, since `1+0` evaluates the same for every
row). `GROUP BY 1+0` staying a `BindError` (the select list's
non-key, non-aggregate column has no group to belong to, since a
constant key groups the whole table into one implicit group with
no real key to match by shape) is **unaffected by this fix and
must stay** - `1+0` was never an ordinal before this fix and still
is not after it; the fix only widens which *unary-wrapped*
literals count, not which binary expressions do.

Fixed with one shared helper, `_ordinal_value` (`sql/binder.py`),
recursing through arbitrarily many `UnaryOp` layers down to a
bare integer `Literal` and applying each layer's sign, used by
both `_bind_group_by_item` and `_bind_order_by_item` in place of
their own previous, narrower checks - parentheses need no
handling of their own, since `sql/parser.py`'s `_parse_primary`
already strips them at parse time and they never reach the binder
as a node. `-(-1)` unwraps to `1` (a legal ordinal); `-1` and
`-(1)` both unwrap to `-1` (out of range, `BindError`, matching
sqlite3's identical rejection there).

2026-09-25 - LIMIT/OFFSET's <n> narrows to a literal integer,
reusing _ordinal_value - not a general constant expression

Issue #77. `LIMIT`/`OFFSET` accept exactly what `_ordinal_value`
(`sql/binder.py`, built for #61's `GROUP BY`/`ORDER BY` ordinals)
already recognises: a bare integer `Literal`, optionally wrapped
in any nesting of unary `+`/`-`. Everything else sqlite3 3.51.0
itself accepts in this position - confirmed live this session,
not carried over from #61's own grooming notes - is a `BindError`:

    sqlite> create table t(a integer, b text);
    sqlite> insert into t values (1,'a'),(2,'b'),(3,'c'),(4,'d'),(5,'e');
    sqlite> select * from t limit 1+1;            -- 2 rows, legal
    sqlite> select * from t limit case when 1=1 then 2 else 3 end;  -- legal
    sqlite> select * from t limit (1=1);           -- 1 row, legal
    sqlite> select * from t limit abs(-2);         -- legal (scalar fn)
    sqlite> select * from t limit max(2,3);        -- legal (2-arg scalar max)
    sqlite> select * from t limit count(*);        -- misuse of aggregate function count()
    sqlite> select * from t limit a;               -- no such column: a
    sqlite> select a as n from t order by a limit n;  -- no such column: n (no alias fallback)
    sqlite> select * from t limit '2';             -- 2 rows (numeric-affinity TEXT)
    sqlite> select * from t limit '2.5';           -- datatype mismatch
    sqlite> select * from t limit 2.5;             -- datatype mismatch
    sqlite> select * from t limit 2.0;             -- 2 rows (MustBeInt's zero-fraction rule)
    sqlite> select * from t limit NULL;            -- datatype mismatch

This is a syntactic narrowing, the same category as #25's
AS-mandatory decision and #61's own ordinal precedent, not a
semantic disagreement with SQLite about what a query means -
AGENTS.md's "where historian and SQLite disagree, SQLite is
right" arbitrates the grammar historian does accept, not a
mandate to accept every surface SQLite accepts (§1's non-goals
already carve out subqueries and more on exactly this basis).
Three things tip this toward the narrow reading: (1) it is
architecturally free - `_ordinal_value` already exists, already
shared by two clauses, and needs no new evaluation machinery in
`sql/binder.py`/`plan/planner.py`, both of which otherwise only
ever assemble or reshape `Expr` trees and leave value computation
to `exec/expression.py`'s per-row `evaluate()` - `LIMIT`/`OFFSET`
have no row to evaluate against; (2) replicating sqlite3's own
accepted grammar in full means replicating `MustBeInt`'s exact-
zero-fractional-part REAL rule and numeric-affinity TEXT
coercion, genuine SQLite-internals trivia nobody archaeology-
querying a git repo would type by hand; (3) it matches the
ordinal precedent this project already committed to twice
(`GROUP BY`/`ORDER BY`, most recently widened above), for a
clause that is if anything *stricter* than either in sqlite3
itself (no alias fallback, no column reference at all). No range
check, unlike an ordinal: 0 and any negative resolved value bind
successfully - see the runtime-semantics entry below.

Rejected via `LIMIT`/`OFFSET`'s own `BindError`
(`"LIMIT/OFFSET must be a literal integer, optionally wrapped in
unary +/- and parentheses"`), not a `ParseError` - `sql/parser.py`
parses `LIMIT <expr>`/`OFFSET <expr>` generically via the same
`_parse_expr()` `ORDER BY`'s own item uses, deferring the
literal-integer-vs-anything-else decision to the binder exactly
as `ORDER BY`'s ordinal does.

2026-09-25 - the LIMIT comma form (LIMIT m, n) is out of scope,
rejected by name rather than falling through to a bare token error

Issue #77. `_docs/spec.md` §1's grammar line, `LIMIT <n> [OFFSET
<n>]`, has no comma in it. Confirmed live that the comma form
means `LIMIT n OFFSET m` - the *reverse* argument order from the
`OFFSET` spelling already in scope:

    sqlite> select * from t limit 3, 2;         -- rows 4,5
    sqlite> select * from t limit 2 offset 3;   -- rows 4,5 (same)

Not implemented, for the same reason #61's own grooming gave for
declining other SQLite surface syntax: it is a second, differently-
ordered spelling of a clause historian can already express in full
via `OFFSET`, and the swapped argument order is a well-known
footgun (inherited by SQLite for MySQL compatibility) rather than
a capability query authors would otherwise lack. `sql/parser.py`
recognises the shape explicitly - a comma immediately following a
bound `LIMIT` expression - and raises `ParseError` naming the
comma form and pointing at `OFFSET` instead, rather than letting
it fall through to `expect_end()`'s generic "expected end of
query, found ','", matching §3's "Unsupported grammar" rule
("point at the non-goal, don't just report a stray token").

2026-09-25 - LIMIT/OFFSET runtime semantics: negative LIMIT means
no limit, negative OFFSET clamps to zero, OFFSET past the end is
zero rows not an error

Issue #77. Confirmed live against sqlite3 3.51.0 (`create table
t(a integer, b text); insert into t values (1,'a'),(2,'b'),(3,'c'),
(4,'d'),(5,'e');`, `select * from t order by a ...`):

    limit 0                    -> 0 rows
    limit -1                   -> all 5 rows (negative LIMIT = no limit)
    limit 2 offset -1          -> rows 1-2 (negative OFFSET = OFFSET 0)
    limit -5 offset -5         -> all 5 rows (both defaults at once)
    limit -1 offset 2          -> rows 3-5 (OFFSET still applies)
    limit 5 offset 100         -> 0 rows, not an error

`exec/operators.py`'s new `Limit` operator implements all five
directly: `offset` is clamped to `max(0, offset)` once, at
construction, rather than left as an incidental consequence of
how `rows()` iterates (`range()` over a negative count is
silently empty in CPython, which would have made the clamp
appear to work without actually happening - caught by a
deliberate mutation check during this issue's own testing, see
`tests/test_operators.py`'s `test_negative_offset_is_clamped_on_
the_operator_itself`); a negative `limit` skips the truncation
branch entirely and yields every remaining child row after
`OFFSET`; `LIMIT 0` returns before pulling even one row from
`child`.

2026-09-25 - Limit's tree position (above Project) and its own
laziness are two independent choices, both settled without a
harness or scan change

Issue #77. `plan()` inserts `Limit` as the new outermost operator,
wrapping `Project` unconditionally, whenever `stmt.limit is not
None` - `Scan -> Filter (WHERE) -> [Aggregate -> Filter (HAVING)]
-> Sort -> Project -> Limit`. `LIMIT` cannot change *which*
columns a row has or what its values are (`Project` is a 1-in-1-
out, order- and count-preserving generator), so its position
relative to `Project` is undetermined by row-correctness alone;
what does force the choice is `SELECT DISTINCT` ("12c", #61's own
unfiled follow-on), which SQLite applies before `LIMIT` (`SELECT
DISTINCT x ... LIMIT n` limits the deduped set) - so `Distinct`
must land strictly between `Project` and `Limit` once it exists,
and placing `Limit` outermost now is the one choice that leaves
that slot free without a second tree-shape change later.

`Limit.rows()` is a plain generator, never `list(child.rows())
[offset:offset+limit]` - it pulls at most `offset + limit` rows
from `child` (fewer if `child` itself runs out first), stopping
the instant the requested count is yielded. This is the ordinary
Volcano-model property every operator in this codebase already
has except `Sort`/`Aggregate` (which must consume their child
fully before producing anything) - `Limit` merely has to not
throw it away by materializing up front. It is *not* the `LIMIT`
pushdown `_docs/spec.md` §3/§6 describe (M4: a scan stopping
`git log`/`git blame` itself) - that is a planner/scan
capability negotiation this operator knows nothing about;
`exec/operators.py`'s `Scan` still calls `source.scan(pushed=())`
unconditionally, unchanged by this issue. Proved directly with a
spy `ScanSource` (`tests/test_operators.py`, mirroring `Sort`'s
own `test_sort_reads_child_rows_exactly_once` pattern): `Limit`
over `Scan` with 20 rows available, no `ORDER BY`/`GROUP BY`
between them, pulls at most `offset + limit` rows, and `LIMIT 0`
pulls none at all.

2026-09-25 - the LIMIT/OFFSET oracle: row count only without
ORDER BY, self-consistency instead of a second SQL front end,
boundary-tie freedom proved from a query rather than hard-coded

Issue #77. Without `ORDER BY`, "the first n rows" is engine-
defined on both sides at once - SQLite promises no row order at
all (§3), and historian's own order for a `LIMIT`-free query is
merely *deterministic*, not the same sequence SQLite happens to
produce - so neither a sorted-multiset nor an exact-order
comparison means anything for a truncated result. Row **count**
is still comparable (`min(n, matching rows)` after `OFFSET`, on
both sides) and is checked directly in
`tests/differential/test_blame.py`'s own test bodies, bypassing
`assert_rows_match` entirely rather than adding a fourth mode to
it - `conftest.py` needed no change, avoiding any conflict with
#80's concurrent work there.

Row *content* without `ORDER BY` is checked the non-oracle way
instead, mirroring #61's own determinism pattern: historian's
`LIMIT n [OFFSET m]` result must equal the plain Python slice
`[m:m+n]` of historian's own result for the same query with the
clause removed - both are already deterministic (`AGENTS.md`),
and this catches an off-by-one or a double-counted `OFFSET`
without needing a second engine to agree with at all.

With `ORDER BY`, `assert_rows_match(ordered=True,
key_positions=...)` is reused exactly as #61 left it - but only
once the cut is proved boundary-tie-free: the row immediately
before the cut and the row immediately after it (on the full,
unfiltered, sorted result) must not share an `ORDER BY` key
tuple, or SQLite and historian could each legitimately keep a
different member of a group `LIMIT`/`OFFSET` splits apart, which
the tie-tolerant comparison (built for a *complete* result) would
misread as a mismatch. The proof is computed from a query against
the unfiltered SQLite side inside the test body itself
(`_order_with_limit`, `tests/differential/test_blame.py`) - never
hard-coded - the same discipline `#61`/`#80` already established
for the "`ORDER BY` key not in the select list" case: a fixture
change that later introduces a boundary tie fails the proof
loudly, as a broken test, rather than silently passing on a false
historian bug. No new parameter on `assert_rows_match` and no
change to `conftest.py` was needed for this either.

2026-09-25 - SELECT DISTINCT's ORDER BY narrows to keys built from
the select list; Sort's placement does not move; the justification
is oracle reliability, not historian's own determinism

Issue #78. `Distinct` is the seventh and last of phase 1's operators,
inserted directly above `Project` whenever `SELECT DISTINCT` is
present - exactly the slot #77's own design reserved
(`Scan -> Filter(WHERE) -> Aggregate -> Filter(HAVING) -> Sort ->
Project -> Distinct -> Limit`). Two things needed deciding, both
re-verified live against sqlite3 3.51.0 rather than carried over from
#61's own grooming (per this project's three-strikes history of
grooming passes that trusted unverified sqlite3 recollections).

First: `Sort`'s own placement does not move, despite #61's grooming
having speculated it might need to. `Sort` still sorts the wide,
pre-`Project` row set, exactly where #61/#77 already put it; `Distinct`
simply appends after `Project`, the same way `Limit` already does.
Confirmed live this holds even for a non-contiguous case:

    sqlite> create table t3(p,m); insert into t3 values
       ...> ('b',2),('b',2),('a',1),('a',1),('a',3);
    sqlite> select distinct p, m from t3 order by m;
    a|1
    b|2
    a|3

The two `a` rows are not adjacent in the insert order and `p`'s own
groups are not contiguous, yet `m`-order interleaving comes out
correct. The reasoning: whenever every bare column an `ORDER BY` key
touches is itself a selected column (or built purely from selected
columns - the narrowing below is what guarantees this), two pre-
`Distinct` rows headed for the same output row necessarily carry the
same `ORDER BY` key value, so sorting the wide row set and only then
projecting and deduplicating in a streaming, order-preserving pass
gives the identical answer to sorting the narrow, deduplicated set
directly. `tests/test_planner.py`'s
`test_distinct_with_order_by_and_limit_through_the_real_pipeline_end_to_end`
runs this exact case through the real pipeline.

Second, and the reason the first part is safe to rely on: a new
binder narrowing, symmetric to the GROUP BY/HAVING one (2026-09-19/
2026-09-24 entries above) but matched against the *select list*
instead of `GROUP BY`'s keys. Once `SELECT DISTINCT` is present,
every bare column an `ORDER BY` key touches must match a select-list
item by shape (exactly, or be built purely from select-list items),
or it is a `BindError`. Implemented in `sql/binder.py`'s `bind()`,
reusing `_split_for_grouped_check` and `_expr_shape_equal` unchanged
- the identical walk the GROUP BY narrowing already uses, called with
the bound select-list expressions in place of `group_by`'s keys. An
ordinal `ORDER BY` key needs no extra check (it already resolves to
the referenced select-list item's own bound expression, which
trivially shape-matches itself), and neither does a select-list alias
reference (`_resolve_name`'s `alias_first=True` for `ORDER BY`
already splices in that item's own bound expression).

Unlike the 2026-09-19 GROUP BY entry's own justification ("SQLite's
own choice is an unspecified internal choice, so there is no rule to
copy"), this narrowing is **not** framed that way, on the orchestrator's
own correction during this issue's grooming: it is the oracle, not
historian's own determinism, that makes the excluded shape unsafe.
historian's own pipeline - a stable `Sort`, then a streaming
first-seen `Distinct` - is already fully deterministic for the
excluded shape too, with no narrowing at all: the same repository and
query always produce the same rows in the same order, exactly
`AGENTS.md`'s guarantee, whether or not the `ORDER BY` key is built
from selected columns. The problem is on the other side of the
comparison: sqlite3's own answer for this shape is not reproducible
from any documented or stable rule, so matching it would mean
reverse-engineering (and permanently pinning historian to) an
undocumented, version-fragile SQLite internal, and getting that
reverse-engineering wrong would be invisible until the oracle
disagreed on some future query nobody thought to check.

The discriminating evidence, confirmed live and worth recording in
full rather than summarized, since it is what makes this a genuine
narrowing decision and not a coincidence:

    sqlite> create table u2(p,n); insert into u2 values
       ...> ('x',2),('x',1),('y',1);
    sqlite> select distinct p from u2 order by n;
    y
    x

Two plausible deterministic rules both predict the *opposite* answer,
`x` then `y`, which is what makes this discriminating rather than an
arbitrary preference:

1. "Sort the wide rows by `n` first, then dedup keeping the first
   occurrence." Stable-sorting `('x',2),('x',1),('y',1)` by `n`
   ascending gives `('x',1),('y',1),('x',2)` - the two `n=1` rows keep
   their original relative order, `x` before `y`, and the `n=2` row
   moves last. First-occurrence dedup by `p` over that gives `x` then
   `y` - the wrong order.
2. "Dedup first by `p`, keeping each group's first-encountered `n`,
   then sort the representatives by `n`." `x`'s first-encountered row
   has `n=2`, `y`'s has `n=1`; sorting those two representatives by
   `n` gives `y` (`n=1`) then `x` (`n=2`) - which happens to match
   sqlite3's actual output this time, but only by coincidence: swap
   the insert order of `x`'s two rows and rule 2's answer changes,
   while sqlite3's actual behaviour is driven by its own B-tree
   internals, not by insertion order in any documented way.

Since neither rule is reliably right and sqlite3 gives no documented
contract for which one applies (or whether either applies at all once
its query planner picks a different index or join order), there is no
rule for historian to copy - which is precisely why this shape is
narrowed away with a `BindError` instead of guessed at.
`tests/test_binder.py`'s
`test_distinct_order_by_a_column_not_in_the_select_list_is_a_bind_error`
and `tests/differential/test_blame.py`'s
`test_distinct_order_by_column_not_in_select_list_raises_bind_error`
pin this.

`count(DISTINCT x)` and the same form for `sum`/`avg`/`min`/`max` -
confirmed live during this issue's own grooming that all five accept
it - is a different mechanism entirely (deduplicating one aggregate's
own input, inside `Aggregate`/`_Accumulator`, never touching
`Project`/`Distinct`/the plan tree above `Aggregate`) and is
deliberately out of this issue's scope, left for its own follow-up
issue per the grooming note on #78.
