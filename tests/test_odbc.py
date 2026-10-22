import os
import unittest
from unittest.mock import patch, sentinel

from manager import odbc


class ODBCManagerTests(unittest.TestCase):
    loader = "WinDLL" if os.name == "nt" else "CDLL"

    def test_falls_back_when_a_manager_cannot_be_loaded(self):
        # KiCad.app's bundled libodbc can't be loaded outside KiCad (@rpath).
        with patch.object(
            odbc.C, self.loader,
            side_effect=[OSError("Library not loaded: @rpath/libltdl.7.dylib"), sentinel.dll],
        ):
            name, dll = odbc.load_manager(["bundled/libodbc.2.dylib", "homebrew/libodbc.2.dylib"])
        self.assertEqual((name, dll), ("homebrew/libodbc.2.dylib", sentinel.dll))

    def test_reports_every_failure_when_nothing_loads(self):
        with patch.object(odbc.C, self.loader, side_effect=OSError("missing")):
            with self.assertRaisesRegex(RuntimeError, "a.dylib: missing; b.dylib: missing"):
                odbc.load_manager(["a.dylib", "b.dylib"])

    def test_candidates_are_unique_and_present(self):
        candidates = odbc.manager_candidates()
        self.assertEqual(len(candidates), len(set(candidates)))
        if os.name == "nt":
            self.assertEqual(candidates, ["odbc32.dll"])


if __name__ == "__main__":
    unittest.main()
