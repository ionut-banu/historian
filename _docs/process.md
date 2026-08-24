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
data, and the results must be identical. See §1 of the design spec.

Two consequences for the process:

- QA runs the fuzzer with its own budget, larger than the engineer's.
  QA is expected to find failures the engineer's tests did not. A FAIL
  from a generated query is a normal outcome, not an escalation.
- The differential test count never goes down. An issue that reduces
  it is a FAIL regardless of its acceptance criteria.

The spec is binding

`docs/superpowers/specs/2026-08-24-historian-design.md` is the source
of truth. Every issue names the section it implements.

The non-goals and the out-of-scope list in §1 are binding. Work that
contradicts them does not get implemented and does not get argued
about in an issue comment - it gets filed as a v2 issue and dropped
from the current one.

Decisions made while building go in `_docs/decisions.md`, newest last,
one short entry each. If a decision contradicts the spec, the spec is
edited in the same commit.
