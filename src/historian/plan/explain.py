"""The operator-tree and expression printer behind `--explain`
(issue #42, `_docs/spec.md` §5).

`format_plan(tree)` renders the tree root first, one operator per
line, each child indented two spaces under its parent. Every operator
is printed, `Project` included. The one line that is not derived from
the tree alone is the `Scan` line: its name and "n of total" come from
the source, through `ScanSource.estimate()`, so this module knows no
table and imports no git.

Operators above an `Aggregate` hold expressions over its *output* row:
the planner has rewritten each group key and aggregate call into a
`BoundColumnRef` at that slot (`plan/planner.py`, `_split_expr`). The
printer undoes that for display: a reference into such a slot prints
as the group key's or the call's own text, so the `Project` of
`SELECT author_name, count(*) ...` reads `author_name, count(*)`, not
`group_1, count`.

A column the planner replaced by a propagated constant (#142,
`FixedColumnRef`) prints as the constant, so the `WHERE` `Filter` line
shows the rewritten terms the scan was offered.

`ConstantGuard` (#171) prints its column-free terms joined by `AND`,
spelled as the `Filter` line below it spells them. Nothing here runs
the tree, so the guard is never evaluated.

Expressions print as SQL text. A child is parenthesized when SQLite's
precedence needs it, and always when it is a nested `AND`/`OR`/`NOT`
(other than an `AND` inside an `AND` or an `OR` inside an `OR`, which
are associative and print as a flat chain). The walk is an explicit
stack, never recursion: expressions up to SQLite's depth limit (1000)
parse, and printing one must not hit `RecursionError`.

Plain Python, explicit `isinstance` checks, no dispatch tables
(AGENTS.md).
"""

from __future__ import annotations

from historian.exec.operators import (
    Aggregate,
    ConstantGuard,
    Distinct,
    Filter,
    Limit,
    Operator,
    Project,
    Scan,
    Sort,
    child_of,
)
from historian.sql.ast import (
    And,
    Between,
    BinaryOp,
    ColumnRef,
    Expr,
    FunctionCall,
    In,
    Is,
    Like,
    Literal,
    Not,
    Operator as BinOp,
    Or,
    Star,
    UnaryOp,
    UnaryOperator,
)
from historian.sql.binder import BoundColumnRef
from historian.sql.walk import FixedColumnRef, join_conjuncts

__all__ = ["format_expr", "format_plan"]

#: Binding strength of each expression shape, loosest first, after
#: SQLite's own table: `OR`, `AND`, `NOT`, then `=`-like comparisons
#: (`=`, `<>`, `IS`, `IN`, `LIKE`, `BETWEEN`), relational comparisons,
#: `+ -`, `* / %`, `||`, unary signs. A leaf or a call is the tightest.
_PREC_OR = 1
_PREC_AND = 2
_PREC_NOT = 3
_PREC_EQUALITY = 4
_PREC_RELATIONAL = 5
_PREC_ADD = 6
_PREC_MUL = 7
_PREC_CONCAT = 8
_PREC_UNARY = 9
_PREC_ATOM = 10

#: What an operand of `IS`/`LIKE`/`IN`/`BETWEEN` must bind at least as
#: tightly as, to print without parentheses.
_PREC_OPERAND = _PREC_RELATIONAL


def _binary_symbol_and_prec(op: BinOp) -> tuple[str, int]:
    if op == BinOp.ADD:
        return "+", _PREC_ADD
    if op == BinOp.SUB:
        return "-", _PREC_ADD
    if op == BinOp.MUL:
        return "*", _PREC_MUL
    if op == BinOp.DIV:
        return "/", _PREC_MUL
    if op == BinOp.MOD:
        return "%", _PREC_MUL
    if op == BinOp.CONCAT:
        return "||", _PREC_CONCAT
    if op == BinOp.EQ:
        return "=", _PREC_EQUALITY
    if op == BinOp.NE:
        return "<>", _PREC_EQUALITY
    if op == BinOp.LT:
        return "<", _PREC_RELATIONAL
    if op == BinOp.LE:
        return "<=", _PREC_RELATIONAL
    if op == BinOp.GT:
        return ">", _PREC_RELATIONAL
    if op == BinOp.GE:
        return ">=", _PREC_RELATIONAL
    raise AssertionError(f"plan/explain.py: unhandled binary operator {op}")


