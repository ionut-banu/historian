Orchestrator

The main session is the orchestrator. It launches the PM, the engineer
and QA as subagents. It does not groom, implement or test itself.

Lifecycle

1. Pick the next open issue from the backlog
2. PM grooms it
3. Engineer implements it on a branch
4. QA verifies it on that branch
5. On FAIL, back to step 3 with the QA comment as input
6. On PASS, push the branch and open a pull request into `main`
7. Stop. A human reviews and merges it, and the merge closes the issue
8. Repeat until the backlog is empty

Rules

- Do not skip step 2
- The engineer does not close the issue
- QA does not fix the code, only outputs PASS or FAIL
- Nobody closes an issue by hand. The pull request says `Closes #N`
  and merging it does the closing, so an issue can never be closed
  while its code is still unmerged.
- Nobody merges the pull request except a human. QA passing means the
  work is ready to be looked at, not that it is ready to land.
- The orchestrator never fixes the code itself. Fixing in the main
  session skips QA entirely and is how unverified work gets closed.

Labels and milestones

Every issue carries a milestone, one `kind:` label, and one `area:`
label. The PM applies them while grooming; an issue that reaches the
engineer without them was not groomed.

- `kind:` - `feature`, `bug`, `chore`, `process`. What sort of work it is.
- `area:` - `values`, `sql`, `plan`, `exec`, `tables`, `cli`, `tests`,
  `docs`. Matches the module layout in §3 of the spec, so the labels
  stay true as the code grows rather than drifting into their own
  vocabulary.
- `blocked` - waiting on another issue. Say which one in a comment.

Three more record where an issue came from, and only the orchestrator
applies them:

- `from: fuzzer` - a mismatch against SQLite that needs more than a
  regression test
- `from: review` - a finding from the milestone reviewer
- `from: qa` - something QA found that was outside the issue it was
  testing

Those three matter more than they look. Most issues here come from the
spec, which was written before any code existed. The ones that came
from the machinery finding real problems are the evidence the process
works, and they are worth being able to list.

A pull request carries the same labels and milestone as the issue it
closes.

Writing for GitHub

Do not hard-wrap anything that goes into an issue body, a pull request
body, or a comment. GitHub renders a single newline in those as an
actual line break rather than reflowing the paragraph, so text wrapped
at 70 columns comes out as a column of ragged short lines.

One paragraph is one line, however long. One list item is one line,
however long. Let the browser wrap it. Fenced code blocks are
unaffected - wrap those however the code reads best.

This is the opposite of the convention for files in `_docs/`, which are
read as text and stay wrapped at about 70 columns. The difference is
where it will be rendered, not who wrote it.

One worktree per subagent

A branch is not enough isolation. The orchestrator works on `main` in
the main directory while a subagent works on a branch, and there is only
one working directory - so `git checkout` from either side changes the
files under the other.

This is not hypothetical. On issue 2 it happened twice in one minute:
the orchestrator switched to `main` while QA was mid-review and the
module under test vanished, and QA switched back while the orchestrator
was committing, so a documentation commit landed on the feature branch
and a push to `main` silently pushed nothing.

So each implementing subagent gets its own directory:

    git worktree add ../historian-issue-7 -b issue-7-blame-scan

The engineer and QA work there. The orchestrator stays in the main
directory on `main` and never checks out a feature branch. Remove it
when the pull request merges:

    git worktree remove ../historian-issue-7

Neither side can then disturb the other, and no amount of care is
required to keep it that way.

A worktree carries a second hazard that a directory does not fix:
a bare `python` or `python3` inside it can silently run the main
checkout's code. The subagent's shell inherits the orchestrator's
`PATH`, and the main checkout's `.venv/bin` is first on it no
matter which directory the shell is in. That `.venv`'s editable
install of `historian` has a `.pth` file hardcoding an absolute
path to the main checkout's `src/`, so a bare interpreter imports
the orchestrator's code regardless of where it is run from -
`VIRTUAL_ENV` travels alongside `PATH` but is not what causes
this, and unsetting it would not help. `uv run` does not have the
problem: it re-resolves against the worktree's own
`pyproject.toml` and warns when it overrides a mismatched
`VIRTUAL_ENV`.

