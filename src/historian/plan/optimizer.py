"""Pushdown negotiation: the optimizer stage of the pipeline
(`_docs/spec.md` §3 - "optimizer  operator tree, with predicates
pushed into scans"). Issue #121.

`optimize(tree)` takes the tree `plan()` built and performs spec §3's
"Pushdown negotiation", its four steps and nothing else:

1. Split the `WHERE` predicate - the `Filter` directly above the
   `Scan`, when it is negotiable - on top-level `AND` into conjunctive
   terms, left to right (`split_conjuncts`, `sql/walk.py`). `OR` is
   never split; `NOT (x AND y)` is one term too, since its `And` is not
   at the top. The `Filter` of `HAVING` terms that moved below the
   aggregate (#141) is marked not negotiable (`Filter.negotiable()`)
   and is never split or offered, even when there is no `WHERE` and it
   sits directly above the `Scan` (#172).
2. Offer each term to the scan, in that order, by calling
   `source.accepts(term)` - unless `source.capabilities()` is empty,
   in which case nothing is offered at all.
3. Record the accepted terms, in offered order, on the `Scan`
   (`Scan.set_pushed`), which passes them to `source.scan(pushed=...)`.
4. Leave the `Filter` in place, unchanged, with every term still in
   it. This module never builds, replaces or removes a `Filter`; the
   predicate object it reads is the one it leaves (spec §2,
   `_docs/decisions.md` 2026-08-24).

A term is the bound AST subexpression itself (`exec/operators.py`'s
`Predicate = Expr`), so a scan recognises what it can use with the
same explicit `isinstance` checks the evaluator uses, and pushed terms
carry `BoundColumnRef` offsets into the scan's own schema.

The step is separate from `plan()` on purpose: `cli.py` calls it
between planning and execution, `--no-pushdown` (#43) is "do not call
it", and `--explain`/`--stats` (#42) read the outcome back from
`Scan.pushed()` and the scan's own records. It rewrites the one `Scan`
in place and returns the same root: there is still one operator tree
(spec §3, "One plan representation, not two"). Row order cannot
change - only what `Scan` hands its source does, and the `Filter`
above it is the same single in-order pass either way.

Plain Python: no git, no I/O. Explicit `isinstance` checks and loops,
no dispatch tables (AGENTS.md), so it ports to Rust as a `match`.
"""

from __future__ import annotations

from historian.exec.operators import Filter, Operator, Predicate, Scan, child_of
from historian.sql.walk import split_conjuncts

# `split_conjuncts` lives in `sql/walk.py` (#141), shared with the
# planner's `HAVING` split, and is re-exported here.
__all__ = ["optimize", "split_conjuncts"]


def optimize(tree: Operator) -> Operator:
    """Negotiate pushdown for *tree*'s one `Scan`; return *tree*.

    Walks the operator chain down to its `Scan`. If the operator
    directly above that `Scan` is a negotiable `Filter` - the `WHERE`
    filter `plan()` puts there - and the scan declares at least one
    capability, each of that filter's conjunctive terms is offered to
    the scan's source in order, and the accepted ones become the
    `Scan`'s pushed terms. Otherwise (no `WHERE`, a scan that can push
    nothing, or only the `Filter` of moved `HAVING` terms above the
    scan) the `Scan` is left pushing nothing.

    A `Filter` anywhere else is never negotiated: `HAVING`, above
    `Aggregate`, ranges over aggregate output rows, and the moved
    `HAVING` terms (#141), above the `WHERE` `Filter` or marked not
    negotiable when directly above the `Scan`, are kept out of
    pushdown on purpose (#172).
    """
    parent: Operator | None = None
    node: Operator = tree
    while not isinstance(node, Scan):
        child = child_of(node)
        if child is None:
            raise AssertionError(f"plan/optimizer.py: {type(node).__name__} has no child and is not a Scan")
        parent = node
        node = child
    scan = node

    # Negotiation always starts from nothing pushed, so running this
    # twice over one tree gives the same result as running it once.
    scan.set_pushed(())
    if not isinstance(parent, Filter) or not parent.negotiable():
        return tree
    source = scan.source()
    if not source.capabilities():
        return tree

    accepted: list[Predicate] = []
    for term in split_conjuncts(parent.predicate()):
        if source.accepts(term):
            accepted.append(term)
    scan.set_pushed(accepted)
    return tree
