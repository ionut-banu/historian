"""The `historian` command-line entry point (`_docs/spec.md` §5).

Issue #13, closing out M2: wires lexer -> parser -> binder -> planner
-> operator tree -> stdout, so
``historian "SELECT path, author_name FROM blame WHERE path = 'src/a.py'"``
actually runs against a real repository.

Scope is deliberately a slice of §5, not the whole section - grooming
found §5 reads as one unit and is not one unit of work. In scope here:
a required positional `QUERY`, `-C`/`--repo PATH` (default: the
current working directory), and `table`-format output. Out of scope,
each already filed: `-f`/`--file` (#40), the other `--format` values
and the pretty position-aware error box (#41, M6 item 18), `--explain`
and `--stats` (#42, M4 item 15), `--no-pushdown` (#43), the REPL
(#44, M6 item 19). None of these five flags is registered with
`argparse` below, so passing any of them hits `argparse`'s own
"unrecognized arguments" handling and exits 2 - not silently accepted,
not silently ignored.

Two §5 rules apply in full even though the rest of the section does
not, because both are cheap now and expensive to retrofit once output
code exists: **the format never depends on whether stdout is a
terminal** (no `isatty()` call anywhere in this module - see
`_docs/decisions.md`, 2026-09-01), and **results go to stdout,
everything else to stderr**, so piping works. §5's "`NULL` renders as
the word `NULL` in `table`" rule is in scope for the same reason, even
though `table` is otherwise a minimal implementation - see
`_render_table` below for what "aligned and human-facing" means here
and what it deliberately does not yet do (colour, dimming `NULL`,
paging - all §5 surface this issue does not touch).

Errors: four types, never a traceback
----------------------------------------

Per §3's "Errors" and this issue's own constraints, exactly four
exception types are caught at this boundary and no others: `LexError`
(`sql/lexer.py`), `ParseError` (`sql/parser.py`), `BindError`
(`sql/binder.py`), and `EvalError` (`exec/expression.py`) - the last
one easy to miss, since most of what used to reach it as a generic
"unsupported grammar" failure is caught earlier now. Issue #60 closed
the biggest gap: `SELECT count(*) FROM blame` used to parse and bind
cleanly and only fail once evaluation reached the unhandled
`FunctionCall` (`sql/binder.py` had no aggregate registry yet); it now
either runs (a real aggregate, correct arity) or raises `BindError` at
bind time (an unknown function, wrong arity, or an aggregate call in
`WHERE`) - `sql/binder.py`'s own docstring has the details. `EvalError`
remains reachable for other reasons, `exec/operators.py`'s `Aggregate`
among them (`sum`'s int64-overflow check). Each of the four prints
`error: <message>` to stderr - the exception's own message text, with
no position/caret/"blame has: ..." rendering (that box is M6 item 18)
- and the process exits 1.

A fifth failure mode is not one of the four: the repository itself
could not be read - `-C` pointing at a path that does not exist
(`FileNotFoundError`, a subclass of `OSError`, from `subprocess.run`'s
own `cwd` handling) or at a directory with no `.git`
(`RuntimeError`, raised by `tables/blame.py`'s `_run_git_bytes` when
the `git` subprocess itself fails). Both are caught around plan
execution and exit 3. Deliberately *not* printed via `str(exc)`: a
`RuntimeError` from `_run_git_bytes` embeds git's own possibly
multi-line stderr text, which would violate spec §5's "never a
traceback" spirit by leaking a different kind of unstructured wall of
text instead - this module substitutes one plain, single-line message
naming the repository path, regardless of which of the two exception
types triggered it or what git itself said.

Issue #49: a backstop for everything else, and a pipe of its own
------------------------------------------------------------------

The four exception types above cover every way a *query* can be the
user's fault. They do not cover a bug in historian itself - an
internal invariant broken somewhere between a clean bind/plan and a
row actually being produced or rendered, which is neither a parse
error, a binding error, nor a documented "unsupported grammar"
rejection. Spec §3 and §5 both say "never a traceback" without
carving out an exception for that case, so it needs a backstop rather
than being left to whatever Python does by default (a traceback).

`main` therefore gains two more `except` clauses, added to the chain
without touching the two above:

- `except BrokenPipeError`, ordered *before* both `except (OSError,
  RuntimeError)` and the new catch-all below. `BrokenPipeError` is an
  `OSError` subclass (stdout closed under it, e.g. piped to `head`),
  so without its own clause it would be silently misreported as "the
  repository could not be read", exit 3 - a diagnosis that is simply
  wrong for an ordinary broken pipe. Caught on its own, `main` returns
  `0` and nothing further is written anywhere; a `| head` is not a
  failure.
- `except Exception`, last in the chain, after every clause above it.
  Never `except BaseException` - `KeyboardInterrupt` is deliberately
  not an `Exception` subclass, and this clause must not swallow it.
  Anything landing here is historian's own bug, not the user's, so the
  message says that and discards the exception's own text entirely -
  same reasoning as the `RuntimeError` case above, generalized: `str
  (exc)`, the exception's class name, and any traceback text must
  never reach the user. Exit code `4`, distinct from `1` (bad query),
  `2` (bad usage), and `3` (repository unreadable) - see
  `_docs/spec.md` §5 and `_docs/decisions.md`.

The guarded region also grows to match: rendering and writing the
result (`_render_table`, `sys.stdout.write`) move *inside* the `try`,
where they were not before. A bug while rendering is exactly the kind
of internal error this backstop exists for, and a `BrokenPipeError`
from a closed pipe can only ever be raised from the write itself - so
both new clauses need the write to be reachable from inside the same
`try` that already guards lex/parse/bind/plan/materialize.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from historian.exec.expression import EvalError
from historian.plan.planner import plan
from historian.schema import Row, Schema
from historian.sql.binder import BindError, bind
from historian.sql.lexer import LexError, tokenize
from historian.sql.parser import ParseError, parse
from historian.values import Value

__all__ = ["main"]


def _build_parser() -> argparse.ArgumentParser:
    """The argument grammar this issue owns: a required positional
    `query` and `-C`/`--repo`. Every other §5 flag is deliberately
    absent - see the module docstring - so `argparse` itself rejects
    them as unrecognized, with its own default exit code of 2, which
    already matches spec §5 exactly and needs no custom handling
    here."""
    parser = argparse.ArgumentParser(
        prog="historian", description="A SQL query engine over git history."
    )
    parser.add_argument("query", help="the SQL query to run")
    parser.add_argument(
        "-C",
        "--repo",
        default=None,
        help="repository to query (default: the current working directory)",
    )
    return parser


def _format_value(value: Value) -> str:
    """Render one cell for `table` format. `NULL` is the literal word
    `NULL` (spec §5) - not an empty string (that is `csv`/`tsv`'s
    rule, #41) and not Python's own `None` spelling."""
    if value is None:
        return "NULL"
    return str(value)


def _render_table(schema: Schema, rows: list[Row]) -> str:
    """Render *rows*, described by *schema*, as `table` format: a
    header line of column names, then one line per row, every column
    left-justified to the widest cell in it (header included).

    Deterministic and terminal-independent by construction: every
    width comes from the data being printed, never from `shutil`,
    `os.get_terminal_size`, or any other read of the environment - so
    the same rows always render to the same bytes, piped or not (spec
    §3 "Determinism and row order"; §5 "the format never depends on
    whether stdout is a terminal"). Trailing padding is stripped from
    the end of each line rather than left as trailing whitespace on
    the last column.

    Header-only output (zero data rows) is not a special case: the
    loop over `rows` below simply does not run, and the header line -
    computed from `schema` alone - is still returned. A predicate
    matching nothing is not an error (an explicit acceptance
    criterion), and this function is what makes that true for output:
    it never suppresses the header for an empty result.
    """
    headers = schema.names
    str_rows = [[_format_value(value) for value in row] for row in rows]
    widths = [len(header) for header in headers]
    for str_row in str_rows:
        for i, cell in enumerate(str_row):
            widths[i] = max(widths[i], len(cell))

    def _render_line(cells: Sequence[str]) -> str:
        return "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(cells)).rstrip()

    lines = [_render_line(headers)]
    lines.extend(_render_line(str_row) for str_row in str_rows)
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    """The `historian` entry point.

    `argv` defaults to `None`, in which case `argparse` reads
    `sys.argv[1:]` itself; tests pass a list explicitly and call this
    in-process, per this issue's own constraint - no test spawns a
    `historian` subprocess.

    Runs the full pipeline - lex, parse, bind, plan, materialize every
    row from the resulting operator tree, then render and write the
    table - inside one `try`. A query that fails at any stage
    (including mid-evaluation, for `EvalError`, or while rendering)
    prints nothing to stdout at all, which is also what keeps "the
    header line printed, or nothing" true rather than a partial table
    appearing before a late failure.

    Five clauses handle everything that `try` can raise, in this
    order:

    1. The four query-error types (`LexError`/`ParseError`/
       `BindError`/`EvalError`): the user's SQL is at fault. Exit 1.
    2. `BrokenPipeError`: stdout was closed on the other end (e.g.
       piped to `head`) while the result was being written. Not the
       user's fault and not historian's bug either - exit 0, nothing
       further written anywhere.
    3. `(OSError, RuntimeError)`: the repository itself could not be
       read. Exit 3. Ordered after `BrokenPipeError` deliberately -
       `BrokenPipeError` is an `OSError` subclass, and without its own
       clause first it would be misreported as this case instead.
    4. `Exception` (never `BaseException` - see below): anything else,
       which by elimination is a bug in historian rather than in the
       user's query. Exit 4, with a fixed message that never repeats
       `str(exc)`, the exception's class name, or a traceback (see
       #49). `KeyboardInterrupt` is a `BaseException`, not an
       `Exception`, so this clause does not catch it and it propagates
       out of `main` uncaught, same as it would with no handler here
       at all.
    """
    parser = _build_parser()
    args = parser.parse_args(argv)
    repo = Path(args.repo) if args.repo is not None else Path.cwd()

    try:
        tokens = tokenize(args.query)
        stmt = parse(tokens)
        bound = bind(stmt)
        tree = plan(bound, repo)
        rows = list(tree.rows())
        sys.stdout.write(_render_table(tree.schema, rows))
    except (LexError, ParseError, BindError, EvalError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except BrokenPipeError:
        return 0
    except (OSError, RuntimeError):
        print(f"error: could not read repository: {repo}", file=sys.stderr)
        return 3
    except Exception:
        print(
            "error: historian hit an internal error and could not finish this "
            "query - this is a bug in historian, not a mistake in your SQL. "
            "Please report it, with the query that triggered it.",
            file=sys.stderr,
        )
        return 4

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