This cannot be fixed once at `git worktree add` time the way the
`git checkout` hazard above was. A directory persists across every
command a subagent runs, which is why making one per subagent
holds for the whole session. An environment variable does not -
shell state does not survive between one invocation and the next,
only the working directory does - so there is no single moment to
unset `PATH` or `VIRTUAL_ENV` that would stay fixed. The rule has
to hold at each invocation instead: every ad hoc probe run inside
a worktree uses `uv run` - `uv run python3 -c "..."`, matching the
existing `uv run pytest` / `uv run historian` convention - never a
bare `python` or `python3`.

Run this once on entering a worktree, before anything else, and
confirm the path it prints is under that worktree's own directory
rather than the main checkout's:

    uv run python3 -c "import historian; print(historian.__file__)"

This is not hypothetical either. On issue #25 the entire
observable change was one error-message string. QA's probe with a
bare interpreter showed the old string - main's, not the branch's
- and the only reason a wrong PASS did not ship is that QA
happened to compare `historian.__file__` and noticed the mismatch.
Nothing required that comparison at the time. A change to actual
query results, rather than to a string, would look plausible
instead of obviously stale, with no equivalent signal to catch it
by luck a second time.

Branches and pull requests

One branch per issue, named for it - `issue-7-blame-scan`. The engineer
creates it, commits to it, and never touches `main`.

The pull request is opened by the orchestrator after QA passes, not by
the engineer. It carries `Closes #N`, a link to the QA verdict comment,
the test result, and anything known-broken and deliberately deferred.
It is the one place a human sees the whole change at once, so it is
written for a reader who has not followed the issue.

Step 7 is a real stop. The loop does not continue to the next issue
while a pull request is waiting, unless the next issue is independent
of the one under review - and in this backlog most are not, because
each milestone builds on the last.

Progress lives in the issues

Not in this session, and not in a checklist file. An issue is done when
it is closed, and what happened to it is in its comments. If this
session is lost or compacted, the backlog is still exactly where it
was, so re-read the issues rather than trusting recollection about what
was finished.

Do not add a plan file with checkboxes. It would be a third copy of
work already described by §6 of the spec and tracked by the issues, and
the copy that is nobody's job to update is the one that goes stale
while still looking authoritative. Progress within a single task is the
engineer's own business and disappears with it.

Review at milestone boundaries

When a milestone's last issue closes, dispatch the reviewer over the
whole milestone's diff before starting the next one.

It is a separate role from QA on purpose. QA is deliberately blind to
the implementation - it checks behaviour against the acceptance
criteria and is told to ignore what the code claims about itself. That
blindness is what makes it hard to fool, and reading the code for
quality would destroy it. So the two jobs stay apart, and the reading
one runs once per milestone rather than once per issue.

Its findings become issues. Nobody fixes them in place.

The loop has a ceiling

Step 5 is bounded at three rounds. Each engineer subagent is fresh, so
the issue thread is its memory - every dispatch reads the prior QA
comments and the engineer's own replies.

- Rounds 1 and 2: dispatch a fresh engineer with the QA comment
- Round 3: dispatch on a more capable model, saying plainly that two
  attempts already failed and pointing at the thread

After round 3, stop dispatching and decide. An engineer that has
failed three times is usually not the problem:

- **An acceptance criterion is wrong or impossible.** Send it back to
  the PM, fix the issue, restart at round one.
- **The task is too large.** Split it, close this issue as superseded,
  and link the pieces.
- **The spec is wrong.** Fix `_docs/spec.md`, record why in
  `_docs/decisions.md`, restart at round one.
- **It is a real limitation nobody needs solved yet.** File a follow-up
  issue, say so in a comment, and let QA pass what remains.

What is forbidden is a fourth round. Three failures on the same code
means something upstream is wrong, and dispatching again just pays to
discover that more slowly.

Models

Every subagent that does not name a model inherits this session's,
which is the most expensive one. Name a model on every dispatch.

- **PM** - mid-tier. Grooming is judgment about edge cases, not depth.
- **QA** - mid-tier. With an oracle, most of the verdict is arithmetic.
- **Engineer** - mid-tier by default. Use the most capable model for
  work that is design rather than transcription: the value semantics
  (issue 2), the optimizer, the fuzzer and its shrinker.
- **Reviewer** - most capable. It runs rarely, and reading a milestone
  of code for what is wrong with it is the hardest job here.
- **Escalation** - one tier up from whatever just failed.

Each role's full definition - what it does, and what counts as
done - lives in `_docs/team/`: `pm.md`, `software-engineer.md`,
`qa-engineer.md`, `reviewer.md`.

