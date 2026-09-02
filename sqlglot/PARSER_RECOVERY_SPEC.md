# Error-tolerant parser recovery

This document defines the contract for the opt-in parser recovery mode used by
editor and language-server callers.

## 1. Public API and compatibility

`sqlglot.parse` and `sqlglot.parse_one` accept a parser option `recover: bool = False`.
The option is propagated through dialect parser construction in the same way as the
existing parser options.

When `recover=False`, parsing behavior is unchanged. Existing callers, exceptions,
ASTs, and generated SQL must remain compatible with the current behavior.

When `recover=True`, valid, tokenizable SQL must produce the same AST semantics and
generated SQL as the normal parser. It must not introduce `Error` nodes for valid SQL.

Recovery applies to parser errors after successful tokenization. Lexical/tokenization
errors are outside this feature and keep their existing behavior.

## 2. Error expression

Add a public `sqlglot.exp.Error` expression representing a recovered source region.
Each Error node exposes:

- `this`: the exact raw SQL substring covered by the recovered token span;
- `message`: the description of the first parser error that triggered recovery;
- `start_line`, `start_col`: 1-based position of the first recovered token;
- `end_line`, `end_col`: 1-based inclusive position of the last recovered token.

The positions describe the raw region stored in `this`. A recovered region spanning
multiple tokens uses the first token as its start and the final token as its end.

Each Error node is also one parser diagnostic. A caller can obtain diagnostics by
walking the returned tree for `exp.Error` nodes. Do not emit additional Error nodes
for parser failures caused only by the same recovered region.

Generating SQL from a partial tree must not raise because it contains an Error node.
The Error node generates its `this` value verbatim; surrounding nodes continue to use
the normal generator behavior.

## 3. Recovery behavior

With `recover=True`, a parser syntax error must not raise `ParseError`. A malformed,
non-empty statement must still contribute one expression to the result of `parse`.

Prefer local recovery when the parser can resume at a boundary owned by the enclosing
construct. The required local recovery boundaries are:

1. SELECT projection items;
2. entries in a WITH/CTE list and the body of an individual CTE;
3. FROM and JOIN table sources;
4. WHERE, HAVING, and QUALIFY predicates.

At a local boundary, preserve already parsed siblings and continue parsing later
siblings/clauses when it is safe to do so. Replace only the malformed region with an
`exp.Error` node. If no required local boundary can safely resume, the whole malformed
statement may be represented by one `exp.Error` node.

Synchronization delimiters only count at the nesting depth of the construct being
recovered. A comma or keyword inside a nested parenthesized expression must not be
mistaken for a boundary of an outer SELECT list, CTE list, or clause.

The first parser error that begins a recovered region supplies that region's message.
Subsequent parse failures that are consequences of skipping the same region must not
produce a diagnostic cascade.

## 4. Statement isolation

Recovery never consumes tokens belonging to a following semicolon-delimited statement.
If a malformed statement is followed by a valid statement, the later statement must
parse exactly as it does when parsed by itself with the same dialect.

For example, recovery of the first statement in:

```sql
SELECT * FROM t WHERE x = ;
SELECT b FROM y;
```

must not change the AST or generated SQL of `SELECT b FROM y`.

## 5. Normative examples

For tokenizable SQL equivalent to:

```sql
SELECT a, +, c FROM t WHERE x = 1
```

the returned Select must retain the valid `a` and `c` projection items and the FROM
and WHERE clauses, with an Error node occupying the malformed projection region.

For:

```sql
SELECT * FROM t WHERE x = ;
```

the SELECT/FROM structure must be retained and the malformed predicate represented by
an Error node rather than discarding the whole statement.

For:

```sql
SELECT foo(a, +, c), d FROM t
```

recovery inside `foo(...)` must not treat the commas inside the function call as
SELECT-list synchronization points. The outer projection `d` must remain parseable.

These examples define observable recovery invariants, not a required internal parser
implementation.

## 6. Dialect integration

Recovery must work through existing dialect parser subclasses and their parser-table or
statement-parser overrides. Core recovery code must not branch on named dialects.
Dialect-specific recovery behavior is allowed only when an existing dialect override
owns the grammar being recovered, and the reason must be documented in code.

## 7. Bounded happy-path overhead

Recovery is opt-in. With `recover=False`, do not perform synchronization scans, clone
or copy token streams for recovery, or eagerly reconstruct raw source spans solely for
this feature. Recovery bookkeeping on the normal path should be limited to constant-time
checks and existing parser state.
