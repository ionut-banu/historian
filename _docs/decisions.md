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

2026-09-25 - LIKE gains ESCAPE; implemented, not declined

Re-groomed and routed to implement (issue #51), reversing the
provisional "declined" framing an earlier pass gave it: `_docs/
spec.md` §1's v1 grammar lists `LIKE` unqualified in its "Expressions"
line, and the "Explicitly out of scope for v1" list (subqueries, CTEs,
window functions, `UNION`/`INTERSECT`/`EXCEPT`, outer/cross joins,
correlated anything, UDFs) never mentions `ESCAPE` - unlike `CASE`/
`JOIN`, which were deferred by their own explicit grooming decisions
even though §1's grammar line also lists `CASE`. The feature itself is
small and self-contained: one optional clause on an operator (`LIKE`)
that already exists end to end, confined to `sql/lexer.py` (one new
keyword), `sql/ast.py` (`Like.escape`), `sql/parser.py`'s two existing
`LIKE` branches, and `exec/expression.py`'s `_eval_like`/
`_like_pattern_to_regex` - no new operator, no new table, no pushdown
design, nothing `plan/`-level, and `sql/binder.py` ends up untouched
(`Like.escape` is never bound - see below).

`ESCAPE` became the lexer's 31st reserved keyword, matching `sqlite3`
exactly: `create table t(escape text);` already fails there ("near
"escape": syntax error"), i.e. `escape` was already reserved in the
oracle. historian had no `ESCAPE` token at all, so `escape` unquoted
was a legal bare identifier here before this issue - a real divergence
this reservation closes rather than creates.

All findings below were run live against `sqlite3` 3.51.0 during this
issue's own implementation, not assumed from the grooming pass that
preceded it:

    sqlite> select '10%' like '10!%' escape '!';        -> 1
    sqlite> select '10x' like '10!%' escape '!';         -> 0
    sqlite> select 'a!' like 'a!' escape '!';             -> 0
    sqlite> select 'aX' like 'a!' escape '!';             -> 0
    sqlite> select 'ab' like 'a!b' escape '!';            -> 1
    sqlite> select 'a!b' like 'a!!b' escape '!';          -> 1
    sqlite> select 'a%b' like 'axb' escape 'X';           -> 0
    sqlite> select 'aXb' like 'axb' escape 'X';           -> 1
    sqlite> select 'a%b' like 'aXb' escape 'x';           -> 0
    sqlite> select 'a%b' like 'ax%b' escape 'X';          -> 0
    sqlite> select '10%' like '10!%' escape (1=1);        -> 0
    sqlite> select '10%' like '101%' escape (1=1);        -> 0
    sqlite> select '10😀%' like '10😀é%' escape '😀';       -> 0
    sqlite> select '10%' like '10😀%' escape '😀';         -> 1
    sqlite> select length('é'), length('😀');              -> 1|1
    sqlite> select typeof('10%' LIKE '10!%' ESCAPE NULL); -> null
    sqlite> select null like 'x' escape 'ab';   -- raises, not NULL
    sqlite> select 'x' like null escape 'ab';   -- raises, not NULL
    sqlite> select '10%' like '10!%' escape '!!';   -- raises
    sqlite> select '10%' like '10!%' escape '';     -- raises
    -- both: "ESCAPE expression must be a single character"

Escape-character *recognition* inside the pattern is case-sensitive /
exact-codepoint, independent of `LIKE`'s own ASCII fold of the matched
text - the trap most likely to produce a silent oracle mismatch, since
`_eval_like` ASCII-folded `pattern_text` *before* handing it to
`_like_pattern_to_regex`, which would have scanned already-folded text
for the escape character and made recognition wrongly
case-insensitive. Fixed by scanning the pattern's raw, un-folded text
for escape occurrences and ASCII-folding only the characters that end
up literal, one at a time, inside `_like_pattern_to_regex` itself;
`left_text` is still folded up front as before, since that half of the
comparison is unaffected.

"Single character" is counted the same way SQLite's own `length()`
counts it: Unicode code points, not UTF-8 bytes (`😀` is 4 UTF-8 bytes
and one code point in both). Python's `len()` on a decoded `str`
already counts code points, so `len(escape_text) != 1` needed no
special-casing.

An escape character at the very end of the pattern, with nothing
following it to escape, makes the pattern unsatisfiable - not a
no-op, not an error. `_like_pattern_to_regex` compiles this case to
`(?!)`, the standard "never matches" regex idiom, rather than treating
the escape as a literal character or raising.

The single-character length check is a **runtime** (`EvalError`)
failure, never a parse- or bind-time one, and it fires whenever the
escape operand's coerced text is not `NULL` and not exactly one code
point - independent of whether `left`/`pattern` are themselves `NULL`.
Confirmed live: `select null like 'x' escape 'ab';` and `select 'x'
like null escape 'ab';` both raise, they do not quietly return `NULL`
- so the escape operand is evaluated, and its length checked,
unconditionally, before the combined `NULL`-propagation check that
covers `left`/`pattern`/`escape` all being possibly-`NULL`. A `NULL`
escape operand itself is the one case that *is* NULL-propagated with
no error, even though its (missing) text could never pass the length
check - confirmed: `select typeof('10%' LIKE '10!%' ESCAPE NULL);` ->
`null`.

**`Like.escape` is bound in `sql/binder.py`, correcting a wrong
grooming claim.** The original grooming for this issue asserted
`sql/binder.py` needed no change because `Like.escape` "is bound the
same generic way every other `Expr` field already is." That is false:
`_bind_expr` is an explicit `isinstance` chain, not a generic
dataclass-field walk, and its `Like` branch (`dataclasses.replace(expr,
left=..., pattern=...)`) never mentioned `escape` at all - a
column-reference escape operand stayed a raw, unbound `ColumnRef` all
the way to `exec/expression.py`'s `evaluate()`, which has no case for
it and hit its defensive "unhandled expression node type"
`AssertionError`. Confirmed live on the implementation branch, before
this correction:

    SELECT count(*) FROM blame WHERE 'a' LIKE 'a' ESCAPE author_name
        sqlite3:   Error: ESCAPE expression must be a single character
        historian: AssertionError: exec/expression.py: unhandled
                    expression node type ColumnRef

New, legal syntax (`ESCAPE <column-reference>` is real `sqlite3`
syntax, and nothing in this issue's grammar work restricts the escape
operand to a literal) that parsed and bound cleanly and then crashed
with an internal assertion rather than a structured error - `cli.py`'s
backstop reports that as "a bug in historian," not a query result, and
exactly the class of bug the differential/`BindError`/`EvalError`
taxonomy exists to prevent. `sql/binder.py`'s `Like` branch now binds
`escape` exactly like `left`/`pattern` (`_bind_expr(expr.escape, ctx)
if expr.escape is not None else None` - `None` passes through
unchanged when no `ESCAPE` clause is present). Two other `Like`-
specific branches in the same module - `_contains_aggregate` (used to
classify a query as aggregate-or-not) and `_split_for_grouped_check`
(used for `GROUP BY`/`HAVING` validation) - had the identical gap for
the same reason and are fixed the same way, for the same reason: an
aggregate call or an ungrouped bare column hidden inside an `ESCAPE`
expression would otherwise silently evade both checks. All three
changes are local to `Like`'s own branch in each function - no other
node type's handling changed, and #84 (running concurrently) does not
touch `sql/binder.py` at all, so there is nothing to conflict with.

With `escape` now bound, a column-reference escape operand follows
the same three rules already implemented and tested for a literal one,
since `_eval_like` already evaluated `expr.escape` generically - the
only thing that was broken was reaching this code with a *bound* tree
at all. Confirmed live for all three: `create table t(x text, esc
text);` with `insert into t values ('a!b','!'),(...,'!!'),(...,NULL)`:
one character (`esc='!'`) is used as the escape (`select x like 'a!!b'
escape esc from t;` -> `1`); a longer value (`esc='!!'`) raises the
same `EvalError` a bad literal would (`select x like 'a!!b' escape esc
from t;` raises "ESCAPE expression must be a single character"); a
`NULL` value (`esc=NULL`) makes the result `NULL`, no error
(`select typeof(x like 'a!!b' escape esc) from t;` -> `null`). An
unknown column in `ESCAPE` (`ESCAPE nosuchcol`) now raises the
binder's ordinary `BindError: no such column: nosuchcol` - confirmed
it did not raise anything at bind time before this fix (the unbound
`ColumnRef` was accepted silently, regardless of whether the name
existed in the schema, and would only ever have surfaced as the
generic `AssertionError` above once evaluated).

`tests/test_binder.py`'s "LIKE ... ESCAPE: escape is bound like
left/pattern" section pins all of this directly - confirmed via `git
stash` that those tests fail with exactly the shapes above before this
fix (an unbound `ColumnRef` where a `BoundColumnRef` was asserted, and
"did not raise BindError" for the unknown-column case) and pass after.
`tests/differential/test_blame.py`'s
`test_like_escape_column_operand_reruns_the_coordinators_repro`
re-runs the coordinator's own repro through the real end-to-end
pipeline and confirms the symptom changed from the unstructured
`AssertionError` to the correct, sqlite3-matching `EvalError`.
`tests/test_expression.py`'s "LIKE ... ESCAPE with a column-reference
operand" section covers the three per-row rules above at the
`evaluate()` level, using `s` (text, one character) and `r` (real,
three-character text after coercion) from that file's own shared
`_SCHEMA`/`_ROW` fixture for the one-character and wrong-length cases
(`blame` itself has no single-character column), plus a small local
synthetic schema for the `NULL`-row-value case.

**Short-circuit `AND`/`OR`, left to right - a correction discovered
during this issue's own re-grooming.** The original grooming pass
claimed `LIKE ... ESCAPE 'ab' AND 1=0` still errors "since the LIKE is
evaluated first," which is wrong:

    sqlite> create table t(p); insert into t values('a'),('b');
    sqlite> select count(*) from t where p = 'zzz' and p like 'a' escape 'ab';
    0
    sqlite> select count(*) from t where p like 'a' escape 'ab' and p = 'zzz';
    Error: ESCAPE expression must be a single character
    sqlite> select count(*) from t where 1=1 or p like 'a' escape 'ab';
    2
    sqlite> select count(*) from t where p like 'a' escape 'ab' or 1=1;
    Error
    sqlite> select count(*) from t where 0=1 or p like 'a' escape 'ab';
    Error

SQLite evaluates `AND`/`OR` left to right and stops once the result is
decided. historian's `evaluate()` (`exec/expression.py`) evaluated
both operands of `And`/`Or` unconditionally before this issue - always
harmless until `ESCAPE` became the first expression able to raise at
runtime. Fixed by short-circuiting in `evaluate()` itself: `And`
returns `FALSE` without evaluating its right operand once the left
coerces to `FALSE`; `Or` returns `TRUE` without evaluating its right
operand once the left coerces to `TRUE`. This is exact under
three-valued logic - `and3(FALSE, x)` is `FALSE` and `or3(TRUE, x)` is
`TRUE` for every `x`, `NULL` included - so it changes nothing about
any *result*, only whether the right operand's `evaluate()` call
happens at all. A `NULL` left operand is not "decided" either way and
still evaluates the right, matching the table above (`0=1 OR ...`
still raises, since `0=1` is `FALSE`, not `TRUE`, so `OR` does not
short-circuit it).

**What is not replicated: SQLite's constant-folding.** `select
count(*) from t where p like 'a' escape 'ab' and 1=0;` returns `0`
rows with **no error**, in either operand order, because SQLite's
prepare-time optimizer removes the constant-false `1=0` conjunct
before the query ever runs - confirmed live, both orders return `0`
silently. This is not the same mechanism as the short-circuit above
(which is a runtime evaluation-order property, present in every SQL
engine's three-valued `AND`/`OR`); it is a query-plan rewrite specific
to SQLite's optimizer, and nothing in `_docs/spec.md` commits
historian to reproducing any particular optimizer's rewrites. historian
does not fold constants, so `p like 'a' escape 'ab' and 1=0` still
raises here where `sqlite3` returns `0` rows - a known, accepted
difference. The differential and unit tests for the `EvalError` case
deliberately avoid a constant-false conjunct for this reason, using
only the unconditional shapes confirmed above (bare `LIKE`, `LIKE` on
the left of `AND`/`OR`, and the non-constant short-circuit shape where
a real column decides the left operand).

2026-09-25 - correcting #60's sum overflow rule: it is not
order-dependent, it is "any non-integer value, anywhere"

Issue #88 (orchestrator comment, widening its scope over a bug #60
shipped). #60's own grooming described `sum`'s int64-overflow/REAL-
promotion interaction as order-dependent: "once a REAL has been seen,
overflow isn't checked" - read from testing only `(int64max, 1)` and
`(1.5, int64max, 1)`. Issue #88's re-grooming inherited the same
framing, and the orchestrator repeated it again in PR #73's and #89's
descriptions. None of the four checked the third order until this
issue: `(int64max, 1, 1.5)` - the overflowing addition *before* the
REAL, rather than after.

Verified against `sqlite3 3.51.0` directly, all eight rows below:

```
int64max, 1               -> Error: integer overflow
1.5, int64max, 1          -> 9.22337203685478e+18   (real)
int64max, 1, 1.5          -> 9.22337203685478e+18   (real)
int64max, 1, 'abc'        -> 9.22337203685478e+18   (real)
int64max, 1, '3'          -> Error: integer overflow
int64max, 1, -1           -> Error: integer overflow
int64max, 1, -5           -> Error: integer overflow
int64min, -1, 0.0         -> -9.22337203685478e+18  (real)
```

The rule is not about *order* at all. `sum` raises `integer overflow`
if and only if (a) the exact integer running total left int64 range at
some point, *and* (b) every non-`NULL` input was integer-classified
(a plain `INTEGER`, or `TEXT` whose entire trimmed string is
integer-shaped, per issue #88's own whole-string affinity
classification). A `REAL`, or `TEXT` that is not a clean whole-string
integer, appearing *anywhere* in the input - before the overflowing
addition or after it - permanently suppresses the check, and the
result is the plain float sum instead. This is symmetric in position:
`(1.5, int64max, 1)` and `(int64max, 1, 1.5)` both return the same
REAL value, not one raising and the other not.

The "permanent" half of the old description does still hold, just not
for the reason given: once the exact integer total has left int64
range with no non-integer value seen (`int64max, 1, -1` above), it
stays an error even though `-1` brings the *exact* total back to
`int64max`, in range. Overflow, once triggered, is never re-checked
against a later total and never cleared by one - it is cleared only in
the sense that it stops being checked at all, once a non-integer value
arrives.

`historian` on `main` before this issue got `(int64max, 1, 1.5)`
wrong: `_sum_add` (`exec/operators.py`) raised `EvalError` the instant
the `int64max + 1` step left int64 range, before it could ever see the
`1.5` that comes next - the running total was a bare `Value` combining
the "current sum" and "has anything overflowed yet" into one field
with no way to defer the decision. Fixed by separating the exact
integer accumulator (`_Accumulator._sum_int`), the parallel float
accumulator (`_sum_float`), an `_sum_overflowed` latch, and an
`_sum_saw_non_integer` latch into independent fields, and moving the
raise from `_sum_add` (now a pure step function that never raises)
into `_Accumulator.finish()`, evaluated once, after every row has been
seen: raise iff `_sum_overflowed and not _sum_saw_non_integer`.

Full-precision float check: SQLite 3.51.0's `sum()` may use
compensated (Kahan-Babuska-Neumaira) summation internally for
accuracy, so its REAL results were compared byte-for-byte against
Python's, not merely by type. For every row in the table above, naive
left-to-right `float` accumulation in the same order `sum` steps its
rows (`total += float(value)` per value, no compensation) produced a
double bit-identical to `sqlite3`'s own `printf('%.20g', ...)` output
- confirmed via `decimal.Decimal` on both sides, not string
comparison. No deviation was found for any input this issue's table
covers; historian's plain running float total is sufficient and no
compensated-summation algorithm was needed.

2026-09-25 - #88's "no compensated-summation algorithm was needed"
was wrong for other inputs; `sum`/`avg` now port SQLite's own
Kahan-Babuska-Neumaier accumulator, and `avg` shares `sum`'s
accumulator instead of casting to float up front

Issue #91. #88's own grooming (the entry directly above) checked
naive `float` accumulation against `sqlite3` only for its own
overflow-ordering table's eight rows and found no deviation there -
correctly, for those particular inputs - and concluded no compensated
summation was needed at all. That conclusion did not generalize:
issue #91 found inputs (`0.1` summed 10 times; `1e16, 1.0, -1e16`; a
large-magnitude mixed-exponent sum) where `sqlite3 3.51.0`'s `sum()`
disagrees with naive left-to-right `float` accumulation bit-for-bit,
because SQLite's own `sumStep`/`sumFinalize` (`src/func.c`) use
Kahan-Babuska-Neumaier (KBN) compensated summation internally. Neither
`math.fsum` nor Python's own compensated built-in `sum()` (3.12+) is a
safe substitute - one row in the issue's verification table (case I,
`1.2e6, -5.51e-6, 5.34e55, 4.22e21, -7.65e23, 8.9e57, -9.13e57, 5.55e0`)
has naive, KBN, and `fsum` land on three distinct bit patterns
simultaneously, and `fsum` is the *correctly-rounded* sum, which is
provably not what `sqlite3`'s `sum()` computes. There is no
approximation of SQLite's own algorithm that reproduces it exactly
except the algorithm itself, so `exec/operators.py` now ports it
step-for-step: `_kbn_step` (`kahanBabuskaNeumaierStep`), `_kbn_step_
int64`/`_kbn_split_int64` (`kahanBabuskaNeumaierStepInt64`, splitting
`|v| >= 2**52` into a multiple of 16384 plus a remainder so both
halves convert to `double` exactly - computed with exact Python `int`
arithmetic and an explicit sign fixup for C's truncating `%`, never
`math.fmod`, which would convert the original value to `float` first
and lose the precision the split exists to preserve), `_kbn_init`
(`kahanBabuskaNeumaierInit`), and `_kbn_is_overflow`
(`sqlite3IsOverflow`). One porting hazard worth recording: C's `pSum-
>rErr += (s - t) + r` computes `(s - t) + r` as one unit *before*
adding it to the old `rErr` - a left-to-right Python transliteration
(`r_err + (s - t) + r`, evaluated as `(r_err + (s - t)) + r`) rounds
differently once `rErr` and `s`/`r` are at very different magnitudes,
and silently fails the very first row of this issue's own verification
table. Caught by writing the table's tests before the port, per
`_docs/team/software-engineer.md`.

A second, unrelated bug surfaced during the same grooming and is fixed
by the same refactor: `avg` is not a separately-implemented "cast
every value to `float` and average" aggregate in SQLite - it shares
`sum`'s own `xStep` (`sumStep`) outright, differing only in
`xFinal`/`xValue`, so it keeps `sum`'s exact `iSum` running total for
as long as possible too, converting to `double` only once, at
finalize time (or at the same fold transition `sum` uses). historian's
`avg` on `main` before this issue cast every value to `float`
individually and summed those (`self._avg_total += float(arithmetic_
operand(value))`, unconditionally, every row), which is wrong for
integers past 2**53 because `float` is not distributive over integer
addition: `avg(9007199254740993, 1)` returned `4503599627370496.0`
on `main`, where `sqlite3` returns `4503599627370497.0`. Fixed by
giving `sum` and `avg` the same shared step algorithm in `_Accumulator
.step` (each `AggregateCall` still gets its own `_Accumulator`
instance and its own independent state - only the algorithm is
shared, not the state across calls), differing only in `finish()`:
`sum` raises on `self._sum_overflowed and not self._sum_saw_non_
integer` and returns the exact `self._sum_int` when the exact path was
never abandoned; `avg` never raises and always divides by `self.
_non_null_count`.

#88's overflow rule itself (raise iff `self._sum_overflowed and not
self._sum_saw_non_integer`, both latched permanently, neither ever
un-latched) needed no change. SQLite's own `sumStep` actually
live-clears `p->ovrfl = 0` on every non-integer value stepped while
`p->approx` is already set, which reads as a different, more dynamic
rule - but issue #91's grooming proved the two are exactly equivalent
for every possible input sequence: an overflow can only occur while
`p->approx`/`self._sum_approx` is still unset, which is only true
before the first non-integer value anywhere in the input, so whenever
any non-integer value appears at all, any overflow must have happened
strictly before it and SQLite's own live-clearing step always
suppresses it - the same outcome #88's simpler latch already produces
without ever tracking *when* the non-integer value arrived relative to
the overflow. Re-verified computationally and pinned with a regression
test (case G in the verification table: `int64max, 1, 1.5` must not
raise) after the refactor, since the refactor touches the same code
paths #88 built.

Every "Required" row of issue #91's verification table is asserted to
actually fail under plain naive `float` accumulation, not merely to
pass under the new code - proof each row exercises the fix rather than
passing by coincidence. Two rows absent from the ported source
material's own worked examples (F1-F3, G) were added during
implementation (F4, F5) after those examples turned out not to
distinguish a correctly-split `int64` conversion from an unsplit
direct `float()` cast for the specific magnitudes involved (`int64max`/
`int64min` happen to round-trip identically either way for those
particular follow-on addends) - found by a randomized search over
large integers and confirmed against `sqlite3` directly, so the
`StepInt64`/`Init` split's own correctness has a test that actually
depends on it.

2026-09-25 - The CLI-versus-module float disagreement reported
during #91's and #93's grooming came from binding values versus
writing them as SQL literals - a real difference, on this
platform, not a printf display artifact

Both #91's and #93's grooming reported that the system `sqlite3`
CLI (3.51.0, Apple-patched) and Python's bundled `sqlite3` module
(3.50.4) computed a genuinely different double for an adversarial
float sum. This entry's own first version, written earlier during
#93, misdiagnosed the cause as `printf` zero-padding and claimed
the two computed the same values - wrong, caught before the branch
reached main, and rewritten here rather than left standing with a
second entry stacked on top; `_docs/process.md`'s "if a decision
contradicts the spec, the spec is edited in the same commit" is
the closest precedent for correcting a decision found wrong before
it lands, and this file's append-only rule protects entries once
they are part of the shared record, not a mistake caught inside
the same still-open branch that wrote it.

**Reproduced inside the module alone, no CLI involved** - so the
Apple build is not the variable. Same three values throughout
(`-6.6116480458179635e-18`, `-1.8193757715275717e+299`,
`3729136089270252.0`): bound as Python floats via `execute('...
VALUES (?)', (v,))`, they sum to `-0x1.16317cc804165p+994`; written
as the identical text as SQL literals (`INSERT INTO t VALUES
(-1.8193757715275717e+299)`, or via `executescript`), they sum to
`-0x1.16317cc804164p+994` - one ULP lower. Reproducible with
`tests/oracle.py` (#93):

    uv run python tests/oracle.py "" "SELECT ? + ? + ?" -6.6116480458179635e-18 -1.8193757715275717e+299 3729136089270252.0
    uv run python tests/oracle.py "" "SELECT -6.6116480458179635e-18 + -1.8193757715275717e+299 + 3729136089270252.0"

**Cause: SQLite's own decimal-literal parser is not correctly
rounded on this platform, for some values.** Measured directly,
comparing `select <lit>` against Python's own `float(<lit>)` for
the same literal text:

    <=6 significant digits, exponent -10..10        0 / 10000 differ
    <=15 significant digits, exponent -20..20        0 / 10000
    17 significant digits, exponent -20..20          0 / 10000
    17 significant digits, exponent 200..300      1622 / 10000  differ by 1 ULP
    plain decimals (e.g. 3.14159)                     0 / 10000

Independently spot-checked at smaller scale during this rewrite
(2000 trials per row, a fresh seed): 0/2000 for the first three
rows, 299/2000 (about 15%) for the large-exponent 17-digit row -
same shape, same order of magnitude, confirming the effect rather
than merely repeating the earlier number. Only 17-significant-digit
literals at large exponents are affected, and at that shape the
mismatch rate is in the tens of percent, not rare. Likely
mechanism: SQLite's text-to-double conversion goes through `long
double`, and on arm64 macOS `long double` is the same width as
`double`, so the extra rounding headroom other platforms get for
free is unavailable here - making this platform-dependent SQLite
behaviour, not something specific to Apple's CLI patch. The CLI and
the module parse literals the same way; #91's and #93's grooming
disagreed only because their two checks fed the value down
different paths - the CLI checks wrote it as a literal, the module
checks bound it.

**The earlier "300 trials, 0 mismatches" exact-comparison check
(this entry's own first pass) was invalid, not merely
insufficient.** It compared `sum(x) = <module repr>` entirely
inside SQL, so the module's `repr()` text was itself re-parsed by
SQLite as a literal on the right-hand side of `=` - both sides of
every comparison went through SQLite's own literal parser, so a
parsing error present on both sides canceled itself out and was
invisible to that check by construction.

`printf`'s zero-padding past about 16 significant digits is real
and still worth knowing - `select printf('%.20e',
-1.8193757715275717e+299)` still prints
`-1.81937577152757100000e+299` - but it is a separate display
hazard, not the cause of the reported disagreement: even
`printf('%!.20e', ...)`, which does show real digits throughout,
still disagrees between the literal-parsed value and the bound
value, because they are genuinely different doubles.

**The rule going forward:** be deliberate about how a value reaches
SQLite. A value written as a SQL literal goes through SQLite's own
parser; a value bound as a parameter is Python's exact double,
untouched. A comparison against the oracle must feed both sides the
same way. The differential harness (`tests/differential/
conftest.py`) loads blame rows by binding, so it is unaffected.
`tests/oracle.py` (#93) now accepts bind arguments after its setup
and query strings for exactly this reason - see its own docstring.
`_docs/process.md`'s "The oracle" section states the rule.

Also noted for #6 (scientific notation in historian's own lexer,
not yet built): historian will parse `REAL` literals with Python's
correctly rounded `float()`, while SQLite on this platform does
not always - so historian and the oracle can disagree on a
literal's own value before any arithmetic runs, for this same
17-digit/large-exponent shape. Filed on #6, not fixed here.
2026-09-25 - §1's six reachable non-goals are rejected by name
(`UnsupportedGrammarError`, an `isinstance` of `ParseError`), not
folded into the generic `ParseError` every other unbuilt construct
raises

Issue #24. `sqlite3` itself runs several of these constructs -
subqueries, CTEs, window functions, and `UNION`/`INTERSECT`/`EXCEPT`
all execute under real `sqlite3` - so historian's rejection of them is
a deliberate §1 narrowing, not a mismatch for the differential oracle
to catch; no new differential cases were added for this issue, and
none should be. `_docs/spec.md` §3's "Unsupported grammar" rule and
§5's own literal example (`error: window functions are not
supported ...`) already said the shape; this issue is what makes six
of §1's non-goals actually take it, in place of the "expected end of
query, found ..." (or similarly generic) message the parser raised
for all of them before.

Detection is by token *text* at a specific grammar position, never by
token type: none of `WITH`, `UNION`, `INTERSECT`, `EXCEPT`, `OVER`,
`LEFT`, `RIGHT`, `FULL`, `OUTER`, `CROSS`, or `NATURAL` are lexer
keywords (confirmed by reading `KEYWORD_TYPES` in `sql/lexer.py`
directly) - they all lex as plain `IDENTIFIER`, so promoting any of
them to a keyword to detect them would make each illegal as an
ordinary identifier everywhere in the grammar (`SELECT over FROM
blame`, `SELECT path AS union FROM blame`, and the rest), which is
precisely the regression this issue exists to avoid. `sql/lexer.py`
is untouched.

The trap this issue exists to avoid making twice: `INNER JOIN` (and
plain `JOIN`) is v1 grammar phase 3 (§6) simply hasn't built yet, not
a §1 non-goal - §1 says "outer and cross joins," not joins in
general - so it keeps the ordinary generic `ParseError` unchanged,
and `tests/test_cli.py::test_unimplemented_grammar_exits_1` (a plain
`JOIN`) passes unmodified. `NATURAL JOIN` is excluded for the same
literal-wording reason and is regression-tested explicitly, so a
later "completion" of join detection does not silently sweep it in.

`UnsupportedGrammarError` subclasses `ParseError` rather than being a
new sibling exception (correcting issue #8's original grooming
proposal, `UnsupportedError`): `cli.py`'s `except (LexError,
ParseError, BindError, EvalError)` clause, a closed set fixed by
issue #49, catches it via `isinstance` with zero changes to `cli.py` -
exit code `1`, the same as any other bad query, not exit `4`'s "a bug
in historian" backstop, which a new top-level exception would have
fallen into.

Outer/cross-join detection is keyed on the *token sequence*
(`LEFT`/`RIGHT`/`FULL` followed by `JOIN` or by `OUTER JOIN`, `CROSS`
followed by `JOIN`), not the bare word immediately after `FROM
<table>` - an orchestrator amendment ahead of dispatch. historian has
no table-alias grammar yet, so any identifier in that position is
already an error today, and keying on the bare word alone would have
worked for now; but once table aliases exist, `FROM blame left` (an
alias named `left`) would silently become a false "outer and cross
joins are not supported" error, with nothing to signal that it had
regressed. `tests/test_parser.py::test_bare_left_without_join_keeps_
ordinary_parse_error` pins the distinction now, before aliases exist,
specifically so that a future alias implementation trips it.

Follow-up, same day: the shared message template
(`"{feature} are not supported"`) assumes a grammatically plural
*feature* - true of "CTEs", "subqueries", "window functions", and
"outer and cross joins", but not of a bare `UNION`/`INTERSECT`/
`EXCEPT`, which produced "error: UNION are not supported" - a
singular keyword with a plural verb. Found by the orchestrator running
real CLI queries after the initial implementation, not by any test,
since every test up to that point only checked for the keyword's
presence as a substring rather than the message's exact text. Fixed by
wrapping the three set-operator keywords as "compound queries
(UNION)"/"compound queries (INTERSECT)"/"compound queries (EXCEPT)" at
the one call site in `expect_end()`, rather than threading a verb
parameter through `_unsupported_grammar_message` and every one of the
six call sites: "compound queries" is SQLite's own term for a
`UNION`/`INTERSECT`/`EXCEPT` statement, reads correctly with "are",
and still names the specific keyword seen, so the fixed template needs
no change and the other five call sites are untouched. The three
`test_..._is_unsupported_grammar_and_names_...` tests in `tests/
test_parser.py` were widened from a substring check to the full exact
message text, so a regression back to the bare-keyword phrasing is
actually caught rather than merely still containing the keyword.

2026-09-26 - one `historian/catalog.py`, not two hand-kept table
dicts or a self-registering registry; #52 folded into #35 as the same
fix

Issues #35 and #52, closed by the same PR. Before this issue,
`sql/binder.py` and `plan/planner.py` each hardcoded their own real
table catalog - `TABLES = {"blame": BLAME_SCHEMA}` and `TABLES =
{"blame": BlameScan}` - built by importing `historian.tables.blame`
directly, and defaulted `bind()`'s `catalog` and `plan()`'s `tables`
parameters to them. That had two separate costs: merely *importing*
the binder or the planner - never mind running a query - put
`subprocess` into `sys.modules` (`tables/blame.py` imports it at
module level to shell out to `git`), violating AGENTS.md's "the
parser, the planner and the executor are plain Python with no git and
no subprocess imports"; and the two catalogs were independent dicts
with nothing to stop them naming a different set of tables, a
divergence that would have surfaced as a bare `KeyError` reaching a
user rather than a clean error (#52, interacting with #49).

The chosen design: `historian/catalog.py` is the one new module that
imports each table module directly and builds one literal `TABLES:
dict[str, TableDef]`, pairing a `Schema` and a `ScanFactory` per
table. `SCHEMAS` and `SCAN_FACTORIES` - what the binder and the
planner actually consume - are dict comprehensions *over*
`TABLES.items()`, not copies someone keeps in sync by hand, so they
cannot list different table names from each other by construction,
not merely by a test that happens to check both today. `bind()`'s
`catalog` and `plan()`'s `tables` parameters lost their hardcoded
defaults and became required - an empty-dict or lazily-imported
default would only relocate the same import-timing accident into a
different function, not remove it. `cli.py` became the composition
root: the only production module that imports `historian.catalog`,
passing `SCHEMAS`/`SCAN_FACTORIES` into `bind()`/`plan()` explicitly.
`sql/binder.py` and `plan/planner.py` no longer import
`historian.tables.*` or `subprocess`, directly or indirectly, and
neither does `exec/expression.py` or `exec/operators.py` (both
inherited the violation solely by importing names out of
`sql/binder.py`, so fixing the binder's chain fixed both for free -
confirmed by a fresh-interpreter test naming each module separately,
`tests/test_layering.py`).

Two other designs were considered and rejected:

- **Lazy `import subprocess` inside `tables/blame.py`.** Makes a
  fresh-interpreter `subprocess`-absence test pass, but leaves the
  actual layering violation untouched: the binder and the planner
  would still import `tables/blame.py` directly and hardcode
  `{"blame": ...}` each, so #52's divergence risk stays completely
  unaddressed, and a phase-2 table still means hand-editing two files.
  It makes the architectural claim true only by an accident of when
  one particular `import subprocess` line happens to run - exactly
  the phrase #35 was filed to fix, not paper over.
- **Scans self-registering into a catalog by import side effect**
  (each table module calls something like `catalog.register("blame",
  ...)` at its own import time, with some import-everything step
  making that run). Rejected for two reasons. First, something still
  has to import every table module for its registration to run - the
  same "what imports the tables" question the explicit design answers
  directly, except a registration design answers it implicitly, by
  relying on Python's import-executes-top-level-code behaviour as the
  wiring mechanism itself. That is exactly what AGENTS.md's "no
  metaclasses, no dynamic dispatch tricks, no clever descriptors...
  portable to Rust later" rule warns against: a Rust port has no
  equivalent for "importing a module has the side effect of
  registering it into a global table" without reaching for something
  like the `inventory` or `ctor` crates, themselves considered a smell
  in idiomatic Rust for this exact reason. Second, it reintroduces the
  import-order hazard the explicit design avoids for free - a registry
  populated by side effects can be read before every table has
  registered into it, with nothing preventing that ordering by
  construction; it would have to be enforced by convention (import all
  tables first) or by a test, whereas `historian/catalog.py`'s literal
  dict, built top to bottom in one expression, cannot be read
  half-built. The explicit dict is the more honest reading of this
  option's actual intent (the planner depends on an abstraction,
  `historian.catalog`, rather than reaching into `tables/blame.py`
  itself) without the side-effect machinery, and it translates
  directly to a Rust `HashMap` or `match` built once in an equivalent
  `catalog.rs`, which a registration pattern would not.

`historian/catalog.py` itself still imports `tables/blame.py`, and
therefore still imports `subprocess` transitively - unavoidable, and
not the bug this issue fixes. Something concrete has to name every
table's schema and scan class; the fix is confining where that name
is allowed to appear (only `cli.py` and tests that want the real
catalog), not making the import vanish from the codebase.

Guard tests proved themselves against the bug, not just the fix:
`tests/test_layering.py`'s fresh-interpreter checks were run against
the pre-fix code first and confirmed to fail (naming the binder's and
the planner's chains independently), then a working fix was
temporarily broken twice more by hand - reintroducing each of the two
`tables/blame.py` imports one at a time, and hand-editing
`SCAN_FACTORIES`'s derivation to drop `"blame"` while leaving
`SCHEMAS` alone - confirming each mutation was caught by the guard
that specifically names it, before being reverted.
`tests/test_binder.py::test_binder_module_does_not_import_subprocess_directly`
was removed rather than kept alongside the new guards: it only ever
checked `vars(binder_module)` for a directly-written `import
subprocess` statement, which stayed `False` throughout this entire
bug (the violation was transitive, via `BLAME_SCHEMA`), so it gave
false confidence in both directions and had no reason to survive next
to a test that actually catches the failure mode.

2026-09-26 - `_ascii_fold`/`_is_ascii_digit` merged into a new
`historian/ascii.py`; the int64 bound moved into `historian/values.py`;
the two number scanners stay separate, pending #6

Issue #53. Three small helpers were each defined more than once:
`_ascii_fold` in `sql/binder.py` and `exec/expression.py`;
`_is_ascii_digit` in `sql/lexer.py` and `exec/expression.py`; and
SQLite's int64 bound in three places - `sql/parser.py`'s `_INT64_MAX`
(the positive bound only, for a decimal literal that overflows),
`exec/expression.py`'s `_INT64_MIN`/`_INT64_MAX` (both bounds, since
arithmetic can overflow toward either end), and `exec/operators.py`'s
`_SUM_INT64_MIN`/`_SUM_INT64_MAX` (the same two numbers again, under a
third pair of names, for `sum`'s own overflow check). Each of the two
text predicates had a documented reason for its second copy at the
time: `#35`'s original layering fix meant `sql/lexer.py` and
`exec/expression.py` could not import `sql/binder.py` without pulling
`historian.tables.blame` (and therefore `subprocess`) transitively.
That reason is gone now that `#35`/`#52` are merged into
`historian/catalog.py`, which is exactly why this issue stands on its
own: the trade-off that justified the duplication no longer exists,
so nothing is left to reconcile except giving each helper one home.

Both `_ascii_fold` copies were read side by side before merging them -
byte-for-byte identical
(`return "".join(chr(ord(ch) + 32) if "A" <= ch <= "Z" else ch for ch
in text)` in both) - so this was a pure dedup, not a behaviour
reconciliation. There was nothing to decide about *which* copy's
behaviour to keep.

New home: `historian/ascii.py`, a leaf module with no imports beyond
the stdlib, holding `is_ascii_digit` and `ascii_fold`. Neither
`values.py` ("comparison and three-valued logic" per its own
docstring) nor `schema.py` (row shape) is the right home for a
text-classification rule that has nothing to do with either concern -
stretching either module's documented scope to fit these two functions
would be a worse fit than a new, narrowly-scoped module. Because
`historian/ascii.py` imports nothing, every current and future
consumer - the lexer, the binder, the expression evaluator - can
import it with no cycle and no risk of dragging in git/subprocess,
which is also why it needed no `#35`-style layering fix of its own.

The int64 bound went to `historian/values.py` instead, alongside it
rather than into `historian/ascii.py`: `INT64_MIN`/`INT64_MAX` are a
property of `Value`'s own `int` variant (SQLite's INTEGER storage
class is int64), which `values.py` already documents, not a text-
classification rule. `sql/parser.py` now imports only `INT64_MAX` (it
never sees a negative literal - the lexer always emits a leading `-`
as its own `MINUS` token); `exec/expression.py` imports both, since
subtraction and negation can overflow toward either end; and
`exec/operators.py` imports both for `_sum_add`'s own overflow check.
`_sum_add` itself - the decision to raise on overflow rather than
promote to `float`, per #60/#91 - did not move and did not change;
only the two magic numbers underneath it moved.

`exec/expression.py`'s `_INT64_MIN_MAGNITUDE_AS_FLOAT = 9223372036854775808.0`
was deliberately left alone: it is `2**63` as a `float`, a distinct
constant used only to detect a negated literal spelling of
`INT64_MIN` (`-9223372036854775808` lexes as unary minus applied to
the literal `9223372036854775808`, one past `INT64_MAX`), not another
copy of either bound. The guard test below was written to name-check
this directly, not just trust that nobody would confuse the two.

The one real divergence in this area was never `_ascii_fold` - it is
the two *number scanners*: `sql/lexer.py`'s `_read_number` (tokenizing
a bare numeric literal) rejects an exponent, while
`exec/expression.py`'s `_scan_number` (text-to-number coercion for
arithmetic/affinity) accepts one, because SQLite's own text-to-number
conversion does. That gap is issue #6 (v2 backlog: lexer scientific
notation and hex literals) and stays out of scope here - merging the
two scanners would also have to teach the lexer an exponent grammar,
which is a grammar change, not a refactor. The two functions instead
each gained a short comment naming the other and issue #6, so whoever
picks up #6 finds both scanners from either one. The divergence is
already pinned by two existing tests, left unchanged:
`tests/test_expression.py::test_arithmetic_text_coercion_exponent_form_is_always_real`
and
`tests/differential/test_blame.py::test_scientific_notation_glue_still_hits_the_bare_alias_parse_error`.

