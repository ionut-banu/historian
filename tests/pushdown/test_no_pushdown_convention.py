"""`--no-pushdown` (#43) shares a convention with the differential
harness, not a code path: "pushdown disabled" means "nothing pushed",
i.e. `source.scan(pushed=())`. A tree `plan()` returns, never passed
through `optimize()`, is exactly that tree."""

from __future__ import annotations

import inspect

from historian.catalog import SCHEMAS
from historian.exec.operators import Scan, child_of
from historian.plan.optimizer import optimize
from historian.plan.planner import plan
from historian.sql.binder import bind
from historian.sql.lexer import tokenize
from historian.sql.parser import parse
from historian.tables.blame import BlameScan


def test_unoptimized_plan_scans_with_nothing_pushed(tiny_repo, monkeypatch):
    calls = []
    original = BlameScan.scan

    def spy(self, pushed=()):
        calls.append(tuple(pushed))
        return original(self, pushed=pushed)

    monkeypatch.setattr(BlameScan, "scan", spy)
    bound = bind(parse(tokenize("SELECT path FROM blame WHERE path = 'src/utils.py'")), catalog=SCHEMAS)
    sources: list[BlameScan] = []

    def factory(repo):
        sources.append(BlameScan(repo))
        return sources[-1]

    tree = plan(bound, tiny_repo, tables={"blame": factory})
    node = tree
    while not isinstance(node, Scan):
        node = child_of(node)
    assert node.pushed() == ()
    list(tree.rows())
    assert calls == [()]
    assert sources[0].blamed_paths == ["feature/thing.py", "src/utils.py"]


def test_no_pushdown_parameter_on_plan_or_optimize():
    for fn in (plan, optimize):
        assert not any("push" in name for name in inspect.signature(fn).parameters)
