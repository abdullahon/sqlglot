# Partition pruning analysis — rules

This is the specification the analyzer in `partition_pruning.py` implements. It is
normative: where this document and the analyzer disagree, the analyzer is wrong.

A query **prunes** if the engine can decide which partitions to read without reading
table data. The analyzer answers one question: *does this query prune on the
partitioned table it reads?*

Both kinds of mistake are costly and are treated as equally serious:

- Reporting `prunable=False` for a query that does prune blocks work that was fine.
- Reporting `prunable=True` for a query that does not prune lets a full scan through.

## 1. A qualifying predicate

A predicate qualifies when **all** of the following hold.

1. It is a comparison (`=`, `<`, `<=`, `>`, `>=`, `BETWEEN`, `IN` over a literal list,
   or `IS NULL`).
2. One side references the partition column and **no other column**.
3. That side wraps the partition column only in *pruning-safe* functions (section 2),
   or not at all.
4. The other side is **constant**: literals, intervals, and time functions such as
   `CURRENT_TIMESTAMP()` / `CURRENT_DATE()`. It must not reference any column and must
   not contain a subquery.

## 2. Pruning-safe functions

On a **time-unit** partition column, these preserve pruning when their remaining
arguments are constant:

    DATE, DATETIME, TIMESTAMP, CAST(... AS DATE)
    DATE_ADD, DATE_DIFF, DATE_SUB, DATE_TRUNC
    DATETIME_DIFF
    TIMESTAMP_ADD, TIMESTAMP_DIFF, TIMESTAMP_SUB, TIMESTAMP_TRUNC
    EXTRACT, but only with the YEAR or DATE part
    FORMAT_TIMESTAMP / FORMAT_DATE, but only with the format strings
      '%F', '%Y-%m-%d', '%Y%m%d'

Everything else — arithmetic on the column (`ts + INTERVAL 1 DAY`), `EXTRACT(MONTH ...)`,
`FORMAT_DATE('%Y-%m-%d %H', ts)`, string functions — defeats pruning.

On an **integer-range** partition column no function is safe. The column must appear bare.

## 3. Combining predicates

- `AND`: the query prunes if **any** conjunct qualifies. Other predicates on other
  columns are irrelevant and must not change the verdict.
- `OR`: the query prunes only if **every** branch qualifies. A single non-qualifying
  branch defeats pruning for the whole disjunction.
- `NOT`: inverts the branch structure. Analyse the normalised form.

## 4. Reaching through composition

A query rarely filters the base table directly. The predicate may sit several layers
above it, and the analyser must decide whether it still reaches the partition column.

The predicate reaches the base table when every layer between them **passes it through
unchanged**. A layer passes the predicate through when the partition column is projected
straight up — possibly renamed by an alias, which must be followed.

A layer **blocks** the predicate when pushing it down would change the result:

- an aggregation of any kind, including one that groups by the partition column itself
- a window function
- `DISTINCT`
- `LIMIT` or `OFFSET`
- the null-extended side of an `LEFT`/`RIGHT`/`FULL OUTER JOIN`
- a `UNION`/`INTERSECT`/`EXCEPT` branch that does not itself project the column.
  Every branch is a layer in its own right and every one of them must pass the predicate
  through: branches are matched by position, not by name, so a branch that lists the
  columns in a different order, substitutes a constant or a computed value, or blocks on
  its own account defeats pruning for the whole set operation

A layer also blocks it when the column is not projected as itself — for example
`SELECT DATE_TRUNC(ts, DAY) AS ts FROM ...` shadows the real column with a computed one,
so an outer predicate on `ts` no longer constrains the partition column.

If any layer blocks, the query does not prune, however well-formed the outer predicate is.

## 5. Which table

Only the partitioned tables named in the supplied spec matter. A query that reads no
partitioned table trivially does not prune, and the analyser reports that as
`prunable=False` with reason `no_partitioned_table`.

## 6. Diagnostics for blocked composition

