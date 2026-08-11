from __future__ import annotations

import argparse
import sys
import unittest
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.scrape_almaty import (
    DEFAULT_PARTITION_TARGETS,
    DEFAULT_TOTAL_TARGET,
    ROOM_PARTITIONS,
    partition_counts,
    partition_status,
    resolve_partition_targets,
    scaled_partition_targets,
)


class BalancedTargetsTest(unittest.TestCase):
    def test_default_total_and_order_are_stable(self) -> None:
        self.assertEqual(sum(DEFAULT_PARTITION_TARGETS.values()), DEFAULT_TOTAL_TARGET)
        self.assertEqual(
            list(DEFAULT_PARTITION_TARGETS),
            [name for name, _ in ROOM_PARTITIONS],
        )

    def test_scaled_targets_sum_to_requested_total(self) -> None:
        for total in (5, 20_000, 40_000, 41_000, 55_555):
            targets = scaled_partition_targets(total)
            self.assertEqual(sum(targets.values()), total)
            self.assertTrue(all(value > 0 for value in targets.values()))

    def test_explicit_room_override_wins(self) -> None:
        values = {f"{name}_target": None for name, _ in ROOM_PARTITIONS}
        values["rooms_3_target"] = 12_345
        args = argparse.Namespace(target=40_000, **values)
        targets = resolve_partition_targets(args)
        self.assertEqual(targets["rooms_3"], 12_345)


class ResumeAccountingTest(unittest.TestCase):
    def test_partition_counts_ignore_unknown_retry_rows(self) -> None:
        frame = pd.DataFrame(
            {"scrape_partition": ["rooms_1", "rooms_1", "rooms_3", "retry", None]}
        )
        counts = partition_counts(frame)
        self.assertEqual(counts["rooms_1"], 2)
        self.assertEqual(counts["rooms_2"], 0)
        self.assertEqual(counts["rooms_3"], 1)

    def test_status_accepts_exhausted_small_inventory(self) -> None:
        self.assertEqual(partition_status(100, 100, False), "quota_met")
        self.assertEqual(partition_status(80, 100, True), "inventory_exhausted")
        self.assertEqual(partition_status(80, 100, False), "incomplete")


if __name__ == "__main__":
    unittest.main()
