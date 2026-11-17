import unittest

from nvidia_nspawn.common import Error
from nvidia_nspawn.native_execution import _unit_state


class UnitStateTests(unittest.TestCase):
    unit = "nvidia-nspawn-exec-0123456789abcdef.service"

    def test_collected_unit_has_no_state(self):
        self.assertIsNone(_unit_state("", self.unit))
        self.assertIsNone(_unit_state("\n  \n", self.unit))

    def test_reads_active_state_not_load_or_substate(self):
        for active, sub in (("active", "running"), ("deactivating", "stop-sigterm"),
                            ("failed", "failed"), ("inactive", "dead")):
            with self.subTest(active=active):
                output = f"  {self.unit} loaded {active} {sub} command with spaces\n"
                self.assertEqual(_unit_state(output, self.unit), active)

    def test_rejects_unexpected_or_incomplete_observations(self):
        for output in (f"{self.unit} loaded\n",
                       f"{self.unit}.other loaded inactive dead other\n",
                       "UNIT LOAD ACTIVE SUB DESCRIPTION\n",
                       f"{self.unit} loaded inactive dead one\n"
                       f"{self.unit} loaded active running two\n"):
            with self.subTest(output=output), self.assertRaises(Error):
                _unit_state(output, self.unit)
