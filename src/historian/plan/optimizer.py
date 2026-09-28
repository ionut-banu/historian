"""Pushdown negotiation: the optimizer stage of the pipeline
(`_docs/spec.md` §3 - "optimizer  operator tree, with predicates
pushed into scans"). Issue #121.

`optimize(tree)` takes the tree `plan()` built and performs spec §3's
"Pushdown negotiation", its four steps and nothing else:

1. Split the `WHERE` predicate - the `Filter` directly above the
   `Scan` - on top-level `AND` into conjunctive terms, left to right
   (`split_conjuncts`). `OR` is never split; `NOT (x AND y)` is one
   term too, since its `And` is not at the top.
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
from historian.sql.ast import And, Expr

__all__ = ["optimize", "split_conjuncts"]


def split_conjuncts(expr: Expr) -> list[Predicate]:
    """The conjunctive terms of *expr*, left to right.

    Every top-level `And` is split; anything else, `Or` included, is
    one term. Parens produce no AST node, so `(x AND y) AND z` and
    `x AND (y AND z)` both give `[x, y, z]` - `AND` is associative,
    and a term's own evaluation is unchanged by where it came from.

    Iterative with an explicit stack rather than recursive: the parser
    builds `x1 AND x2 AND ... AND xn` as a left-deep chain, and its
    depth must not be bounded by Python's recursion limit here.
    """
    terms: list[Predicate] = []
    stack: list[Expr] = [expr]
    while stack:
        node = stack.pop()
        if isinstance(node, And):
            # Right pushed first so left is popped - and emitted - first.
            stack.append(node.right)
            stack.append(node.left)
        else:
            terms.append(node)
    return terms


def optimize(tree: Operator) -> Operator:
    """Negotiate pushdown for *tree*'s one `Scan`; return *tree*.

    Walks the operator chain down to its `Scan`. If the operator
    directly above that `Scan` is a `Filter` - the `WHERE` filter
    `plan()` puts there - and the scan declares at least one
    capability, each of that filter's conjunctive terms is offered to
    the scan's source in order, and the accepted ones become the
    `Scan`'s pushed terms. Otherwise (no `WHERE`, or a scan that can
    push nothing) the `Scan` is left pushing nothing.

    A `Filter` anywhere else - `HAVING`, above `Aggregate` - is never
    negotiated: its terms range over aggregate output rows, not scan
    rows.
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
    if not isinstance(parent, Filter):
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