def _literal_text(value: object) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, str):
        return "'" + value.replace("'", "''") + "'"
    if isinstance(value, float):
        if value != value:
            return "NULL"
        if value == float("inf"):
            return "1e999"
        if value == float("-inf"):
            return "-1e999"
        return repr(value)
    return str(value)


def _is_logical(expr: Expr) -> bool:
    return isinstance(expr, (And, Or, Not))


#: One pending piece of output: literal text, or an expression still to
#: print with the tightest binding it needs (`need`), the kind of its
#: parent when that is `AND`/`OR`/`NOT` ("and"/"or"/"not"), and the
#: aggregate slot expressions its column references resolve through.
_Piece = str | tuple[Expr, int, str | None, "tuple[Expr, ...] | None"]


def _format(expr: Expr, slots: tuple[Expr, ...] | None) -> str:
    out: list[str] = []
    stack: list[_Piece] = [(expr, 0, None, slots)]
    while stack:
        piece = stack.pop()
        if isinstance(piece, str):
            out.append(piece)
            continue
        node, need, parent, node_slots = piece
        if isinstance(node, BoundColumnRef) and node_slots is not None and node.offset < len(node_slots):
            # The slot's expression is over the aggregate's *input*
            # columns, so its own references resolve through nothing.
            node = node_slots[node.offset]
            node_slots = None

        parts: list[_Piece] = []
        prec = _PREC_ATOM
        if isinstance(node, Literal):
            parts = [_literal_text(node.value)]
        elif isinstance(node, BoundColumnRef):
            parts = [node.name]
        elif isinstance(node, FixedColumnRef):
            # A column replaced by a propagated constant (#142) prints as
            # that constant, already converted by the column's affinity.
            parts = [_literal_text(node.value)]
        elif isinstance(node, ColumnRef):
            parts = [node.name if node.table is None else f"{node.table}.{node.name}"]
        elif isinstance(node, Star):
            parts = ["*" if node.table is None else f"{node.table}.*"]
        elif isinstance(node, FunctionCall):
            parts = [node.name + "(" + ("DISTINCT " if node.distinct else "")]
            for index, arg in enumerate(node.args):
                if index > 0:
                    parts.append(", ")
                parts.append((arg, 0, None, node_slots))
            parts.append(")")
        elif isinstance(node, UnaryOp):
            prec = _PREC_UNARY
            sign = "-" if node.op == UnaryOperator.NEG else "+"
            # Tighter than unary itself, so `- -x` never prints as `--x`
            # (a comment in SQL): a nested unary is parenthesized.
            parts = [sign, (node.operand, _PREC_ATOM, None, node_slots)]
        elif isinstance(node, BinaryOp):
            symbol, prec = _binary_symbol_and_prec(node.op)
            parts = [(node.left, prec, None, node_slots), f" {symbol} ", (node.right, prec + 1, None, node_slots)]
        elif isinstance(node, And):
            prec = _PREC_AND
            parts = [(node.left, prec, "and", node_slots), " AND ", (node.right, prec, "and", node_slots)]
        elif isinstance(node, Or):
            prec = _PREC_OR
            parts = [(node.left, prec, "or", node_slots), " OR ", (node.right, prec, "or", node_slots)]
        elif isinstance(node, Not):
            prec = _PREC_NOT
            parts = ["NOT ", (node.operand, prec, "not", node_slots)]
        elif isinstance(node, Is):
            prec = _PREC_EQUALITY
            parts = [
                (node.left, _PREC_OPERAND, None, node_slots),
                " IS NOT " if node.negated else " IS ",
                (node.right, _PREC_OPERAND, None, node_slots),
            ]
        elif isinstance(node, Like):
            prec = _PREC_EQUALITY
            parts = [
                (node.left, _PREC_OPERAND, None, node_slots),
                " NOT LIKE " if node.negated else " LIKE ",
                (node.pattern, _PREC_OPERAND, None, node_slots),
            ]
            if node.escape is not None:
                parts.append(" ESCAPE ")
                parts.append((node.escape, _PREC_OPERAND, None, node_slots))
        elif isinstance(node, In):
            prec = _PREC_EQUALITY
            parts = [(node.left, _PREC_OPERAND, None, node_slots), " NOT IN (" if node.negated else " IN ("]
            for index, value in enumerate(node.values):
                if index > 0:
                    parts.append(", ")
                parts.append((value, 0, None, node_slots))
            parts.append(")")
        elif isinstance(node, Between):
            prec = _PREC_EQUALITY
            parts = [
                (node.operand, _PREC_OPERAND, None, node_slots),
                " NOT BETWEEN " if node.negated else " BETWEEN ",
                (node.low, _PREC_OPERAND, None, node_slots),
                " AND ",
                (node.high, _PREC_OPERAND, None, node_slots),
            ]
        else:
            raise AssertionError(f"plan/explain.py: unhandled expression node type {type(node).__name__}")

        if parent is not None and _is_logical(node):
            same_chain = (parent == "and" and isinstance(node, And)) or (parent == "or" and isinstance(node, Or))
            parenthesize = not same_chain
        else:
            parenthesize = prec < need
        if parenthesize:
            parts = ["(", *parts, ")"]
        # Pushed reversed so the first piece is popped first.
        for part in reversed(parts):
            stack.append(part)
    return "".join(out)


