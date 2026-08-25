Orchestrator

The main session is the orchestrator. It launches the PM, the engineer
and QA as subagents. It does not groom, implement or test itself.

Lifecycle

1. Pick the next open issue from the backlog
2. PM grooms it
3. Engineer implements it
4. QA verifies it
5. On FAIL, back to step 3 with the QA comment as input
6. On PASS, close the issue
7. Repeat until the backlog is empty

Rules

- Do not skip step 2
- The engineer does not close the issue
- QA does not fix the code, only outputs PASS or FAIL
- The orchestrator closes the issue only after QA outputs PASS
- The orchestrator never fixes the code itself. Fixing in the main
  session skips QA entirely and is how unverified work gets closed.

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

Cheapest is not the same as fastest. A weak model on a task beyond it
takes several times the turns and costs more than the right one would
have. Mid-tier is the floor, not the target.

The oracle

Correctness in this project is decided by SQLite, not by opinion. Every
supported query runs through historian and through SQLite over the same
data, and the results must be identical. See §1 of `_docs/spec.md`.

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
