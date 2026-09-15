"""The planner: `BoundSelectStatement` -> operator tree.

`planner.py` is the only module here. Spec §3's module layout also
lists `plan/nodes.py` ("operator tree definitions"), but per §3's "One
plan representation, not two" there is no separate node IR in v1:
`exec/operators.py`'s `Scan`/`Filter`/`Project` classes are the plan
representation, so there is nothing left for `nodes.py` to define.
Issue #13's own grooming confirmed this is out of scope rather than an
oversight - see `plan/planner.py`'s module docstring.
"""