When a predicate is syntactically suitable for pruning but cannot reach a configured
partition column because a layer in section 4 blocks propagation, the analyzer must
identify the **first semantic boundary** encountered while tracing from the outer
predicate toward the base table.

`PruningResult.reason` uses the stable form:

    blocked_by:<category>@<scope>

`<scope>` is the CTE name or derived-table alias whose query layer contains the
boundary. The supported categories are:

    aggregation
    window
    distinct
    limit_offset
    computed_projection
    null_extended_join
    set_operation_branch

For a set operation, the set-operation scope is the first boundary when any branch
cannot pass the corresponding output column through unchanged. This includes branches
that supply a constant, a computed expression, or a different source column in that
output position.

If several layers are unsafe, report the outermost one: the first boundary reached from
the predicate. If one query layer contains more than one blocker kind, use the first
applicable category in the order listed above.

Successful pruning keeps the existing `pruned_on:<table>.<column>` reason. Existing
non-composition reasons such as `no_partitioned_table`, `no_filter`, and
`no_qualifying_predicate` remain unchanged when no section-4 boundary is responsible.

## 7. Normalized literal partition ranges

For a successful pruning decision the analyzer must also expose the literal partition
ranges that can be proved from the query. This is used by CI diagnostics to show not
only that pruning is possible, but which part of the partition key space is selected.

Add a public immutable `PartitionRange` value with these fields:

    lower: str | None
    lower_inclusive: bool
    upper: str | None
    upper_inclusive: bool

and add `ranges: tuple[PartitionRange, ...] = ()` to `PruningResult`. Existing callers
that construct or inspect only `prunable` and `reason` must remain compatible.

Bounds are the canonical BigQuery SQL text of the literal, for example `10` or
`'2025-03-30'`. `None` means unbounded. Ranges are ordered by lower bound and must be
non-overlapping; overlapping ranges are merged. A point range has equal inclusive
lower and upper bounds.

Range derivation is required for bare partition-column comparisons against integer
literals and ISO date/timestamp string literals:

    col = v              -> [v, v]
    col > v              -> (v, +inf)
    col >= v             -> [v, +inf)
    col < v              -> (-inf, v)
    col <= v             -> (-inf, v]
    col BETWEEN a AND b  -> [a, b]
    col IN (a, b, ...)   -> one point range per distinct literal

When the literal is written on the left, comparison direction is reversed before the
range is formed. Duplicate `IN` values are removed.

Boolean composition operates on the ranges after column lineage has been traced to the
configured base partition column:

- `AND` intersects the ranges from partition predicates. Conjuncts on unrelated
  columns do not widen or erase a known partition range.
- `OR` unions branch ranges only when every branch yields a range for the same
  configured partition column. Otherwise the expression yields no provable range.
- `NOT` complements the normalized range set, applying De Morgan's law through nested
  boolean expressions.

If an `AND` intersection is empty, the query is still `prunable=True` and `ranges=()`:
the predicate is unsatisfiable and therefore reads no partitions.

Safe wrappers from section 2, `IS NULL`, dynamic constants such as `CURRENT_DATE()`,
and literal forms other than the required integer / ISO string forms keep their
existing pruning verdict but do not have to produce ranges.

For blocked composition, `prunable=False`, the section-6 blocker reason is returned,
and `ranges` must be empty. For a successful literal predicate through aliases, CTEs,
derived tables, joins, or set operations, the ranges describe the configured base
partition column rather than the outer alias.

The analyzer must keep normalized range analysis bounded. A result may contain at most
32 disjoint ranges. Canonicalize and merge ranges before applying this limit. If the
exact normalized result would still contain more than 32 disjoint ranges, preserve the
existing pruning verdict and reason but return `ranges=()` rather than expanding or
truncating the range set. Do not reject a query merely because exact range diagnostics
exceed the cap.

The cap applies to the final normalized result, not to intermediate syntax. For example,
many overlapping OR branches that merge to one interval produce that interval, and an
IN list larger than the cap may still produce ranges when later AND predicates reduce
the exact normalized result to 32 or fewer disjoint ranges.
