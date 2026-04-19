import csv
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


class TestPhase6ReplayHarness(unittest.TestCase):
    def test_timing_bucket_uses_15_second_bins(self):
        from scripts.replay_harness import _timing_bucket

        self.assertEqual(_timing_bucket(150), "150-136s")
        self.assertEqual(_timing_bucket(136), "150-136s")
        self.assertEqual(_timing_bucket(15), "15-5s")
        self.assertEqual(_timing_bucket(5), "15-5s")
        self.assertEqual(_timing_bucket(151), "other")

    def test_walk_forward_day_blocks_split_3_train_2_test(self):
        from scripts.replay_harness import _walk_forward_day_blocks

        days = [
            "2026-01-01",
            "2026-01-02",
            "2026-01-03",
            "2026-01-04",
            "2026-01-05",
            "2026-01-06",
            "2026-01-07",
            "2026-01-08",
            "2026-01-09",
            "2026-01-10",
        ]
        blocks = _walk_forward_day_blocks(days, window_days=5)

        self.assertEqual(
            blocks,
            [
                (["2026-01-01", "2026-01-02", "2026-01-03"], ["2026-01-04", "2026-01-05"]),
                (["2026-01-06", "2026-01-07", "2026-01-08"], ["2026-01-09", "2026-01-10"]),
            ],
        )

    def test_longest_consecutive_day_streak(self):
        from scripts.replay_harness import _longest_consecutive_day_streak

        days = ["2026-01-01", "2026-01-02", "2026-01-04", "2026-01-05", "2026-01-06"]
        self.assertEqual(_longest_consecutive_day_streak(days), 3)

    def test_fetch_binance_candles_falls_back_to_existing_cache_when_offline(self):
        from scripts.replay_harness import _fetch_binance_candles

        with tempfile.TemporaryDirectory() as tmpdir:
            cache_path = Path(tmpdir) / "binance_1m.csv"
            with cache_path.open("w", newline="") as handle:
                writer = csv.writer(handle)
                writer.writerow(["open_time", "open", "high", "low", "close", "volume"])
                writer.writerow([1000, 1, 2, 0.5, 1.5, 10])
                writer.writerow([2000, 1.5, 2.5, 1.0, 2.0, 11])

            with patch("scripts.replay_harness.urllib.request.urlopen", side_effect=OSError("offline")):
                rows = _fetch_binance_candles("1m", 1000, 120000, cache_path)

        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["open_time"], "1000")


if __name__ == "__main__":
    unittest.main()