Guard test: `tests/test_shared_primitives.py` greps `src/historian/`
directly (not via import) for `def ascii_fold(`, `def
is_ascii_digit(`, and a module-level assignment to a name ending
`INT64_MIN`/`INT64_MAX`, asserting each occurs exactly once and lands
in the right new home file. The assignment check needed to be more
precise than the issue's own suggested `INT64_M(IN|AX)\s*=`, which
would also match a *comparison* such as `INT64_MAX == x` sitting at
the start of a line - the regex actually used,
`^[A-Z_]*INT64_M(IN|AX)\s*(:[^=]*)?=(?!=)` with `re.MULTILINE`, adds
the trailing `(?!=)` to exclude `==` and anchors to a line-initial run
of uppercase letters/underscores so a *use* of the name
(`if value > _INT64_MAX:`) can never match (the line does not begin
with the name). The same anchoring is what keeps it off
`_INT64_MIN_MAGNITUDE_AS_FLOAT = 9223372036854775808.0`: after
matching `INT64_M` + `MIN`, that name continues with
`_MAGNITUDE_AS_FLOAT` before any `=`, and the only path the regex has
past the bound name is an optional group that must start with a
literal `:` (a type annotation), which cannot swallow
`_MAGNITUDE_AS_FLOAT` - so the match fails to reach an `=` at all.
Verified directly, both as source-scanning tests and as standalone
regex-example tests independent of the source tree.

