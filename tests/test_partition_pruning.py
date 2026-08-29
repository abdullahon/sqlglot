import unittest

from sqlglot.optimizer.partition_pruning import PartitionSpec, analyze

EVENTS = [PartitionSpec("events", "ts")]
ORDERS = [PartitionSpec("orders", "customer_id", "integer")]


class TestPartitionPruning(unittest.TestCase):
    def assertPrunes(self, sql, specs=EVENTS):
        result = analyze(sql, specs)
        self.assertTrue(result.prunable, f"expected pruning, got {result.reason}: {sql}")

    def assertDoesNotPrune(self, sql, specs=EVENTS):
        result = analyze(sql, specs)
        self.assertFalse(result.prunable, f"expected no pruning: {sql}")

    def test_range_filter_prunes(self):
        self.assertPrunes(
            "SELECT * FROM events WHERE ts >= '2025-03-30' AND ts < '2025-03-31'"
        )

    def test_safe_functions_prune(self):
        for predicate in (
            "DATE(ts) = '2025-03-30'",
            "CAST(ts AS DATE) = '2025-03-30'",
            "EXTRACT(DATE FROM ts) = '2025-03-30'",
            "EXTRACT(YEAR FROM ts) = 2025",
            "TIMESTAMP_TRUNC(ts, MONTH) >= '2025-04-01'",
            "TIMESTAMP_ADD(ts, INTERVAL 1 DAY) < '2025-01-03'",
            "FORMAT_TIMESTAMP('%Y-%m-%d', ts) = '2025-03-30'",
        ):
            with self.subTest(predicate=predicate):
                self.assertPrunes(f"SELECT * FROM events WHERE {predicate}")

    def test_unsupported_functions_do_not_prune(self):
        for predicate in (
            "EXTRACT(MONTH FROM ts) = 3",
            "FORMAT_DATE('%Y-%m-%d %H', ts) = '2025-03-28 20'",
            "ts + INTERVAL 1 DAY > CURRENT_TIMESTAMP()",
        ):
            with self.subTest(predicate=predicate):
                self.assertDoesNotPrune(f"SELECT * FROM events WHERE {predicate}")

    def test_constant_side_may_use_functions(self):
        self.assertPrunes("SELECT * FROM events WHERE ts = CURRENT_TIMESTAMP()")
        self.assertPrunes(
            "SELECT * FROM events WHERE ts > CURRENT_TIMESTAMP() - INTERVAL 1 DAY"
        )

    def test_dynamic_predicates_do_not_prune(self):
        self.assertDoesNotPrune("SELECT * FROM events WHERE ts = (SELECT MAX(d) FROM other)")
        self.assertDoesNotPrune("SELECT * FROM events WHERE ts >= ts2")

    def test_other_columns_are_irrelevant(self):
        self.assertPrunes("SELECT * FROM events WHERE user_id = 7 AND ts >= '2025-01-01'")
        self.assertPrunes("SELECT * FROM events WHERE ts >= '2025-01-01' AND user_id = 7")

    def test_missing_filter(self):
        self.assertDoesNotPrune("SELECT * FROM events")

    def test_unpartitioned_table(self):
        result = analyze("SELECT * FROM other WHERE ts >= '2025-01-01'", EVENTS)
        self.assertFalse(result.prunable)
        self.assertEqual(result.reason, "no_partitioned_table")

    def test_integer_range_partition(self):
        self.assertPrunes("SELECT * FROM orders WHERE customer_id BETWEEN 30 AND 50", ORDERS)
        self.assertDoesNotPrune(
            "SELECT * FROM orders WHERE customer_id + 1 BETWEEN 30 AND 50", ORDERS
        )

    def test_transparent_wrapper_prunes(self):
        self.assertPrunes("SELECT * FROM (SELECT * FROM events) t WHERE ts >= '2025-03-30'")
        self.assertPrunes(
            "WITH base AS (SELECT * FROM events) SELECT * FROM base WHERE ts >= '2025-03-30'"
        )


if __name__ == "__main__":
    unittest.main()
