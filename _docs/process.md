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