2026-09-27 - "no affinity" is `Column.type is None`, not a fourth
`ColumnType` member; aggregate results and computed `GROUP BY` keys
declare it

Issue #99. `Aggregate`'s output schema declared `count` `INTEGER`,
`avg` `REAL`, and `sum`/`min`/`max` and every computed `GROUP BY` key
a `TEXT` placeholder. Harmless until `HAVING` (#69) and select-list
comparisons started reading those declared types through
`exec/expression.py`'s `_affinity_of`: `plan/planner.py`'s
`_split_expr` rewrites each aggregate call and computed key into a
bare `BoundColumnRef` into `Aggregate`'s output, indistinguishable by
shape from a real column, so the placeholder was applied as a real
affinity. `HAVING sum(line_no) > 3` compared `'21' > '3'` as text;
`count(*) = '12'` converted `'12'` to `12`. SQLite gives both an
aggregate result and a computed expression no affinity at all.

Options considered for spelling "no affinity":

- A fourth `ColumnType` member (`NONE`/`BLOB`). Rejected: every reader
  of `ColumnType` - including the differential harness's `CREATE
  TABLE`, which emits `column.type.value` verbatim - would take it for
  a fourth affinity a table may declare, which §2 forbids.
- A separate flag on `Column` (`has_affinity: bool`). Rejected: two
  fields that can disagree (`type=INTEGER, has_affinity=False`), with
  `type` then meaningless half the time.
- Teaching `_affinity_of` about provenance (a new bound-node type, or
  a flag on `BoundColumnRef`). Rejected: #99 scopes `_split_expr`'s
  rewrite out, and it would make `exec/expression.py` care which
  operator produced a row, which it deliberately does not.
- `Column.type: ColumnType | None`, `None` meaning no affinity.
  Chosen. `_affinity_of` already returned `ColumnType | None` with
  exactly that meaning for literals and computed expressions, so it
  needed no logic change at all - it reads the declared type off the
  schema as before, and a `None`-declared column now behaves in a
  comparison exactly like any other no-affinity operand. It is also
  the plainest possible Rust translation, `Option<ColumnType>`.

`ColumnType`'s three members keep exactly their old meaning; no
table's schema changes, and a table column is never declared `None`.
A bare-column `GROUP BY` key still copies its source column's type, so
`GROUP BY line_no HAVING line_no = '3'` still converts `'3'` to `3`.

Not changed here: `Project`'s computed-column `TEXT` placeholder in
`_project_column`. Nothing evaluates an expression against `Project`'s
output (no subqueries, and `Sort` sits below `Project`), so it is not
load-bearing today; if that changes it should become `None` by the
same rule.

2026-09-27 - A leading run of `(` is closed incrementally, not all at
once; correcting the 2026-09-01 entry's "behaviourally identical"
claim

Issue #100, filed `from: review` against the 2026-09-01 "Deeply
nested expressions raise ParseError, not RecursionError" entry, whose
last line claimed "the loop-based paren handling is behaviourally
identical to the recursive version it replaces." False:
`_parse_primary`'s LPAREN branch stripped a whole leading run of `(`
and then demanded that same count of `)` in a row immediately after
parsing exactly one inner expression - correct only when every paren
in the run closes at the very end of that one term (`(((1)))`), wrong
whenever an outer `(` in the run closes later, after more tokens
(`((1) + 1)`, whose outer `)` comes after ` + 1`, not immediately
after the inner `)`'s own close). `uv run historian` raised `error:
expected ')', found '+'` on that exact input before this fix.
Confirmed oracle-first throughout, against this checkout's `sqlite3
3.45.1` (tracked separately by #117; none of this issue's cases are
anywhere near a version boundary) via `uv run python tests/oracle.py`:
`SELECT ((1) + 1)` -> `2`, `SELECT (((1) + 1) + 1)` -> `3`, `SELECT
((1 + (2)) + 1)` -> `4`, `SELECT count(*) FROM blame WHERE ((path =
'zzz-no-such-file'))` -> `0`.

The fix, entirely in `sql/parser.py`

Still one `_parse_expr` call for the leading run's first inner term -
not one recursive call per paren, which the 2026-09-01 entry's own
constraints (and issue #100's) rule out, since that would lower the
pure-nesting shape's depth ceiling back toward Python's real
recursion limit and reopen #8. But after that one call returns, the
LPAREN branch now closes only as many `)` as are *immediately*
available (`_consume_available_rparens`), and if some are still
owed, resumes parsing rather than erroring: a new method,
`_continue_expr`, drives the precedence chain's own tightest-to-
loosest sequence explicitly - one call each to
`_parse_concat`/`_parse_multiplicative`/`_parse_additive`/
`_parse_relational`/`_parse_comparison`/`_parse_and`/`_parse_or`,
every one of which grew an optional `left` parameter so it can resume
from an already-reduced value instead of parsing a fresh one from the
next-tighter level. `_parse_primary` loops - `_continue_expr`, then
consume what closes, then check again - until every paren the run
opened is closed or a full iteration makes no progress at all (raised
as `ParseError`, not an infinite loop: `SELECT ((1) + 1 FROM blame`,
missing its final `)`).

Two things this fix deliberately does not change: parens still
produce no AST node of their own (`inner` is returned unchanged,
exactly as before), and the four depth-limit regression tests named
in #100's acceptance criteria pass unmodified. The first is what lets
`test_where_or_wrapped_in_extra_parens_matches_unwrapped` (`tests/
differential/test_blame.py`) assert the wrapped and unwrapped forms
of a `WHERE` predicate bind to the *identical* expression tree
(position aside) rather than merely producing the same rows - with no
distinct AST, pushdown (M4, not yet built - `exec/operators.py`'s
`Scan` still calls `source.scan(pushed=())` unconditionally) has
nothing to tell the two forms apart by either.

What was actually measured, not assumed

`_continue_expr` is a fixed, small sequence of direct calls - it never
recurses into itself - so calling it repeatedly from `_parse_primary`'s
own `while` loop costs no accumulating Python stack, the same property
the 2026-09-01 loop already had. This was verified, not just argued,
against two different mixed-nesting shapes, both added to `tests/
test_parser.py`:

- `_mixed_nested_parens` (issue #100's own named acceptance-criteria
  shape: many leading opens, one inner literal, one `+ 1`, then the
  matching closes all at the end - e.g. `(` * N + `1 + 1` + `)` * N):
  the `+ 1` sits *before* any `)`, so the whole run still closes
  together in one `_parse_expr` call and never touches
  `_continue_expr` at all. Measured directly: parses up to
  `_MAX_NESTING_DEPTH` (1000, same as pure nesting), raises
  `ParseError` beyond it, and raises `ParseError` (never
  `RecursionError`) at 20,000 - identical ceiling to the pure-nesting
  case, because it is mechanically the same loop-based path.
- `_staggered_batch_chain` (`levels` copies of `((1)+<prev>)`, each
  one nested in the *previous* level's own operand position rather
  than adjacent to its opening parens): this genuinely re-enters
  `_parse_primary` from inside `_continue_expr`'s own operand fetch,
  while the outer invocation is still on the Python call stack
  waiting inside its `while closed < depth` loop. Measured directly,
  *before* adding this entry's second fix below: a bare
  `RecursionError` at 1000 levels, with `self._depth` (the counter
  `_parse_expr` maintains) never exceeding 2 the whole time - because
  `_continue_expr`'s operand fetches call straight into
  `_parse_concat`/`_parse_multiplicative`/etc., bypassing
  `_parse_expr`'s own increment entirely, so nothing was counting the
  real stack cost of an outer LPAREN branch sitting on the stack
  through an entire nested re-entry.

The fix: `_parse_primary`'s LPAREN branch now increments `self._depth`
(the same counter and the same `_MAX_RECURSION_DEPTH` = 50
`_parse_expr` already uses) for its *own entire duration* - from
before the first `_parse_expr` call to the branch's final `return`,
covering every `_continue_expr` iteration - not only around that one
inner call. Measured after the fix: `_staggered_batch_chain` parses
up to 48 levels, raises `ParseError` at 49, and raises `ParseError`
(never `RecursionError`) at 5,000. 48 is lower than the pure-nesting
ceiling by roughly the same order of magnitude the 2026-09-01 entry's
own "function-call arguments" figure (15 frames/level, `_MAX_
RECURSION_DEPTH` = 50) already accepted as the cost of genuine
recursion - this is a new instance of that same category, not a new
kind of limit, and #100's own acceptance criteria only ask for "some
depth comparable to today's pure-nesting ceiling," measured and
recorded, not equal to it.

Two depth regimes for one grammar feature, not a design flaw:
`(((1)))` and `((1)+1)+1)...` both go through the same LPAREN branch,
but only the second one ever pays for genuine recursion, and it pays
only in proportion to how many times a staggered closing forces a
*fresh* re-entry from an operand position rather than from the cheap
batching loop. A fuzzer generating arbitrary paren nesting will
overwhelmingly produce shapes closer to the first regime; #100's own
"comparable to today's pure-nesting ceiling" phrasing already
anticipated that the worst adversarial mixed shape would not, and
should not, reach exactly 1000.

2026-09-28 - Pushdown negotiation is one accepts(term) call per term,
and a term is the bound AST subexpression

Issue #121. The spec fixed `capabilities()` and `scan(pushed)` but
not how a single term is offered, nor what a `Predicate` is.

The per-term call is `accepts(term: Predicate) -> bool` on the scan
source. The alternative, one `negotiate(terms) -> accepted` call,
lets a scan reorder, duplicate, or return a term it was never
offered; with a boolean per term the optimizer builds the accepted
list itself, so "an ordered subset of what was offered" holds by
construction rather than by each table's care. `capabilities()`
becomes the gate: an empty set means `accepts()` is never called,
which is also why every pre-#121 test fake still works unchanged.

A `Predicate` is the bound AST node for the term, not a second
representation. A pushdown-specific type (column, operator, literal)
would need a translation pass that either loses shapes a future
table can use or grows into a copy of the AST. The bound node
already has what #122 needs - `BoundColumnRef.offset` into the scan's
own schema, since only the `WHERE` filter directly above the `Scan`
is negotiated - and a table matches it with the same explicit
`isinstance` checks the evaluator uses. `PushdownKind` is a plain
string each table names for itself; the optimizer never interprets
it.

The optimizer is `optimize(tree) -> tree` in `plan/optimizer.py`, a
separate step `cli.py` calls between `plan()` and execution, so
`--no-pushdown` (#43) is "do not call it" and `--explain` (#42)
reads the outcome from `Scan.pushed()`. It rewrites in place - the
one write is `Scan.set_pushed(accepted)` - rather than rebuilding
the tree, because rebuilding every ancestor would mean every
operator exposing its constructor arguments for no decision. It
reaches the `Scan` through `exec/operators.py`'s `child_of()`, an
explicit `isinstance` chain, not through other modules' private
attributes. The `Filter` is read, never replaced: the predicate
object left in it is the one `plan()` put there. Term splitting uses
an explicit stack, not recursion, so a long left-deep `AND` chain is
not bounded by Python's recursion limit.

2026-09-28 - blame's path pushdown: one shape function, candidates
from ls-tree, the evaluator's own LIKE fold

Issue #122. `BlameScan` declares `path_eq`, `path_in` and
`path_like_prefix` and accepts only the shapes spec §2 lists, with
text literals only. A numeric literal is rejected even though TEXT
affinity would turn `path = 5` into `path = '5'`: that conversion is
`exec/expression.py`'s, and a second copy in the scan is a second
place for it to be wrong. Rejecting is always safe; the `Filter`
still answers correctly.

`accepts()` and `scan()` share one function that reads a term's shape
into a selection (exact literals, or a prefix). Two separate readings
could drift apart, so that a term accepted by one is narrowed wrongly
by the other. A term that function does not recognise narrows
nothing, even when handed to `scan()` directly.

The candidates are always a sub-list of `git ls-tree`'s output, never
the query's literals, so `git blame` is never run on a path that is
not tracked, and there is no failure from it to handle. A `LIKE`
prefix is compared through `historian.ascii.ascii_fold` on both sides
- the function `exec/expression.py`'s `LIKE` uses - because SQLite
folds ASCII case in `LIKE` and nothing else. A case-sensitive check
would drop matching rows (`SRC/b.py` for `LIKE 'src/%'`), and
`str.lower()` would add rows the `Filter` then rejects (`CAFÉ%`
selecting `café.py`), which is only wasted work but disagrees with
the evaluator about what matches.

`IN` blames in the list's own order, deduplicated; `=` and `LIKE`
keep `ls-tree` order; several pushed terms narrow one after another,
the first fixing the order. Row order without `ORDER BY` is still a
function of the repository and the query alone (spec §3), so this
stays deterministic. It does mean `--no-pushdown` (#43) can return
the same rows in a different order, which is allowed: only the
multiset is promised without `ORDER BY`.

The work record is three plain attributes on the scan -
`blamed_paths`, `git_invocations` (every `git` process, `ls-tree`
included) and `tracked_path_count` (for `--stats`' "12 of 4,013",
#42) - reset when `scan()` is called rather than at its first row, so
a caller that never iterates still sees a fresh record.

The case-folding criteria needed paths differing only by case, which
neither `tiny` nor `awkward` has, and adding them there would change
every existing differential case's rows. So `tests/fixtures/build.py`
gained a fourth fixture, `casefold`, built the same way and pinned by
HEAD hash. Its paths go into the index from blobs rather than through
the working tree, because `src/`, `SRC/` and `Src/` are one directory
on a case-insensitive file system and the build would otherwise
depend on the machine. Spec §4's fixture list is updated alongside.

2026-09-28 - nested aggregates and aliased-aggregate misuse are
BindErrors, not runtime crashes or silent zero rows

Issue #102. `count(count(*))` and an aggregate reached through a
select-list alias somewhere other than a clause's own direct
reference to it (typically another aggregate call's argument, as in
`HAVING count(c) > 0` with `c` aliasing `count(*)`) used to bind
without error and fail later, data-dependently: a runtime `EvalError`
from `exec/expression.py` when at least one row reached the bad call,
a silent `0` rows at exit 0 when none did. `sqlite3` rejects both
unconditionally at prepare time ("misuse of aggregate function
count()" and "misuse of aliased aggregate c" respectively, confirmed
against the oracle), and now so does historian, at bind time, before
either engine would touch a row.

Two checks, both reusing the existing `_contains_aggregate` walk
rather than a new one: `_bind_expr`'s `FunctionCall` branch rejects
any bound argument that itself contains an aggregate call, whether
written directly (`count(count(*))`) or spliced in through
`_resolve_name`'s alias substitution (`count(c)`); and `_resolve_name`
itself rejects a resolved candidate (real column or alias) that
contains an aggregate call whenever `ctx.reject_aggregates` is set -
the same flag `_validate_function_call` already uses to reject a
literal aggregate call written directly in `WHERE`, extended to catch
one reached through an alias instead (`WHERE c > 1`). Both checks run
after the argument or reference is already bound, since an alias only
reveals what it points at once resolved - checking the raw AST would
miss the alias case entirely.

This surfaced a gap in spec §3's "Errors" enumeration: it named three
kinds and did not name aggregate misuse as its own kind, even though
the binder has treated it as `BindError` since #60/#69. §3 gets a
fourth bullet, in this same commit, naming it explicitly - not a
change in behaviour, a description catching up to code that already
existed.

2026-09-28 - #78's SELECT DISTINCT/ORDER BY narrowing now catches an
unselected aggregate too, closing the gap the M3 milestone review
found

Issue #103. `SELECT DISTINCT author_name FROM blame GROUP BY
author_name, path ORDER BY count(*) DESC` bound and ran without error,
printing rows in whatever order `Sort` happened to produce for the
unselected `count(*)` key - confirmed live against the oracle
(`awkward_repo`'s unfiltered blame rows through `sqlite3` 3.45.1) that
this is the identical "sort key not determined by the deduplicated
output row" shape the 2026-09-25 entry above already decided to
reject for a bare column, just with an aggregate call in the key
position instead of a column reference. That entry's own reasoning
applies unchanged - `sqlite3` accepts the query and answers from its
own unspecified internals (here, `Sam Lee` then `Zoë Müller`; swap the
oracle's own two rules for what a deterministic engine might do and
each predicts a different, and different again, order - see that
entry for the discriminating arithmetic in full) - so this was always
meant to be a `BindError` under #78's own rule. It was not: the root
cause was in the code, not the decision.

`_split_for_grouped_check` had one `FunctionCall` branch shared by
three callers - the two GROUP BY/HAVING-keyed narrowings (#60/#69,
2026-09-19/2026-09-24 entries), where an aggregate call is *never*
required to shape-match a given key (that is what makes a bare column
under it exempt at all), and the DISTINCT/ORDER BY narrowing above,
matched against the *select list* instead, where an aggregate call is
exactly as much a "key touch" as a bare column and must itself
shape-match a select-list item the same way. The branch returned
"contains an aggregate, no bad column" unconditionally for any
`FunctionCall`, which was correct for the first two callers and wrong
for the third - it made every unselected aggregate call in a DISTINCT
query's `ORDER BY` invisible to the walk, rather than caught by it.

Fixed with a keyword-only `strict_function_calls` flag on
`_split_for_grouped_check`, `False` by default (the two GROUP BY/HAVING
callers, unchanged) and `True` only at the DISTINCT/ORDER BY call site
in `bind()`. When set, a `FunctionCall` that does not shape-match one
of the given keys comes back as the walk's "bad" node, exactly as a
bare column already does - so the return type widens from
`BoundColumnRef | None` to `Expr | None`, and the DISTINCT call site
branches on `isinstance(bad, FunctionCall)` to phrase the `BindError`
around an aggregate rather than a column name. No other caller's
behaviour changes: the flag defaults to today's rule everywhere else,
and the existing GROUP BY/HAVING test suite (`tests/test_binder.py`,
`tests/differential/test_blame.py`) still passes unmodified. This is a
bug fix to code that did not yet implement #78's own already-made
decision, not a new design decision.

Confirmed against the oracle for every acceptance shape: the
unselected-aggregate case above and its always-false-`WHERE` and
`count(*) + 0`-nested variants all raise `BindError` now, data-
independently, before any row is read; every already-legal shape
(the aggregate matched by exact select-list shape, by alias, by
ordinal, and - newly tested - by an expression built purely from the
selected aggregate's own alias, `ORDER BY c + 1`) stays legal,
unchanged.

`_docs/spec.md` §3's "Errors" section's "Aggregate misuse" bullet
gained this shape in the same commit as the fix, mirroring #102's own
"fix the code, catch the spec up in the same commit" discipline: an
`ORDER BY` key under `SELECT DISTINCT` - bare column or aggregate call
alike - that is not itself a select-list item or built purely from one
is now named alongside the bullet's other four shapes, with its own
sentence explaining why it is the one of the five `sqlite3` does not
itself reject at prepare time.

2026-09-28 - #103 round 2: aggregate shape-matching is ASCII-case-
insensitive, like every other function-name comparison in this engine

QA (comment #5869548584 on issue #103) found that the fix recorded in
the entry just above introduced a real regression: `_expr_shape_equal`'s
`FunctionCall` branch compared `a.name == b.name` on the raw,
un-folded text the parser stored, never lower-cased the way
`_validate_function_call` already folds a name before checking it
against `_AGGREGATE_NAMES`. With `strict_function_calls=True`, that
made shape-matching case-sensitive for the one caller that needs it to
work: `SELECT DISTINCT author_name, COUNT(*) FROM blame GROUP BY
author_name, path ORDER BY count(*) DESC` (select list spells it
`COUNT`, `ORDER BY` spells it `count`) raised `BindError`, even though
the aggregate *is* selected and the sort key *is* fully determined by
the output row - confirmed against the oracle (`tests/oracle.py`,
`sqlite3` 3.45.1) that SQLite accepts the query and returns 2 rows,
since no SQL engine distinguishes function-name case at all. This is
squarely the failure mode #78's own decision exists to avoid causing -
a sort key genuinely determined by the output row was being rejected.

Fixed by folding both operands through `historian.ascii.ascii_fold`
inside the `FunctionCall` branch of `_expr_shape_equal` - in both
copies, `sql/binder.py`'s and `plan/planner.py`'s (the module docstring
on the latter already explains why it is a second, independent copy
rather than a shared import) - rather than normalizing
`FunctionCall.name` itself once at bind time. Folding at comparison
time was chosen as the more explicit, boring fix: `FunctionCall.name`
is also read verbatim in `no such function: {call.name}` and `misuse of
aggregate function {call.name}(): ...` error messages elsewhere in
`sql/binder.py`, and normalizing the stored value would change what
those messages echo back to whoever wrote the query, for no benefit -
folding only at the point where two names are being compared for
equality keeps every other reader of `.name` exactly as it was, the
same way `_validate_function_call` already folds its own local copy
without touching the AST node.

Confirmed against the oracle both before and after the fix that this
does not disturb the two callers matched against `GROUP BY` keys
instead of the select list: `SELECT author_name, COUNT(*) FROM blame
GROUP BY author_name HAVING count(*) > 1` (mismatched case, HAVING) and
`SELECT path, COUNT(*) FROM blame GROUP BY path ORDER BY count(*)`
(mismatched case, non-DISTINCT GROUP BY/ORDER BY) already bound and ran
correctly before this fix - that `FunctionCall` branch returns
`(True, None)` unconditionally there regardless of name, so name
casing was never load-bearing for those two callers - and still do
after, unchanged.

2026-09-28 - TEXT past int64 is REAL at conversion time, decided once
in `_scan_number`

Issue #105. `exec/expression.py`'s `_scan_number` is the one place
TEXT becomes a number: arithmetic's leading-prefix coercion
(`arithmetic_operand`), column affinity's whole-string coercion
(`try_numeric_affinity`), and `sum`/`avg`'s classification in
`exec/operators.py` (which calls those two) all go through it. For a
plain digit run - no `.`, no exponent - it used to return
`int(text)`, unbounded. Only an arithmetic *result* was ever bounded
(`_int64_bounded`, 2026-09-01), so `'9223372036854775808' - 1`
subtracted exactly and landed back inside int64 as the INTEGER
`9223372036854775807`; `sum('9223372036854775808')` took `sum`'s exact
integer path and raised `integer overflow`; and `'999...9' + 0` (320
nines) crashed with `OverflowError`, because `float()` of a huge
Python `int` raises where `float()` of the same digit text gives
`inf`. A digit run past 4300 digits raised `ValueError` from `int()`
itself (Python's int-string conversion limit).

SQLite classifies at conversion time: a digit run is INTEGER only if
it fits int64, else REAL, before any operator runs. Confirmed with
`tests/oracle.py` (module `sqlite3` 3.45.1): `'9223372036854775808' -
1` is `0x1.0000000000000p+63` REAL, `sum(...)` of it over 3 rows is
`0x1.8000000000000p+64` REAL, the 320-nine and 5000-nine runs are
`inf`, and `'9223372036854775807' - 1` / `'-9223372036854775808' + 0`
stay INTEGER - sign, leading zeros (including 5000 of them) and
surrounding whitespace do not move the boundary.

The rule now lives at that single point. `_int64_digit_run` decides
the range from the digit text alone: it strips the sign and leading
zeros, rejects more than 19 significant digits without converting, and
only then calls `int()` on at most 19 digits and compares against
`INT64_MIN`/`INT64_MAX`. Out of range, `_scan_number` returns
`float(number_text)` - the text, never a Python `int` - so a huge run
overflows to `inf` and nothing raises. No caller bounds its own result
any more for this case; `_int64_bounded` still bounds arithmetic
results, which is a different overflow. The `float()` call stays inside
`_scan_number`, already on the allowlist of
`test_no_stray_float_calls_outside_the_named_exceptions`.

`%` changes too, because it reaches TEXT through the same
`arithmetic_operand`: a digit-run TEXT operand past int64 is now a
REAL operand, truncated and clamped to int64 per #75. That matches
`sqlite3` for a finite one - `'9223372036854775808' % 3` is `1.0` (was
the INTEGER `2`, `2**63 % 3` unclamped), `5 % '9223372036854775808'`
is `5.0` (was `5`), `'-9223372036854775809' % 7` is `-1.0` (was `-2`).
For a run too large for a double, `%` used to return a wrong exact
remainder (`'999...9' % 3` gave `0`; `sqlite3` gives `1.0`) and now
reaches `_int64_truncated` with `inf` and raises `OverflowError` from
`math.trunc` - the same crash `('1e400'+0) % 3` already has, which is
#106's to fix, along with `%`'s own `sqlite3Atoi64`-style TEXT scan.

Not matched: the last bit of the REAL for some long digit runs.
`float(text)` is correctly rounded; `sqlite3`'s `sqlite3AtoF` is not.
Measured on this machine (x86_64, module 3.45.1) with random 19-to-320
digit runs bound as TEXT and converted by `? + 0`: 44 of 56400 differ
from historian by one ULP. This is not new - before #105 such a run
went `int(text)` then `float(exact)` in `_int64_bounded`, also
correctly rounded - and the same gap already exists for TEXT with a
`.` or an exponent, and for SQL literals (2026-09-25 entry). A Python
emulation of 3.45.1's x86_64 path (keep 19-ish leading digits in a
u64, scale by powers of ten in 80-bit long double, round to double)
matched 9420 of 9420 samples, but that path is platform-specific
(`long double` is `double` on aarch64, where SQLite uses a Dekker
double-double path instead) and version-specific, so porting it is a
decision for its own issue, not a side effect of this one. Every value
#105's acceptance criteria name is an exact match.

2026-09-28 - `%` reads TEXT with its own digit-stop scan, and clamps
infinity

Issue #106. `%` (#75) took its operands from `arithmetic_operand`,
the same general text-to-number conversion as `+ - * /`, so `'1e3' %
7` computed `1000 % 7` and gave `6.0`. SQLite gives `1.0`. Its
`OP_Remainder` classifies each operand with `numericType` (the
general conversion, which is why the result is REAL) but takes the
integer it divides from `sqlite3VdbeIntValue`, which for TEXT is
`sqlite3Atoi64`: whitespace, sign, digits, stop at the first
non-digit, clamp to int64. For a REAL it is `doubleToInt64`, which
clamps infinity like any other out-of-range magnitude.

So a `%` TEXT operand now goes through `_modulo_text_operand`, a
sibling of `_scan_number` rather than a change to it: the class still
comes from `_coerce_arithmetic_text`, the value from a digit-stop scan
whose digit run is range-checked by `_int64_digit_run` (#105) and
clamped by sign when it does not fit. `_scan_number`,
`arithmetic_operand` and `+ - * /` are unchanged. Confirmed with
`tests/oracle.py` (module `sqlite3` 3.45.1): `'1e3' % 7` is `1.0`,
`'1.5e2' % 7` is `1.0`, `'-1e2' % 7` is `-1.0`, `'1e400' % 3` is
`1.0`, `'99999999999999999999e0' % 7` is `0.0` (int64 max % 7),
`'5e' % 3` is the INTEGER `2` (a dangling exponent is not one), and
`'abc' % 5` is the INTEGER `0`.

`_int64_truncated` now returns `INT64_MAX`/`INT64_MIN` for `+inf`/
`-inf` before calling `math.trunc`, which raised `OverflowError` and
reached the exit-4 backstop for `('1e400'+0) % 3` and, since #105,
for a 320-digit TEXT run. NaN cannot reach it: `squash_nan` makes a
NaN operand NULL first.

Not changed: `_NUMERIC_WHITESPACE` omits `\v`, which `sqlite3Atoi64`
and `sqlite3AtoF` both skip (`'\v12' % 5` is `2` in `sqlite3`). The
new scan uses the same constant as `_scan_number` so the value and
the class cannot disagree about where a number starts; widening it is
a change to every TEXT conversion, not to `%`.

2026-09-28 - Expression trees are capped at SQLite's own height, 1000,
and every walk below the parser is an explicit-stack loop

Issue #107, from the M3 review. A query nested deeper than Python
could walk raised `RecursionError`, and `RecursionError` is a
`RuntimeError`, so `cli.py`'s repository clause reported it as "could
not read repository", exit 3. The parser reads left-deep chains
(`1 + 1 + ...`, `a AND b AND ...`) with a loop and had no limit on
them at all; the binder, planner and evaluator then recursed once per
level. Measured on `main` at fa20120, default recursion limit 1000:
the evaluator used two frames per level for `+`/`||`/`LIKE` (fails at
497 terms), three for `=`/`<`/`IS`/`IN` (332), the binder's
`_bind_expr` and the planner's `_split_expr` one (989 to 992), and
`_ordinal_value` one (994).

The limit is SQLite's `SQLITE_MAX_EXPR_DEPTH`, exactly: 1000, with
SQLite's own message, "Expression tree is too large (maximum depth
1000)", as a `ParseError` (exit 1). Not lower: SQLite answers a
601-term `+` chain, and §1 makes that the right answer, so a limit
sized to what Python could walk would turn a crash into a permanent
mismatch. Not unlimited: SQLite rejects anything taller, and an
engine that accepts what SQLite rejects is the same divergence the
other way. `sql/parser.py`'s `_expr_height` measures each finished
clause expression with an explicit stack, by SQLite's per-node rules
read off the oracle node kind by node kind (`IN (c)` with one
constant `c` is `= +c`, one extra level; `IN ()` is a leaf; `BETWEEN`'s
bounds add nothing; `NOT LIKE`/`NOT IN`/`NOT BETWEEN` add a `NOT`;
`t.c` is two levels; `x AND 0` collapses to a leaf; `LIMIT`/`OFFSET`
sit under one extra node). A test asserts the oracle's
`getlimit(SQLITE_LIMIT_EXPR_DEPTH)` equals the parser's constant, so
drift (#117) fails loudly. The check runs before binding, so a too-
deep query exits 1 even with `-C` on a directory that does not exist.

Of the issue's two ways to make the walks safe up to that height,
this takes (a), explicit stacks, not (b), raising the recursion
limit in `cli.py`. `evaluate()` and its `_finish_*` helpers, the
binder's `_bind_expr`, `_contains_aggregate`, `_expr_shape_equal`,
`_split_for_grouped_check` and `_ordinal_value`, and the planner's
`_split_expr` and `_expr_shape_equal` are each a loop over a plain
list, in the style of the optimizer's `split_conjuncts` (#121),
visiting operands left to right so error order, `AND`/`OR`
short-circuits and aggregate slot order are what the recursive
versions gave. Reasons for (a):

- A bound tree is not bounded by the parse-time height. A select-list
  alias referenced from `WHERE` is spliced in as its whole bound
  tree, so a query SQLite accepts can bind to a tree about 2000
  levels tall. A recursion limit derived from "1000 levels times
  three frames" would have been wrong on the first such query.
- (b) is what #8 (2026-09-01) already rejected - "`sys.
  setrecursionlimit` is not a fix: it relocates the cliff" - and the
  objection holds here: the cliff would move with every new frame per
  level anyone adds, and the process-wide setting also changes what
  every other caller of the library gets. With explicit stacks,
  nothing the query does grows Python's stack, so the outcome cannot
  depend on how deep the caller already was. `tests/test_cli.py` runs
  one accepted and one rejected boundary query from 0 and from 200
  extra frames and asserts identical output; the unit tests build
  5000-level trees and walk them from 700 frames deep.
- It is the shape a Rust port needs anyway.

The parser's own recursion limits (#8's 1000-long `(`/`NOT`/unary
runs, #100's resume bookkeeping, the 50-level nesting cap) are
unchanged. Where they overlap with the height - 1000 `NOT`s or unary
operators around a literal is height 1001 - the height check now
rejects the tree, so three #8 parser tests that pinned exactly that
(1000 `NOT`s, 1000 unary minus, a 5000-term `AND` chain) now pin the
tallest tree SQLite accepts instead; a run longer than 1000 is still
rejected by the run limit first, while it is being read. Historian
stays more permissive than SQLite for unary, `NOT` and paren runs:
SQLite's LALR stack gives "parser stack overflow" from 90 operators
after `LIMIT` and 95 in the select list, and #8 chose on purpose not
to copy that build-specific quirk, so those chains (up to height
1000) evaluate here and are tested as plain tests, not differential
ones.

A `RecursionError` that still reaches `cli.main` now has its own
clause, ordered before `(OSError, RuntimeError)`, and goes to #49's
fixed internal-error message, exit 4. With the height capped and no
walk recursing per level, reaching it means historian's own invariant
broke: spec §5's "a bug in historian, not a mistake in the query".
Exit 1 would blame the user for historian's bug; exit 3 was simply
false. Narrowing the repository clause to a dedicated git-failure
type would remove the class of problem, but nothing else in `src/`
raises a `RuntimeError` subclass today, so it was noted in the issue,
not done.

Left for later: nested `BETWEEN` and multi-element `IN` evaluate their
left operand once per bound or element, so chains of them are
exponential in time (#137); they are within the height limit and
correct, only slow, and the depth tests leave them out. `CASE` (#126)
must add its node to `_height_children`/`_node_height` and to each
module's `_operands`/`_with_operands`.

2026-09-29 - The differential harness compares REALs by `float.hex()`,
and unary minus is `0 - x` except over a REAL literal

Issue #110, from the M3 review. The harness matched cells with
`type(a) is type(b) and a == b`, and `0.0 == -0.0` in Python, so a
zero of the wrong sign passed. It hid a live mismatch: `SELECT
-(line_no * 0.0) FROM blame` is `0x0.0p+0` on every row in SQLite and
was `-0x0.0p+0` in historian, because `_finish_negate` flipped the
sign of every REAL. SQLite codes unary minus over anything but a
literal as `0 - x`, which is `+0.0` for either zero, and folds a REAL
literal directly under `-` into a negative literal.

Measured with `tests/oracle.py` (module `sqlite3` 3.45.1), by
`float.hex()`: `-(line_no * 0.0)`, `-(0.0 * 1)`, `-(0.0 / 1)`, `-(0.0
% 5)`, `-(1 - 1.0)`, `-(-0.0 + 0)`, `-(+0.0)`, `-'0.0'`, `-'0.0abc'`,
`-'1e-400'`, `-'-0.0'`, `-(-0.0)` and `-(-(-0.0))` are all `0x0.0p+0`;
`-(0.0)`, `-((0.0))` and `-0.0` are `-0x0.0p+0`; `-'0'` and `-(0)` are
the INTEGER `0`; `-('1e400'+0)` is `-inf` and `-(-('1e400'+0))` is
`inf`. The first eleven of those were `-0x0.0p+0` in historian.

Harness: two cells match only with the same Python type and, for
REAL, the same `float.hex()`; no tolerance. A NaN on either side is a
failure of its own, checked before any sort, so the harness does not
rely on `squash_nan` and a NaN cannot mis-order the sort. Because
`order_key` ties `0`, `0.0` and `-0.0` and the sort is stable, the
multiset sort key gained an exact tie-break (type name, then the
value, a REAL by hex), or two equal multisets could line up a `0.0`
against a `-0.0`. `ORDER BY` grouping stays on `order_key`, since the
two zeros really do tie there on both engines; within a group the
compare is exact.

Engine: `_finish_negate` computes `0.0 - x` for a REAL, and `_start`
returns `-value` directly for a `UnaryOp(NEG, Literal(<float>))`, a
structural check beside the int64-minimum one (which still runs
first). Parentheses never reach the AST, so `-((0.0))` is that shape;
unary `+` does, so `-(+0.0)` is not. `0 - x` equals `-x` exactly for
every non-zero double, so nothing else moves.

The two changes land together. The exact comparator alone turned none
of the 453 existing differential tests red, and the new zero-sign
cases need the engine fix, so landing the harness first would have
meant either leaving them out or marking them xfail.

2026-10-01 - Evaluation stops early only in condition context; in
value context every operand is evaluated

Issue #111, from the M3 review. The #51 entry (2026-09-25) says
"SQLite evaluates `AND`/`OR` left to right and stops once the result
is decided". As a general statement that is wrong, and this entry
corrects it; the #51 entry stays as written. It held for the shapes
#51 tested, which were all at the root of `WHERE`, and the evaluator
applied it everywhere: `AND` stopped on a `FALSE` left side and `OR`
on a `TRUE` one in every clause, `IN` and `BETWEEN` never stopped, and
nothing depended on `NULL` or `NOT`. Only an error makes the order
visible, and today only an invalid `LIKE ... ESCAPE` raises, so
`SELECT (line_no = 5 AND <raises>) FROM blame` returned rows where
SQLite raises, and `WHERE line_no = NULL AND <raises>` raised where
SQLite returns no rows.

The rule, measured against the oracle (Python's `sqlite3` module,
SQLite 3.45.1 - not 3.50.4, see #117; re-run on version drift):

- Value context (a select-list item, under `DISTINCT` too, an `ORDER
  BY` key by expression, alias or ordinal, a `GROUP BY` key, an
  aggregate argument, any operand of a comparison, arithmetic, unary
  `+`/`-`, `||`, `IS`, `LIKE`, `IN` or `BETWEEN`): every operand of
  `AND`, `OR`, `NOT` and `BETWEEN` is evaluated.
- Condition context (the root of `WHERE` and `HAVING`, and operands of
  `AND`/`OR`/`NOT` in condition context): `AND` stops after a `FALSE`
  left side, `OR` after a `TRUE` one, and a `NULL` left side stops
  `AND` where `NULL` counts as `FALSE` - the root, and under an even
  number of `NOT`s - and `OR` where it counts as `TRUE`, under an odd
  number. `AND`/`OR` pass that polarity to both operands and `NOT`
  flips it, which is the issue's "`NOT` flips what the clause is
  waiting for", generalised to any depth. `BETWEEN` is `x >= low AND
  x <= high` and `NOT BETWEEN` is `NOT` over that.
- `IN` stops at the first element equal to the left side, in both
  contexts; a `NULL` element or left side never stops it.

These are SQLite's code generator's jump rules: `sqlite3ExprIfTrue`/
`IfFalse` with a jump-if-NULL flag, where `AND` under `IfTrue` codes
its left side with `IfFalse` and the flag inverted, and `NOT` swaps
`IfTrue` for `IfFalse`. That was the model the sweep was checked
against; the oracle, not the model, is what the tests compare with.

The sweep. A reference model of the rule was run against SQLite
over `tiny`'s `blame` rows for every formula built from `AND`/`OR`/
`NOT` with up to three binary operators, `NOT` optional on every
operator node (and on every leaf for up to one operator), leaves
`TRUE`/`FALSE`/`NULL`/`ERR`, each in `WHERE`, `HAVING` (over `max()`
leaves, so nothing moves to `WHERE`, #141), the select list, `ORDER
BY` and `GROUP BY`: 421,160 queries, 0 disagreements. With a fifth,
per-row mixed leaf (`line_no < 2`), up to two operators: 22,050
queries, 0. `IN`/`NOT IN` with one to three elements from the leaf
set or the left side itself, and `BETWEEN`/`NOT BETWEEN` with each of
the three from the leaf set, in `WHERE`, under `NOT` in `WHERE` and in
the select list: 8,490 queries, 0. The issue's rule and the oracle
agree; nothing in the issue needed correcting.

One finding about the issue's suggested leaves. With `line_no = 1`,
`line_no = 5` and `line_no = NULL` as leaves, 36 of 22,050 queries
differ from the model, all in `WHERE` with the leaf as a top-level
conjunct: SQLite propagates `column = constant` into the other
conjuncts and folds what becomes constant, so `WHERE NOT (line_no = 5
AND <raises>) AND line_no = 5` raises (the `5 = 5` folds away) and
`WHERE (<raises> AND line_no = NULL) AND line_no = 1` does not. That
is the constant-folding difference #51 already accepted, not
evaluation order, so the sweep's leaves are `line_no >= 1`, `line_no
> 5` and `line_no < NULL`, which SQLite does not propagate.

Design. The context reaches the evaluator as two entry points rather
than a flag: `evaluate()` is value context and keeps every existing
caller (`Project`, `Sort`, `GROUP BY` keys, aggregate arguments);
`evaluate_condition()` is condition context and has one caller,
`Filter`. Inside, both run the #107 work-stack loop, now with a
`_Context` on every entry - `VALUE`, `NULL_IS_FALSE` or `NULL_IS_TRUE`
- which only `AND`/`OR`/`NOT` pass on to their operands; every other
node evaluates its operands as values. `AND_AFTER_LEFT`/
`OR_AFTER_LEFT` exist only in condition context; in value context
`AND`/`OR` push both operands and `FINISH_AND`/`FINISH_OR`. `IN`
became incremental, one `IN_AFTER_ELEMENT` step per element carrying
its index, so it can stop; `BETWEEN` in condition context stops after
`BETWEEN_AFTER_LOW`. A stopped `AND` keeps its `NULL` left side as its
result, so `evaluate_condition()` can return `NULL` where the full
value is `FALSE`. That is safe because the parent shares the
polarity, so the `NULL` counts the same way the `FALSE` would; the
unit tests check that the two entry points keep exactly the same
rows. Still no recursion: the depth tests run through both entry
points.

The module docstring's claim that `evaluate()` "does not carry a
notion of the position this whole call's result is about to be used
in" no longer holds. It still holds for the result's shape, `Value`
vs `Bool3`, which is decided by the node alone, and for the #38/#63
coercions. It does not hold for which operands are evaluated, because
SQLite's own answer depends on the position. The docstring now says
so.

Testing cost. The `k <= 3` sweep through historian's whole pipeline
is about 3.5 minutes, which is too long for every run. The suite runs
`k <= 2` by default (2,312 formulas in five placements, 11,560
queries, plus 2,736 `IN`/`BETWEEN` queries), grouped by formula
skeleton into 249 tests. `HISTORIAN_SWEEP_OPERATORS=3` runs the full
`k <= 3` set: 1,785 tests, 421,160 queries, all agreeing with the
oracle (3.45.1), in 3.5 minutes. Both serve `tiny`'s rows from memory after one real
scan, to avoid running `git blame` per query; nothing is pushed for
any sweep query.

Not changed: `HAVING` terms without an aggregate, which SQLite moves
to `WHERE` (#141); constant folding (#51); evaluating the `IN` left
side and the `BETWEEN` operand once (#137). `CASE WHEN` and `JOIN ...
ON`, once they exist, are condition context and use
`evaluate_condition()`.

2026-10-02 - Binding errors are reported in SQLite's cross-clause
order, and historian's own rejections come after all of them

Issue #115, from the M3 review. `bind()` bound the select list, then
`GROUP BY`, then `WHERE`, `HAVING`, `ORDER BY` and `LIMIT`, and ran
historian's own narrowings in between, so `SELECT path FROM blame
WHERE ghost_w = 1 GROUP BY ghost_g` named `ghost_g` where SQLite
names `ghost_w`. Its docstrings described a third order. The order is
now SQLite's rather than the docstrings corrected: §1 makes SQLite
the definition of correct, the measured order is mechanical to
follow, and the docstrings were wrong either way.

The order, measured against the oracle (Python's `sqlite3` module,
SQLite 3.45.1 - see #117; the PM's drift check on 3.51.1 was not
re-run here):

1. The `FROM` table, then the qualifier of any `x.*` select-list item.
2. `LIMIT`, then `OFFSET`, for what SQLite rejects there: a column
   reference outside every aggregate call (any column, even a real
   one or an alias - `LIMIT` sees none) is reported at once; an error
   from inside an aggregate call - its arity, else its misuse, then a
   column reference among its arguments - is reported only after both
   clauses, the last one found winning.
3. The select list, items left to right.
4. `HAVING` on a non-aggregate query.
5. `HAVING`.
6. `WHERE`; in a non-aggregate query an aggregate call is reported
   here, in place.
7. `ORDER BY`: all name errors, then an out-of-range ordinal.
8. `GROUP BY`: all name errors, then an out-of-range ordinal, then an
   aggregate key.
9. An aggregate call in the `WHERE` of an aggregate query or the
   `ORDER BY` of a non-aggregate one ("aggregate query": `GROUP BY`
   written, or an aggregate call in the select list).
10. historian's own rejections: the grouped narrowing in the select
    list, `HAVING` and `ORDER BY` (2026-09-19, 2026-09-24), the
    `DISTINCT` `ORDER BY` narrowing (2026-09-25, #103) and the
    literal-only `LIMIT` (2026-09-25, #77), in that order.

Within one call: unknown function, then arity, then misuse. A nested
aggregate is reported where it is found, in every clause, like a name
error. "Name error" here is no such column or function, wrong arity,
or a nested aggregate.

Evidence. A catalog of 27 erroring fragments in 7 clauses (unknown
table, column and function, wrong arity, nested aggregate, aggregate
in `WHERE`/`ORDER BY`/`LIMIT`/`GROUP BY`, out-of-range ordinal, an
unknown `x.*`, an `OFFSET` column), each spliced in place of its
clause into three base queries - grouped (`... WHERE line_no = 1
GROUP BY path HAVING count(*) > 0 ORDER BY path LIMIT 1`), plain (no
`GROUP BY`/`HAVING`) and aggregate by select list (`SELECT count(*)`,
`HAVING`, no `GROUP BY`) - gives 79 single fragments, 874 pairs and
5,242 triples from distinct clauses (the issue measured 241 pairs and
1,306 triples on one base). The order above predicted SQLite's choice
in every one, and `tests/differential/test_error_order.py` runs all
of them against the live oracle by default (about five seconds,
every case fails in `bind()`, so no git work), with the issue's 68
rows and 33 further measured cases.

Where the oracle contradicted the issue:

- An aggregate call in `LIMIT` is not reported at `LIMIT`'s turn
  before `OFFSET`: `LIMIT avg(1) OFFSET ghost_f` reports `ghost_f`,
  and `LIMIT count(*) OFFSET sum(1)` reports `sum`. SQLite records
  that misuse without stopping, and a later error overwrites it. It
  is still reported before the select list (`SELECT ghost_s FROM
  blame LIMIT count(*)` is the misuse). Columns among the call's
  arguments behave the same way (`LIMIT count(ghost_x) OFFSET
  ghost_f` reports `ghost_f`). Step 2 above is the oracle's rule.
- The out-of-range message names the term: `ORDER BY 1, 99` is "2nd
  ORDER BY term out of range", `3rd`, `11th`, `21st` likewise.
  historian always wrote "1st", which was only right for the first
  term. The issue pinned these messages exactly, so this was fixed.
- `sum(*)` (and `avg`/`min`/`max`) is SQLite's arity error, "wrong
  number of arguments to function sum()"; historian had its own
  wording, a different kind, so `WHERE sum(*) > 1 GROUP BY ghost_g`
  could not match by kind. It now uses SQLite's message.

The issue's "51 of the 68 queries differ today" was recounted on the
baseline (47af34d) with the new test file before any code change:
51 of 68, correct. On the baseline 428 of the 874 pairs failed as
well.

Design. `bind()` is the ten steps above as straight-line code, one
comment per step. Nothing is raised out of order and caught: the
late misuse is collected rather than raised, through a
`late_misuse: list[BindError] | None` on the binder's `_Context`.
`None` raises on the spot; a list keeps the first error and binding
goes on as if the aggregate were legal, and `bind()` raises it after
`GROUP BY`. `WHERE` gets the list only in an aggregate query, `ORDER
BY` always (it only rejects aggregates in a non-aggregate query).
`GROUP BY` and `ORDER BY` bind in passes over their terms - non-
ordinal terms, then ordinals, then (`GROUP BY`) aggregate keys - and
build the same keys in clause order. `LIMIT`/`OFFSET` get one
explicit-stack walk (#107) that finds what SQLite rejects there; the
literal-only rule is unchanged and runs last. The "is this an
aggregate query" decision is made once, after the select list, in
the existing style; consolidating it with the others is #112. Every
query the test suite binds - 13,599 of them, against the binder at
47af34d - binds to the identical tree, and no query changed between
binding and raising. No existing test pinned the old order.

Not changed: the order of different error kinds inside one
expression tree, a non-aggregate function call in `LIMIT`/`OFFSET`
(still the literal-only rule, reported last), and aggregate-misuse
messages that say `WHERE` for an `ORDER BY` - all #144. historian's
wording for the aggregate-misuse and `HAVING` errors stays its own
(#102).

2026-10-02 - `--explain` runs `ls-tree` but never `blame`; the plan goes
to stdout, `--stats` to stderr; `Project` is printed

Three small calls #42 made where spec §5 was silent or inconsistent.
(1) §5 says `--explain` does not run the query, yet its example shows
`12 of 4013 paths`, which needs the list of tracked paths. `--explain`
makes exactly one `git` call, `ls-tree`, through the scan source's own
`estimate(pushed)`, which reuses the narrowing `scan()` uses; it never
runs `git blame`, so the work record afterwards is one invocation and
no blamed path. (2) §5 sends results to stdout and everything else to
stderr. The plan is what `--explain` was asked for, so it goes to
stdout and can be piped; `--stats` goes to stderr after the results,
so stdout is byte-identical with and without it. `--explain --stats`
prints the plan only, and `--stats` prints nothing after any error.
(3) The planner always builds a `Project`, and the spec's example
omitted it; the printer shows every operator and the example was
edited to match. A call written twice (`count(*)` in the select list
and in `ORDER BY`) occupies two `Aggregate` slots but is listed once on
the `Aggregate` line, because the line says what is computed.

2026-10-02 - A fifth fixture, `numeric`, so a bare `line_no` can tell
numeric order from text order

Issue #109. `tiny`'s largest `line_no` is 2 and `awkward`'s is 6, so
every comparison, sort or `max` of a bare `line_no` gave the same
answer done as a number or as text, and #99 was reachable only
because `sum(line_no)` reaches two digits. The cheaper-looking fix, a
12-line file added to `awkward`, was measured in a scratch copy before
deciding: 18 existing tests fail (12 differential cases hard-coded to
`awkward`'s 12 rows, `test_awkward_yields_exactly_twelve_rows`, one
`--explain` test in `tests/test_cli.py`, and four fixture pins). Each
would be a rewrite of an assertion that was correct, which is where a
differential case quietly stops meaning what it meant. So, following
the `casefold` precedent, `tests/fixtures/build.py` gained a separate
fixture: Ana commits `long.txt` (120 lines), `mid.txt` (12) and
`short.txt` (3), and Bo rewrites `long.txt` lines 10-99, giving 135
blame rows. It is pinned by `NUMERIC_HEAD`, and `_verify_numeric`
asks Python's `sqlite3`, not historian, that `max(line_no)` is 120
while `max(CAST(line_no AS TEXT))` is `'99'` and that `line_no > 9`
holds for 114 rows against 10 as text, so an edit that restores
agreement fails the build. Cost accepted: one more fixture to build,
pin and cache. Spec §4's fixture list is updated alongside.

2026-10-02 - Shape equality compares an aggregate's `DISTINCT` flag

Issue #131. A bug fix to the implementation of #78 and #103, not a
new decision: the rule is unchanged, and `count(x)` and `count(DISTINCT
x)` were always meant to be different expressions. Both copies of
`_same_node_fields` (`sql/binder.py` and `plan/planner.py`) compared a
`FunctionCall` by folded name only, so under `SELECT DISTINCT` an
`ORDER BY count(DISTINCT path)` matched a selected `COUNT(path)` and
sorted by it. Each copy now also compares `distinct` as a plain field.
The binder copy is the live fix: the query is now #78's
unselected-aggregate `BindError`. The planner copy is defensive, kept
mirrored for #112 to consolidate: `_split_expr` gives every call its
own slot and `GROUP BY` keys cannot be aggregates, so no query reaches
it with two aggregate calls.

2026-10-02 - One shared module for the expression walks

Issue #112. The children table existed three times (`sql/binder.py`,
`plan/planner.py`, `sql/parser.py`), rebuild twice and shape equality
twice, and "is this an aggregate query" was decided in three places.
Adding a field to an expression node took an edit in each copy, and
nothing failed when one was missed: #101 (`LIKE ... ESCAPE`) and #131
(`count(DISTINCT x)`) were both a copy left behind. The copies are
merged into `src/historian/sql/walk.py` - `children`,
`with_children`, `expr_shape_equal`, `contains_aggregate` and
`is_aggregate_query` - written as plain `isinstance` chains, and
`tests/test_walk.py` builds every node type by reflection and fails
when a field is not handled. This supersedes the 2026-09-28 advice to
edit each module's `_operands`/`_with_operands` when a node is added.

The module sits in `sql/` because it depends only on the AST and is
needed by the parser, which must not import the binder or anything
above it. `BoundColumnRef` moved into it, since the walks must know
it and cannot import the binder; `sql/binder.py` re-exports it, so
existing imports name the same class.

The predicate takes the `GROUP BY` keys and the expressions to look
at. The binder passes the select list alone, SQLite's rule; the
planner passes the select list, `HAVING` and `ORDER BY`, which gives
the same answer for every bound statement.

Two unreachable edges were settled by the reflection tests rather
than left implicit: shape equality now handles `ColumnRef` (by table
and name; only bound trees are compared in practice) and compares
`BoundColumnRef.name` as well as its offset. Within one bound
statement the offset determines the name, so no query changes.

2026-10-02 - Grouping keys on an explicit values.group_key

Issue #113. `GROUP BY`, `SELECT DISTINCT` and `count/sum/avg(DISTINCT
x)` keyed on `values.order_key`, whose payload is the raw value. The
groups were right, but only because Python's `1 == 1.0` and `hash(1)
== hash(1.0)` compare an `int` against a `float` exactly. AGENTS.md
says code leaning on Python's dynamism is redesigned rather than
translated, and a Rust port has no exact, hash-consistent `i64 ==
f64`. So the key is now explicit: `values.group_key` maps a value to a
tag and a payload - NULL, Int, Real, Text - where every integer-valued
numeric (every `int`, and a finite integral float inside `-2**63 <= f
< 2**63`) keys as Int, and every other float keys as Real. No `int`
ever meets a `float`. Checked against the oracle with bound values:
`1`/`1.0`, `0`/`0.0`/`-0.0` and `-2**63`/`float(-2**63)` merge;
`2**53 + 1`/`float(2**53)` and `2**63 - 1`/`float(2**63 - 1)` do not;
the first value seen is the one a group shows. No row changes.

This reverses the earlier note in `values.py`'s docstring that a
plain `dict` on the raw value was correct and "no function is needed".
`ORDER BY`, `min`/`max` and `values._compare` still lean on Python's
int/float comparison; that is #154.

2026-10-02 - Numeric text skips exactly space, \t, \n, \v, \f, \r

Issue #136. `_NUMERIC_WHITESPACE` in `exec/expression.py` was `" \t\n\r\f"`, a default nobody had checked, and omitted `\v`, so `'\v12' + 0` was `0` in historian and `12` in SQLite. It is now the six ASCII characters 0x20, 0x09, 0x0A, 0x0B, 0x0C, 0x0D. `tests/oracle.py` (sqlite3 module 3.45.1, string as a quoted literal and as a bound parameter, same results): those six are skipped before the number by every conversion, and after it by the whole-string (affinity) conversion; 0x00-0x08, 0x0E-0x1F including `\x1c`-`\x1f`, 0x7F, `\x85`, `\xa0`, U+1680, U+2003, U+2028, U+3000 and U+FEFF are not (`'\x1c12' + 0` is `0`). Whitespace is not skipped between a sign and its digits, and whitespace-only text is `0` in arithmetic and stays TEXT under affinity. The one constant serves `_scan_number` (arithmetic, unary minus, truthiness, `sum`/`avg` values), `%`'s scan and `_strip_numeric_whitespace` (affinity, `sum`/`avg` classification), so all four change together. It stays a plain string, not `isspace()`/`strip()`, which are Unicode-aware, and stays separate from the lexer's `_WHITESPACE`, which rightly omits `\v`.

This supersedes, by reference, the 2026-09-28 "Not changed: `_NUMERIC_WHITESPACE` omits `\v`" paragraph under issue #106; that paragraph is left as written. Spec §3 now names the six characters.

2026-10-02 - The harness loads a REAL column through a CAST view

Issue #140. Measured with `sqlite3` 3.45.1, values bound, compared by `float.hex()`: a column declared `REAL` stores a bound `-0.0` as `0.0`, an int `5` and a text `'5'` as `5.0`, and a NaN as NULL; a column with no declared type keeps `-0.0`, `5` and `'5'` as given but loses `REAL` affinity (`x = '5'` is `0` for the integer `5`); a typeless raw table behind `CREATE VIEW t AS SELECT CAST(x AS REAL) AS x FROM raw` keeps `-0.0`, compares with `REAL` affinity, turns `5` and `'5'` into `5.0` as a `REAL` column would, and `PRAGMA table_info` reports the view column as `REAL`. The view was chosen over no affinity because no affinity trades the `-0.0` false mismatch for a comparison one, and over normalising `-0.0` in scans because that is a spec §2 change outside the harness. A schema with no `REAL` column still loads into a plain table, a scanned int in a `REAL` column is reported as a mismatch, and a scanned NaN fails the load, since SQLite would store it as NULL. Spec §4 step 2 says so.

2026-10-02 - bind() split along the resolution order, binder.py into four modules

Issue #151. `bind()` was 235 lines and `sql/binder.py` 1519, after #112 and #115 had settled what was left in them. `bind()` is now one plain function per step of the documented resolution order (steps 1 to 10, with step 10, historian's own rejections, as three functions), so reading it against the "Resolution order" docstring is a one-to-one check, and a later change to one step touches one function. The order, the errors and the bound tree are unchanged: the `repr` of every bound tree and the type, message, position and `available` of every `BindError` over the `test_binder.py` and `test_error_order.py` corpus were identical on `c83dd89` and after. The modules are layered so a cycle is impossible by construction, each importing only those before it: `sql/bound.py` (`BindError` and the bound types), `sql/bind_expr.py` (name resolution, single-expression binding), `sql/bind_clauses.py` (per-clause binding), `sql/grouped.py` (the grouped and DISTINCT narrowing checks), `sql/binder.py` (`bind()` and the phases). Private names moved with their code and are not re-exported: `_ordinal_value` is now `sql.bind_clauses._ordinal_value` and `_matches_any_key` is `sql.grouped._matches_any_key`, and the two tests that imported them follow. The public names (`BindError`, `BoundColumnRef`, `BoundOrderByItem`, `BoundSelectItem`, `BoundSelectStatement`, `bind`) are still importable from `sql.binder`. The module docstring moved section by section to the module that implements each; the long HAVING, ORDER BY and DISTINCT narrowing comments moved to `sql/grouped.py`. No prose was rewritten (#116) and the long helpers stay long (#162).

2026-10-02 - No binder function is longer than 50 lines, enforced by a test

Issue #162, the follow-up #151 deferred. A function in `sql/bind_expr.py`, `sql/bind_clauses.py`, `sql/grouped.py` or `sql/binder.py` is at most 50 lines, measured as `end_lineno - lineno + 1` of its `def` so comments and the docstring count, and `tests/test_function_length.py` fails when one is longer. `_bind_expr` (82), `_resolve_name` (55), `_bind_group_by` (55) and `_check_limit_offset_names` (52) were split into plain module-level helpers in the same modules, each taking its inputs as arguments: `_rebuild_with_bound_operands`, `_bind_leaf` and `_start_function_call` for the node-kind branches of `_bind_expr`, which is still an explicit-stack loop (#107), then `_reject_aliased_aggregate`, `_walk_limit_offset_expr` and `_reject_aggregate_keys`. Comments moved with their code and no prose was rewritten (#116). Nothing observable changed: the `repr` of every bound tree and the type, message, position and `available` of every `BindError` were identical on `853608f` and after, over the statements bound by `test_binder.py`, `test_error_order.py` and `test_walk.py` (5948) and 32,561 generated query strings. The limit is for the four binder modules only; the parser's long functions are not covered.

2026-10-03 - The oracle is pinned: managed CPython 3.12.11, SQLite 3.50.4, exact-match guard

Issue #117. The oracle's SQLite version followed whichever Python `uv run` found first: this container's system Pythons link the OS libsqlite (3.45.1), every uv-managed CPython bundles 3.50.4, and the entries below record 3.51.0 (the CLI), 3.50.4 and 3.45.1 for the same project. It is now one value. `.python-version` is exactly `3.12.11` and `pyproject.toml` has `[tool.uv] python-preference = "only-managed"` (the SQLite version follows the Python build, not its version, and a system 3.12.3 exists here, so a minor-version pin or the default preference would not do); `requires-python` stays `>=3.11`. `tests/conftest.py` holds `EXPECTED_ORACLE_SQLITE_VERSION = "3.50.4"` and a `pytest_sessionstart` hook raises `pytest.UsageError` on any other `sqlite3.sqlite_version`, naming the expected and running versions, `sys.version` and `sys.executable`; there is no bypass, and every pytest header prints `oracle: sqlite3 <version> (python <version>)`. Exact match rather than a floor because the version-drift policy ("re-run on version drift") was a promise, and an exact check makes it mechanical: a different SQLite is a different oracle, and which one is a decision, not an accident of a machine. Checked by three mutants: the constant set to 3.99.0, `--python /usr/bin/python3.12` (3.45.1), and `.python-version` plus `[tool.uv]` removed (falls back to system 3.11.15): each aborts before any test runs.

The suite had been written and measured on 3.45.1 and was not neutral. On 3.50.4 the same five cases the issue listed still failed, and were re-measured with `tests/oracle.py`. (1) `-(+0.0)`: 3.50.4 gives `-0.0`, 3.45.1 `0.0`; unary plus between `-` and a literal no longer stops the fold, the same for `-(+9223372036854775808)`, which is INTEGER INT64_MIN on 3.50.4 and a REAL on 3.45.1. Historian disagreed, so `exec/expression.py` now looks through `+` for the literal fold (a few lines), and the pinned expectations in `test_blame.py` and `test_expression.py` became `-0.0`. This also made `test_unary_minus_zero_sign_on_every_row[-(+0.0)]` pass. (2) and (3) `(path LIKE 'a' ESCAPE 'ab') IN ()` and `NOT IN ()` in a select list: 3.50.4 raises, because in value context (select list, `ORDER BY`/`GROUP BY` key, operand of `+`, `=`, `IS`, `AND` and so on) `x IN ()` evaluates `x`; in the condition context of `WHERE` it still does not. Historian disagreed, so an empty `IN` in value context now evaluates and discards its left side (one new work step), and the two expectations in `test_evaluation_order.py` changed from "rows" to "error". `tests/test_expression.py` was split to pin both contexts. (4) The `in-like-chain` expression-height boundary moved from 998 to 997: on the pinned oracle (3.45.1 did not), a deterministic scalar function whose arguments are all constant is constant at parse time, so `x IN (<that>)` is rewritten to `x = +<that>` and costs a level. Historian now treats `LIKE` over constant operands as constant in `_node_height`; other deterministic functions (`abs(1)`, `min(1, 2)`) are constant too but are not modelled, since the binder rejects every scalar function call anyway. Measured on the oracle: `abs(1)`, `min(1, 2)` and `'a' LIKE 'a'` chains stop at 997, `count(*)`, `count(1)`, `max(1)` and column arguments are unchanged.

Found and not fixed, outside this issue: on 3.50.4 a `HAVING` root `x IN ()` also evaluates `x` (`GROUP BY path HAVING (path LIKE 'a' ESCAPE 'ab') IN ()` raises), where `WHERE` does not; historian treats `WHERE` and `HAVING` alike and returns rows. No existing test covers it.

Every earlier "confirmed against 3.45.1" entry remains true as measured, on 3.45.1. None of them was re-run except through the five cases above and the whole suite, which passes unchanged on 3.50.4 apart from those; the rest of the suite is evidence, not a re-measurement of each entry, and the spec §3 binding-error order paragraph, which said "measured on 3.45.1", was checked by `tests/differential/test_error_order.py` running live against 3.50.4 and now says "measured against the oracle". The `sqlite3AtoF` rounding gap (2026-09-28) is platform-specific, was not re-measured on other platforms, and is not changed here.

2026-10-06 - Text becomes a REAL by SQLite 3.50.4's sqlite3AtoF, not by correct rounding

Issue #134. Python's `float(text)` is correctly rounded; SQLite's `sqlite3AtoF` is not, so long numerals and large or small exponents sometimes came out one ULP from the oracle (`'18823239210196293635' * 1.0` is `0x1.05399454f5f45p+64` in SQLite, `...f46p+64` from `float()`), and `2.4703282292062328e-324` is `0.0` in SQLite, not the smallest subnormal. SQLite is right by definition, so `src/historian/atof.py`'s `text_to_real` now ports `sqlite3AtoF` and is the one conversion wherever SQL-derived text becomes a REAL.

Scope: TEXT conversion and the lexer's literals in one task. `_scan_number` (both of its `float(number_text)` calls, which serve TEXT operands, affinity and `sum`/`avg`), `sql/parser.py`'s `_int_literal_value` past int64 and the REAL literal all call it. Splitting literals from TEXT would have left historian disagreeing with itself. In-range integer text and literals still become an exact `int`; only the REAL path changed. Exponent and hex literals are #6, which must call the same function.

One model, named: the oracle pinned by #117 (SQLite 3.50.4, CPython 3.12.11) on macOS arm64. The port follows the 3.50.4 source, `sqlite3AtoF` and `dekkerMul2` in `util.c` as published in the amalgamation (read, not vendored): read up to about 19 significant digits into a u64 (later digits only shift the exponent), clamp the exponent at 10000, fold a positive exponent into the significand while it fits and strip trailing zeros for a negative one, split the u64 into a double and its integer rounding error, then scale that double-double by 10^100, 10^10 and 10 (or their reciprocals, each with a correction term) with Dekker multiplication, and add the two halves. Unlike 3.45.1 (whose x86_64 build used an 80-bit `long double` path, per the 2026-09-28 entry), 3.50.4's `sqlite3AtoF` uses no `long double` at all, only binary64 operations made `volatile` so each is rounded to double. The port is that, plain Python floats and integers in the C's order of operations, with `struct` standing in for the C's `memcpy` bit mask. A straight transcription with no fused multiply-add matched the oracle on the first run; nothing had to be tuned.

Measured against the oracle, bound TEXT through `SELECT ? * 1.0`, hex compared: 34,000 seeded samples in `tests/test_atof.py`, 0 mismatches. They are 2000 for each of the nine generators of #134's measurement table, 2000 each for a mixed family, a subnormal family (exponent -325 to -300) and an exponent long enough to be clamped, and 10,000 at the u64 edges the C tests (`(2**64 - 10) // 10`, `(2**64 - 0x800) // 10`, `18446744073709549568`). Python's `float()` on the same texts differs from the oracle in 157 of 2000 digit runs, 296 at exponent 200 to 300, 756 at -300 to -200, 560 in the subnormal family, 1 and 2 for 25 and 40 decimal digits, 0 for the other table rows, and in all 2000 clamped-exponent texts, where SQLite's clamp keeps `0.<9990 zeros>123e9999999` finite (about `1.23e9`) and `float()` gives `inf`. Every pinned vector in #134 matches. The SQL-literal path gives the same double as bound TEXT, as the issue measured.

The platform and version dependence is real: #105's engineer measured 3.45.1 on x86_64 taking a `long double` path with about a hundredth of these mismatches. 3.50.4 has no such path in the source, so its answer may well be the same on other IEEE-double platforms, but a compiler is free to fuse `a*b + c` into one rounding, which would change it, and only measurement can say (#176). So `tests/differential/atof_gate.py` asks the oracle for every pinned vector once per session: if any differs, the oracle-facing cases (the #134 differential cases and the sampling test) skip with a reason naming the platform and #176; they are still collected, so the differential count does not change. historian itself does not change by platform: it always follows this macOS arm64 model, and choosing a conversion at import time was rejected because historian's answers would then depend on the machine. A separate test asserts the gate is open on macOS arm64, and the unit-tested vectors are never gated.

Mutants: replacing the body with `float(text)` fails the vector test, the sampling test (nine of the thirteen families) and the #134 differential cases. A one-ULP change to a power of ten, a 25- or 27-bit split, dropping any product from the Dekker step, not renormalising, a zero initial error term, skipping the exponent fold, the zero stripping, the 10^100 loop or the 10^-10 loop, or a correction term set to zero or off by a power of ten, each fail the sampling test (the 10^-10 loop only in the subnormal and u64-edge families). The u64 and exponent limits off by one, or `<` for `<=` in their comparisons, are seen only by the u64-edge and clamped-exponent families, which exist for that. Not killed, and not killable by sampling: a one-ULP change to one of the four correction terms, or regrouping `x0*yy + x1*y + cc`, moves the double-double by about 2^-106 of the value, which changes the rounded result with a probability around 2^-50 per sample (0 of 300,000 random 16-to-26-digit texts for each of the eight one-ULP changes and the regrouping); changing the last printed digit of `-1.5902891109759918046e83` gives the same double.

2026-10-06 - Replicate SQLite's HAVING-to-WHERE move

Issue #141. The owner chose "Replicate" on the decision card
"Document SQLite's HAVING/WHERE rewrites as accepted divergence, or
replicate them?" (2026-10-03). The other option was to record the
difference next to constant folding (#51); it is rejected because the
difference is an error versus no error on a query a user can write,
and §1 makes SQLite the definition of correct. #142 (constant
propagation in `WHERE`) was decided the same way and is its own issue.

Grooming measured the rule on 3.45.1 and 3.50.4; the implementation
re-measured every answer the issue quotes on the oracle #117 pinned
(Python `sqlite3` 3.50.4), with `tests/oracle.py` and the
differential harness's loader over `tiny`, and each is pinned as a
test's expected outcome in `tests/differential/test_having_hoist.py`.
SQLite moves a term of `HAVING` into `WHERE` only when the query has a
`GROUP BY` (`SELECT count(*) FROM blame HAVING count(*) > 100 AND 'a'
LIKE 'a' ESCAPE 'ab'` returns no rows and does not raise). `HAVING` is
split on every `AND`, nested ones included (`count(*) > 5 AND
(count(*) > 1 AND ERR)` and `(count(*) > 5 AND ERR) AND count(*) > 1`
both raise), and an `OR` or `NOT (...)` is one term. A term moves when
it has no aggregate and every column in it is a `GROUP BY` key or
inside a key's expression (`GROUP BY path || 'x'` moves `(path || 'x')
|| 'y' LIKE ...`; an alias of a key, an ordinal key and a two-key
`GROUP BY` behave the same), or when it has no column at all. A term
with an aggregate stays put, `OR` branches included: `count(*) > 5 AND
(ERR OR count(*) > 1)` returns no rows. A `DISTINCT`, an `ORDER BY`
and a `LIMIT 0` change nothing. Moved terms run after the query's own
`WHERE` terms and in `HAVING` order: `WHERE ERR GROUP BY path HAVING
path > 'zzzz'` raises, `WHERE line_no > 5 GROUP BY path HAVING ERR`
does not, `HAVING count(*) > 5 AND ERR AND path > 'zzzz'` raises and
`HAVING count(*) > 5 AND path > 'zzzz' AND ERR` does not.

One answer differs from the groomed rule, and SQLite is followed: on
3.50.4 a term that is an integer literal `0` does not move. `GROUP BY
path HAVING 0 AND ERR` raises, as do `(0) AND ERR`, `00 AND ERR`,
`count(*) > 5 AND 0 AND ERR` and `ERR AND 0`, so the `0` stayed in
`HAVING` and `ERR` alone moved. Every other constant moves and, being
false and in front, stops `ERR` per row: `0.0 AND ERR`, `-0 AND ERR`,
`NULL AND ERR`, `1 > 2 AND ERR`, `1 = 0 AND ERR`, `'0' AND ERR` and
`NOT 1 AND ERR` all return no rows. That matches
SQLite's source, where `havingToWhereExprCb` skips a term with
`ExprAlwaysFalse`, a flag the parser sets on an integer literal whose
value is 0 (and on `FALSE`, which v1 does not have). The literal `0`
was the version-dependent case grooming kept out of the tests; with
the oracle pinned it is deterministic, so the planner keeps a
`Literal` of `int` value `0` in `HAVING` and the tests pin it. Not
replicated: SQLite's parser also folds `x AND 0` to `0` when neither
side calls a function, so `HAVING path > 'zzzz' AND 0 AND ERR` raises
in SQLite (the folded `0` stays, `ERR` moves) and returns no rows in
historian. That is parse-time constant folding, the difference #51
already accepted, and no test pins it. Measured too, and recorded on
#141 for #171: on 3.50.4 `SELECT path FROM blame WHERE ERR AND 0` and
`... WHERE 0 AND ERR` both return no rows (the constant is evaluated
before any row), which is not the "raises on 3.50.4" #171 records.

A moved term changes rows, not only errors, when the rows of one group
differ in a way the key hides: over rows with `line` equal to `'1'`,
`'1.0'`, `'1'` and `line_no` 1, 2, 3, `SELECT count(*), sum(line_no)
FROM blame GROUP BY line + 0 HAVING (line + 0) || 'x' = '1x'` is `(2,
4)` in SQLite and was `(3, 6)` in historian, and with `'1.0x'` it is
`(1, 2)` against no rows. The issue went on to say a copy of the
moved term left in `HAVING` would also change the answer; it would
not. The group's key comes from a row that passed the moved term, and
the term reads only the key, so the copy is always `TRUE`. The
planner leaves no copy because SQLite leaves none (it replaces the term
with `1`) and a copy is wasted work; the mutant that keeps one is
caught by the plan-shape tests, not by the rows test.

Design: the rewrite is in `plan()`, not the optimizer, because it is
needed for correctness and `--no-pushdown` must still do it. The
moved terms, bound as written (offsets into the scan row), are joined
into one left-deep `And` in `HAVING` order and become one `Filter`
between the `WHERE` `Filter` (or the `Scan`) and the `Aggregate`. Two
stacked `Filter`s evaluate per row in the order of one `AND` chain, so
"after `WHERE`, in `HAVING` order" is position in the tree and in the
chain. The kept terms are joined the same way and split into
aggregate slots as before; a moved term has no aggregate, so no slot
moves, and a `HAVING` from which nothing moves is planned exactly as
before. `split_conjuncts` moved to `sql/walk.py`, shared by the
planner and the optimizer, beside `join_conjuncts` and
`references_only_keys`; all three are explicit-stack loops (#107).
The moved `Filter` is not offered to the scan, because a pushed term
can hide an error raised by an earlier one, and `HAVING ERR AND path
LIKE 'zzz%'` agrees with SQLite only while it is not pushed (#172). It
is marked by an explicit `negotiable=False` on `Filter`, which
`optimize()` reads, rather than inferred from position: with no
`WHERE` it is the operator directly above the `Scan`. "The `Filter`
is never removed" still holds: no pushed term is involved, and every
term lives in exactly one `Filter`.

Left out, each with its own issue: a column-free term is evaluated by
SQLite once, before any row, which historian does not do for `WHERE`
either (#171), so a trailing constant such as `HAVING ERR AND NULL`
still differs; pushing moved terms down (#172). Terms of the form
`column = constant` are kept out of the new tests because SQLite
propagates them (#142).
