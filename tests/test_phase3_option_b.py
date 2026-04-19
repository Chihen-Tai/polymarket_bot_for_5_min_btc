import os
import tempfile
import unittest
from pathlib import Path


class TestPhase3OptionB(unittest.TestCase):
    def test_load_repo_env_reads_dotenv_files_without_python_dotenv(self):
        from core.config import load_repo_env

        keys = ["PHASE3_BASE_ONLY", "PHASE3_SHARED"]
        original = {key: os.environ.get(key) for key in keys}
        try:
            for key in keys:
                os.environ.pop(key, None)

            with tempfile.TemporaryDirectory() as tmpdir:
                root = Path(tmpdir)
                (root / ".env").write_text(
                    "PHASE3_BASE_ONLY=base\nPHASE3_SHARED=base\n",
                    encoding="utf-8",
                )
                (root / ".env.local").write_text(
                    "PHASE3_SHARED=override\n",
                    encoding="utf-8",
                )

                load_repo_env(root)

            self.assertEqual(os.environ.get("PHASE3_BASE_ONLY"), "base")
            self.assertEqual(os.environ.get("PHASE3_SHARED"), "override")
        finally:
            for key, value in original.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_static_vpn_threshold_blocks_high_rtt(self):
        from core.config import SETTINGS
        from core.latency_monitor import LatencyMonitor

        monitor = LatencyMonitor(history_size=10)
        original = (
            SETTINGS.vpn_safe_mode,
            SETTINGS.max_vpn_latency_ms,
            getattr(SETTINGS, "vpn_auto_calibrate_latency", False),
            SETTINGS.vpn_e2e_p50_block_ms,
            SETTINGS.vpn_e2e_jitter_block_ms,
        )
        try:
            SETTINGS.vpn_safe_mode = True
            SETTINGS.max_vpn_latency_ms = 600.0
            SETTINGS.vpn_auto_calibrate_latency = False
            SETTINGS.vpn_e2e_p50_block_ms = 2000.0
            SETTINGS.vpn_e2e_jitter_block_ms = 2000.0

            for rtt in [780.0, 800.0, 812.0, 830.0, 850.0]:
                monitor.add_rtt(rtt)

            is_blocked, reason = monitor.is_blocked()
            self.assertTrue(is_blocked)
            self.assertIn("rtt=", reason)
        finally:
            (
                SETTINGS.vpn_safe_mode,
                SETTINGS.max_vpn_latency_ms,
                SETTINGS.vpn_auto_calibrate_latency,
                SETTINGS.vpn_e2e_p50_block_ms,
                SETTINGS.vpn_e2e_jitter_block_ms,
            ) = original

    def test_option_b_calibrated_threshold_unblocks_same_rtt_sample(self):
        from core.config import SETTINGS
        from core.latency_monitor import LatencyMonitor

        monitor = LatencyMonitor(history_size=10)
        original = (
            SETTINGS.vpn_safe_mode,
            SETTINGS.max_vpn_latency_ms,
            getattr(SETTINGS, "vpn_auto_calibrate_latency", False),
            getattr(SETTINGS, "vpn_latency_multiplier", 1.2),
            getattr(SETTINGS, "vpn_latency_floor_ms", 900.0),
            SETTINGS.vpn_e2e_p50_block_ms,
            SETTINGS.vpn_e2e_jitter_block_ms,
        )
        try:
            SETTINGS.vpn_safe_mode = True
            SETTINGS.max_vpn_latency_ms = 900.0
            SETTINGS.vpn_auto_calibrate_latency = True
            SETTINGS.vpn_latency_multiplier = 1.2
            SETTINGS.vpn_latency_floor_ms = 900.0
            SETTINGS.vpn_e2e_p50_block_ms = 2000.0
            SETTINGS.vpn_e2e_jitter_block_ms = 2000.0

            for rtt in [780.0, 800.0, 812.0, 830.0, 850.0]:
                monitor.add_rtt(rtt)

            self.assertGreaterEqual(monitor.get_effective_max_vpn_latency_ms(), 900.0)
            is_blocked, _reason = monitor.is_blocked()
            self.assertFalse(is_blocked)
        finally:
            (
                SETTINGS.vpn_safe_mode,
                SETTINGS.max_vpn_latency_ms,
                SETTINGS.vpn_auto_calibrate_latency,
                SETTINGS.vpn_latency_multiplier,
                SETTINGS.vpn_latency_floor_ms,
                SETTINGS.vpn_e2e_p50_block_ms,
                SETTINGS.vpn_e2e_jitter_block_ms,
            ) = original


if __name__ == "__main__":
    unittest.main()
