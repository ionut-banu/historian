"""Tests for `historian.plan.explain`: the operator-tree and expression
printer behind `--explain` (issue #42, spec §5).

Everything here runs against an in-memory fake `ScanSource`: no
repository, no git, no subprocess - the printer lives in `plan/`, which
may not know a table. The fake implements the one method the printer
asks a source for, `estimate(pushed)`.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from pathlib import Path

import pytest

from historian.exec.operators import ScanEstimate
from historian.plan.explain import format_expr, format_plan
from historian.plan.optimizer import optimize
from historian.plan.planner import plan
from historian.schema import Row
from historian.sql.binder import bind
from historian.sql.lexer import tokenize
from historian.sql.parser import parse
from historian.tables.blame import BLAME_SCHEMA


class _FakeSource:
    schema = BLAME_SCHEMA

    def __init__(self, repo: Path, accept_path_eq: bool = True) -> None:
        self.estimate_calls: list[tuple] = []
        self._accept = accept_path_eq

    def capabilities(self) -> set[str]:
        return {"x"} if self._accept else set()

    def accepts(self, term) -> bool:
        return self._accept and type(term).__name__ == "BinaryOp" and term.op.name == "EQ"

    def scan(self, pushed: Sequence = ()) -> Iterator[Row]:
        raise AssertionError("explain must never scan")

    def estimate(self, pushed: Sequence = ()) -> ScanEstimate:
        self.estimate_calls.append(tuple(pushed))
        return ScanEstimate(name="FakeScan", selected=len(pushed), total=5)


def _tree(query: str, optimized: bool = True):
    bound = bind(parse(tokenize(query)), catalog={"blame": BLAME_SCHEMA})
    tree = plan(bound, Path("."), tables={"blame": _FakeSource})
    return optimize(tree) if optimized else tree


def _explain(query: str) -> list[str]:
    return format_plan(_tree(query)).splitlines()


def _where(expr_sql: str) -> str:
    """The text `format_plan` prints for the `Filter` of
    `SELECT path FROM blame WHERE <expr_sql>`."""
    lines = _explain(f"SELECT path FROM blame WHERE {expr_sql}")
    (filter_line,) = [line for line in lines if line.strip().startswith("Filter")]
    prefix = "Filter ("
    text = filter_line.strip()
    assert text.startswith(prefix) and text.endswith(")")
    return text[len(prefix) : -1]


# --- tree shape -------------------------------------------------------------


def test_full_tree_for_the_spec_query():
    out = format_plan(
        _tree(
            "SELECT author_name, count(*) FROM blame WHERE path LIKE 'src/%' "
            "GROUP BY author_name ORDER BY 2 DESC"
        )
    )
    assert out == (
        "Project (author_name, count(*))\n"
        "  Sort (count(*) DESC)\n"
        "    Aggregate (group=[author_name], aggs=[count(*)])\n"
        "      Filter (path LIKE 'src/%')\n"
        "        FakeScan (pushed: none -> 0 of 5 paths)\n"
    )


def test_no_where_has_no_filter_line_and_pushed_none():
    assert _explain("SELECT path FROM blame") == [
        "Project (path)",
        "  FakeScan (pushed: none -> 0 of 5 paths)",
    ]


def test_pushed_terms_are_joined_in_order_on_the_scan_line_only():
    lines = _explain("SELECT path FROM blame WHERE path = 'a' AND line_no > 3 AND path = 'b'")
    assert lines[-1] == "    FakeScan (pushed: path = 'a', path = 'b' -> 2 of 5 paths)"
    assert lines[1] == "  Filter (path = 'a' AND line_no > 3 AND path = 'b')"


def test_estimate_is_called_once_with_the_pushed_terms():
    tree = _tree("SELECT path FROM blame WHERE path = 'a'")
    format_plan(tree)
    source = tree._child._child.source()  # Project -> Filter -> Scan
    assert len(source.estimate_calls) == 1
    assert len(source.estimate_calls[0]) == 1


def test_unoptimized_tree_prints_pushed_none():
    out = format_plan(_tree("SELECT path FROM blame WHERE path = 'a'", optimized=False))
    assert "pushed: none -> 0 of 5 paths" in out


def test_having_filter_is_its_own_line_above_aggregate():
    lines = _explain(
        "SELECT author_name, count(*) FROM blame GROUP BY author_name HAVING count(*) > 2"
    )
    assert lines[0] == "Project (author_name, count(*))"
    assert lines[1] == "  Filter (count(*) > 2)"
    assert lines[3] == "      FakeScan (pushed: none -> 0 of 5 paths)"
    assert lines[2].startswith("    Aggregate (group=[author_name], aggs=[count(*)")


def test_limit_offset_distinct_and_sort_lines():
    lines = _explain(
        "SELECT DISTINCT author_name FROM blame ORDER BY author_name LIMIT 5 OFFSET 2"
    )
    assert lines == [
        "Limit (5 OFFSET 2)",
        "  Distinct",
        "    Project (author_name)",
        "      Sort (author_name ASC)",
        "        FakeScan (pushed: none -> 0 of 5 paths)",
    ]


def test_limit_without_offset():
    assert _explain("SELECT path FROM blame LIMIT 3")[0] == "Limit (3)"


def test_sort_key_always_prints_direction():
    lines = _explain("SELECT path FROM blame ORDER BY line_no, path DESC")
    assert "  Sort (line_no ASC, path DESC)" in lines


def test_aggregate_without_group_by_and_distinct_call():
    lines = _explain("SELECT count(DISTINCT author_name), sum(line_no) FROM blame")
    assert lines[0] == "Project (count(DISTINCT author_name), sum(line_no))"
    assert lines[1] == "  Aggregate (group=[], aggs=[count(DISTINCT author_name), sum(line_no)])"


def test_project_prints_alias():
    assert _explain("SELECT path AS p FROM blame")[0] == "Project (path AS p)"


def test_group_key_expression_is_printed_where_the_select_list_uses_it():
    lines = _explain("SELECT line_no + 1, count(*) FROM blame GROUP BY line_no + 1")
    assert lines[0] == "Project (line_no + 1, count(*))"
    assert lines[1] == "  Aggregate (group=[line_no + 1], aggs=[count(*)])"


def test_a_call_written_twice_is_listed_once_on_the_aggregate_line():
    lines = _explain("SELECT count(*), count(*) FROM blame")
    assert lines[0] == "Project (count(*), count(*))"
    assert lines[1] == "  Aggregate (group=[], aggs=[count(*)])"


# --- expression text ----------------------------------------------------------


@pytest.mark.parametrize(
    "sql, text",
    [
        ("path = 'src/utils.py'", "path = 'src/utils.py'"),
        ("path IN ('a', 'b')", "path IN ('a', 'b')"),
        ("path NOT IN ('a')", "path NOT IN ('a')"),
        ("path IN ()", "path IN ()"),
        ("path = 'it''s'", "path = 'it''s'"),
        ("path LIKE 'a%' ESCAPE '!'", "path LIKE 'a%' ESCAPE '!'"),
        ("path NOT LIKE 'a%'", "path NOT LIKE 'a%'"),
        ("line_no BETWEEN 1 AND 3", "line_no BETWEEN 1 AND 3"),
        ("line_no NOT BETWEEN 1 AND 3", "line_no NOT BETWEEN 1 AND 3"),
        ("path IS NULL", "path IS NULL"),
        ("path IS NOT NULL", "path IS NOT NULL"),
        ("line_no <> 2", "line_no <> 2"),
        ("line_no != 2", "line_no <> 2"),
        ("line_no >= 2.5", "line_no >= 2.5"),
        ("line_no + 1 * 2 > 3", "line_no + 1 * 2 > 3"),
        ("(line_no + 1) * 2 > 3", "(line_no + 1) * 2 > 3"),
        ("line_no - (1 - 2) > 0", "line_no - (1 - 2) > 0"),
        ("path || 'x' = 'ax'", "path || 'x' = 'ax'"),
        ("-line_no < 0", "-line_no < 0"),
        ("-(-line_no) < 0", "-(-line_no) < 0"),
        ("path = NULL", "path = NULL"),
        ("blame.path = 'a'", "path = 'a'"),
    ],
)
def test_expression_text(sql, text):
    assert _where(sql) == text


def test_nested_and_or_not_operands_are_parenthesized():
    assert _where("path = 'a' OR path = 'b'") == "path = 'a' OR path = 'b'"
    assert _where("(path = 'a' OR path = 'b') AND line_no = 1") == "(path = 'a' OR path = 'b') AND line_no = 1"
    assert _where("path = 'a' OR (path = 'b' AND line_no = 1)") == "path = 'a' OR (path = 'b' AND line_no = 1)"
    assert _where("NOT (path = 'a' OR line_no = 1)") == "NOT (path = 'a' OR line_no = 1)"
    assert _where("NOT path = 'a'") == "NOT path = 'a'"
    assert _where("line_no = 1 AND NOT path = 'a'") == "line_no = 1 AND (NOT path = 'a')"


def test_string_literal_with_non_ascii_and_embedded_quotes():
    assert _where("path = 'café.py'") == "path = 'café.py'"
    assert _where("path = 'a\"b'") == "path = 'a\"b'"


def test_deeply_nested_expression_does_not_hit_the_recursion_limit():
    depth = 900
    sql = "line_no = " + " + ".join(["1"] * depth)
    text = _where(sql)
    assert text == sql
    long_and = " AND ".join(["line_no = 1"] * 900)
    assert _where(long_and) == long_and


def test_format_expr_is_exported_for_a_bound_expression():
    bound = bind(parse(tokenize("SELECT path FROM blame WHERE path = 'a'")), catalog={"blame": BLAME_SCHEMA})
    assert format_expr(bound.where) == "path = 'a'"
