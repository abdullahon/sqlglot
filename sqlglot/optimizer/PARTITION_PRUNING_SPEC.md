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