Cheapest is not the same as fastest. A weak model on a task beyond it
takes several times the turns and costs more than the right one would
have. Mid-tier is the floor, not the target.

The oracle

Correctness in this project is decided by SQLite, not by opinion. Every
supported query runs through historian and through SQLite over the same
data, and the results must be identical. See §1 of `_docs/spec.md`.

The oracle is precisely SQLite as exposed by Python's bundled
`sqlite3` module - the same module `tests/differential/conftest.py`
uses to build the harness's SQLite side. Not the system `sqlite3`
CLI. The module's current version, checked with:

    uv run python3 -c "import sqlite3; print(sqlite3.sqlite_version)"

is `3.50.4` (as of 2026-09-25). This can differ from `sqlite3
--version`'s CLI build - `3.51.0`, an Apple-patched build, on the
same machine - and the two are already different versions here.

Reach the module directly for an ad hoc check instead of the CLI.
For a query with no setup:

    uv run python3 -c "import sqlite3; print(sqlite3.connect(':memory:').execute('SELECT ...').fetchone())"

For a check that needs table setup first, use the helper,
`tests/oracle.py` (issue #93), rather than chaining
`executescript()` and `execute()` by hand inside a shell `-c`
argument:

    uv run python tests/oracle.py "<setup SQL>" "<query SQL>"
    uv run python tests/oracle.py "<query SQL>"   # no setup

It runs the setup (if given) through `executescript()` and the
query through `execute()`, and prints each result value - with
every `float` shown as both `repr()` and `.hex()` - plus the
module's `sqlite_version` on the last line.

The CLI is never the oracle for anything recorded as a decision -
not in a comment, a `_docs/decisions.md` entry, or a test. It may
still be run as a human's own casual scratch tool, but nothing
written down as "confirmed against sqlite3" may come from it, for
one concrete reason: it does not compute different values from the
module - a 300-trial exact comparison (`sum(x) = <module repr>`
inside SQL) found 0 mismatches - but its printed output cannot be
trusted to show what it computed. Floats must be compared exactly,
with `float.hex()` or equality inside SQL, never by reading printed
text from either tool. The CLI prints floats at 15 significant
digits by default, and its own `printf('%.20e')` pads with zeros
after about 16 significant digits rather than printing real ones:

    sqlite> select printf('%.20e', -1.8193757715275717e+299);
    -1.81937577152757100000e+299

That string looks like a different double from the module's
`-1.8193757715275717e+299` (`...5710...` vs `...5717...`), but it
is the same literal, padded - a display artifact, not a computation
difference. See `_docs/decisions.md`, 2026-09-25, for the full
evidence.

Version-drift policy: whoever changes the Python toolchain this
project resolves against (`uv`'s Python selection, or
`requires-python` in `pyproject.toml`) updates the recorded module
version above in the same commit - mirroring the rule that a
decision contradicting the spec gets the spec edited alongside it.
The CLI's own version needs no tracking here; it is not the oracle.

Two consequences for the process:

- QA runs the fuzzer with its own budget, larger than the engineer's.
  QA is expected to find failures the engineer's tests did not. A FAIL
  from a generated query is a normal outcome, not an escalation.
- The differential test count never goes down. An issue that reduces
  it is a FAIL regardless of its acceptance criteria.

The spec is binding

`_docs/spec.md` is the source of truth and describes historian as it
currently is. Every issue names the section it implements.

It is the only specification. If you find another document in this
repository describing what historian should do, it is stale. Do not
act on it, do not reconcile it with the spec, and do not average the
two. Say what you found, in a comment on the issue, and use the spec.

`README.md` is the one exception, and it is not a specification. It
describes what exists today, for someone who has just arrived, and
points here for what is planned. Never implement from it. If it
contradicts the spec, the README is wrong and gets fixed - which makes
it the one document that has to be updated when behaviour changes.

Nothing that is no longer true is kept in the working tree. Superseded
specs and plans live in git history, tagged - `git tag -l -n1` lists
them. `_docs/decisions.md` is the one exception, because every entry
is dated and written as a past decision, so reading it cannot be
mistaken for reading current requirements.

The non-goals and the out-of-scope list in §1 are binding. Work that
contradicts them does not get implemented and does not get argued
about in an issue comment - it gets filed as a v2 issue and dropped
from the current one.

Decisions made while building go in `_docs/decisions.md`, newest last,
one short entry each. If a decision contradicts the spec, the spec is
edited in the same commit.
