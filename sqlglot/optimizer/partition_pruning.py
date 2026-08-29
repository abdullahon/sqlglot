"""Decide whether a query prunes partitions on the tables it reads.

The rules this module implements are written down in ``PARTITION_PRUNING_SPEC.md``,
next to this file. That document is normative.

Two mistakes matter equally. Saying a query does not prune when it does blocks work
that was fine; saying it prunes when it does not lets a full table scan through.
"""

from __future__ import annotations

import dataclasses
import typing as t

from sqlglot import exp, parse_one
from sqlglot.optimizer.qualify import qualify

# Functions that keep pruning alive on a time-unit partition column, provided their
# remaining arguments are constant. See section 2 of the spec.
PRUNING_SAFE = (
    exp.Date,
    exp.DateAdd,
    exp.DateDiff,
    exp.DateSub,
    exp.DateTrunc,
    exp.TimestampAdd,
    exp.TimestampDiff,
    exp.TimestampSub,
    exp.TimestampTrunc,
)

# Coercion nodes the dialect inserts around a column. They carry no semantics of their
# own, so pruning analysis looks straight through them.
TRANSPARENT = (
    exp.TsOrDsToDate,
    exp.TsOrDsToDatetime,
    exp.TsOrDsToTime,
    exp.TsOrDsToTimestamp,
)

# FORMAT_TIMESTAMP / FORMAT_DATE keep pruning only for these format strings.
SAFE_TIME_FORMATS = {"%F", "%Y-%m-%d", "%Y%m%d"}

# EXTRACT keeps pruning only for these parts.
SAFE_EXTRACT_PARTS = {"YEAR", "DATE"}

COMPARISONS = (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.Between, exp.In)


@dataclasses.dataclass(frozen=True)
class PartitionSpec:
    """A partitioned table and the column it is partitioned on."""

    table: str
    column: str
    kind: str = "time"  # "time" or "integer"


@dataclasses.dataclass(frozen=True)
class PruningResult:
    prunable: bool
    reason: str

    def __bool__(self) -> bool:
        return self.prunable


def _is_constant(node: exp.Expr) -> bool:
    """True when the node can be evaluated without reading table data."""
    if node.find(exp.Column, exp.Select, exp.Subquery):
        return False
    return True


def _wraps_only_partition_column(node: exp.Expr, column: str) -> bool:
    """True when ``node`` references ``column`` and no other column."""
    columns = list(node.find_all(exp.Column))
    if not columns:
        return False
    return all(c.name == column for c in columns)


def _is_safe_wrapper(node: exp.Expr, column: str, kind: str) -> bool:
    """True when ``node`` isolates the partition column through safe functions only."""
    if isinstance(node, exp.Column):
        return node.name == column

    if isinstance(node, TRANSPARENT):
        return _is_safe_wrapper(node.this, column, kind)

    if kind == "integer":
        # No function is safe over an integer-range partition column.
        return False

    if isinstance(node, exp.Cast):
        if node.to.this != exp.DataType.Type.DATE:
            return False
        return _is_safe_wrapper(node.this, column, kind)

    if isinstance(node, exp.Extract):
        part = node.this.name.upper()
        if part not in SAFE_EXTRACT_PARTS:
            return False
        return _is_safe_wrapper(node.expression, column, kind)

    if isinstance(node, exp.TimeToStr):
        fmt = node.args.get("format")
        if fmt is None or fmt.name not in SAFE_TIME_FORMATS:
            return False
        return _is_safe_wrapper(node.this, column, kind)

    if isinstance(node, PRUNING_SAFE):
        inner = node.this
        rest = [v for k, v in node.args.items() if k != "this" and isinstance(v, exp.Expr)]
        if not all(_is_constant(r) for r in rest):
            return False
        return _is_safe_wrapper(inner, column, kind)

    return False


def _qualifies(predicate: exp.Expr, spec: PartitionSpec) -> bool:
    """True when a single comparison isolates the partition column against a constant."""
    if not isinstance(predicate, COMPARISONS):
        return False

    left = predicate.this
    right = predicate.expression

    if isinstance(predicate, exp.Between):
        low, high = predicate.args.get("low"), predicate.args.get("high")
        if not (_is_constant(low) and _is_constant(high)):
            return False
        return _is_safe_wrapper(left, spec.column, spec.kind)

    if isinstance(predicate, exp.In):
        values = predicate.args.get("expressions") or []
        if not values or not all(_is_constant(v) for v in values):
            return False
        return _is_safe_wrapper(left, spec.column, spec.kind)

    if right is None:
        return False

    for column_side, constant_side in ((left, right), (right, left)):
        if not _wraps_only_partition_column(column_side, spec.column):
            continue
        if not _is_constant(constant_side):
            continue
        if _is_safe_wrapper(column_side, spec.column, spec.kind):
            return True

    return False


def _conjuncts(where: exp.Expr) -> t.Iterator[exp.Expr]:
    """Yield the top-level AND-ed predicates of a WHERE clause."""
    if isinstance(where, exp.And):
        yield from _conjuncts(where.this)
        yield from _conjuncts(where.expression)
    else:
        yield where


def analyze(
    sql: str | exp.Expr,
    specs: t.Sequence[PartitionSpec],
    dialect: str = "bigquery",
) -> PruningResult:
    """Report whether ``sql`` prunes partitions on any table in ``specs``."""
    expression = parse_one(sql, dialect=dialect) if isinstance(sql, str) else sql
    expression = qualify(expression.copy(), dialect=dialect, validate_qualify_columns=False)

    by_name = {s.table.lower(): s for s in specs}

    select = expression.find(exp.Select)
    if select is None:
        return PruningResult(False, "not_a_select")

    tables = [
        by_name[table.name.lower()]
        for table in select.find_all(exp.Table)
        if table.name.lower() in by_name
    ]
    if not tables:
        return PruningResult(False, "no_partitioned_table")

    where = select.args.get("where")
    if where is None:
        return PruningResult(False, "no_filter")

    for spec in tables:
        for predicate in _conjuncts(where.this):
            if _qualifies(predicate, spec):
                return PruningResult(True, f"pruned_on:{spec.table}.{spec.column}")

    return PruningResult(False, "no_qualifying_predicate")