def format_expr(expr: Expr) -> str:
    """*expr* as SQL text. Column references print by name."""
    return _format(expr, None)


def _slot_exprs(aggregate: Aggregate) -> tuple[Expr, ...]:
    """The expression each column of *aggregate*'s output row stands
    for: the group keys, then the aggregate calls, in column order."""
    slots: list[Expr] = list(aggregate.group_by())
    for call in aggregate.calls():
        args: tuple[Expr, ...] = (Star(table=None, position=call.position),) if call.arg is None else (call.arg,)
        slots.append(FunctionCall(name=call.kind, args=args, position=call.position, distinct=call.distinct))
    return tuple(slots)


def _operator_line(op: Operator, slots: tuple[Expr, ...] | None) -> str:
    if isinstance(op, Project):
        items: list[str] = []
        for item in op.select_list():
            text = _format(item.expr, slots)
            if item.alias is not None:
                text += f" AS {item.alias}"
            items.append(text)
        return f"Project ({', '.join(items)})"
    if isinstance(op, Filter):
        return f"Filter ({_format(op.predicate(), slots)})"
    if isinstance(op, ConstantGuard):
        # Joined into one `AND` chain and printed as one expression, so
        # each term is parenthesized exactly as on the `Filter` line.
        # Printing never evaluates a term (#171).
        return f"ConstantGuard ({_format(join_conjuncts(op.terms()), slots)})"
    if isinstance(op, Sort):
        keys = [_format(key.expr, slots) + (" DESC" if key.descending else " ASC") for key in op.keys()]
        return f"Sort ({', '.join(keys)})"
    if isinstance(op, Aggregate):
        groups = [_format(expr, None) for expr in op.group_by()]
        # The planner gives a call written twice (`count(*)` in the
        # select list and again in `ORDER BY`) a slot each; the line
        # lists what is computed, once per distinct text.
        aggs: list[str] = []
        for expr in _slot_exprs(op)[len(groups) :]:
            text = _format(expr, None)
            if text not in aggs:
                aggs.append(text)
        return f"Aggregate (group=[{', '.join(groups)}], aggs=[{', '.join(aggs)}])"
    if isinstance(op, Limit):
        text = str(op.limit())
        if op.offset() > 0:
            text += f" OFFSET {op.offset()}"
        return f"Limit ({text})"
    if isinstance(op, Distinct):
        return "Distinct"
    if isinstance(op, Scan):
        pushed = op.pushed()
        estimate = op.source().estimate(pushed)
        terms = ", ".join(format_expr(term) for term in pushed) if pushed else "none"
        return f"{estimate.name} (pushed: {terms} -> {estimate.selected} of {estimate.total} paths)"
    raise AssertionError(f"plan/explain.py: unhandled operator type {type(op).__name__}")


def format_plan(tree: Operator) -> str:
    """The whole tree as `--explain` prints it, one operator per line,
    root first, each child two spaces deeper, ending in a newline.

    Asks the `Scan`'s source for its own line once (`estimate()`),
    which for `blame` is one `git ls-tree` and never a `git blame`."""
    chain: list[Operator] = []
    node: Operator | None = tree
    while node is not None:
        chain.append(node)
        node = child_of(node)

    # Operators above the `Aggregate` read its output row.
    slots: tuple[Expr, ...] | None = None
    for op in chain:
        if isinstance(op, Aggregate):
            slots = _slot_exprs(op)
            break

    lines: list[str] = []
    for depth, op in enumerate(chain):
        if isinstance(op, Aggregate):
            slots = None
            lines.append("  " * depth + _operator_line(op, None))
            continue
        lines.append("  " * depth + _operator_line(op, slots))
    return "\n".join(lines) + "\n"
