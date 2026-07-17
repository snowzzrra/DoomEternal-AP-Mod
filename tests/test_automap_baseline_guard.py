import unittest

from automap_baseline_guard import assert_separate_automap_helper_guard


class AutomapBaselineGuardTests(unittest.TestCase):
    def test_all_physical_locations_have_separate_marker_owners(self):
        self.assertEqual(assert_separate_automap_helper_guard(), 76)


if __name__ == "__main__":
    unittest.main()
