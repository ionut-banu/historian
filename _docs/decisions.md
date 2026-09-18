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
