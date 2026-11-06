import sys
from pathlib import Path
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from timeline_checks import clock_interval, travel_feasibility


class TemporalConstraints(unittest.TestCase):
    def test_fast_clock_subtracts_and_crosses_day(self):
        interval = clock_interval("2025-01-02T00:02:00+08:00", clock_fast_seconds=300, uncertainty_seconds=30)
        self.assertEqual(interval["earliest"], "2025-01-01T23:56:30+08:00")
        self.assertEqual(interval["latest"], "2025-01-01T23:57:30+08:00")

    def test_travel_conflict_not_identity_determination(self):
        first = clock_interval("2025-01-01T10:08:00+08:00", clock_fast_seconds=0, uncertainty_seconds=10)
        second = clock_interval("2025-01-01T10:09:00+08:00", clock_fast_seconds=0, uncertainty_seconds=0)
        result = travel_feasibility(first, second, minimum_travel_seconds=120)
        self.assertEqual(result["status"], "impossible_under_assumptions")
        self.assertEqual(result["maximum_available_seconds"], 70)
        self.assertFalse(result["identity_established"])

    def test_uncertainty_can_allow_only_some_routes(self):
        first = clock_interval("2025-01-01T10:00:00+00:00", clock_fast_seconds=0, uncertainty_seconds=60)
        second = clock_interval("2025-01-01T18:03:00+08:00", clock_fast_seconds=0, uncertainty_seconds=0)
        result = travel_feasibility(first, second, minimum_travel_seconds=180)
        self.assertEqual(result["status"], "possible_for_some_times")
        self.assertEqual(travel_feasibility(first, second, minimum_travel_seconds=120)["status"], "feasible_for_all_times")

    def test_missing_clock_zone_and_invalid_bounds_rejected(self):
        for instant in ("2025-01-01T10:00:00", "10:00"):
            with self.assertRaises(ValueError):
                clock_interval(instant, clock_fast_seconds=0, uncertainty_seconds=1)
        with self.assertRaises(ValueError):
            clock_interval("2025-01-01T10:00:00+00:00", clock_fast_seconds=True, uncertainty_seconds=1)
        with self.assertRaises(ValueError):
            clock_interval("2025-01-01T10:00:00+00:00", clock_fast_seconds=0, uncertainty_seconds=-1)


if __name__ == "__main__":
    unittest.main()
