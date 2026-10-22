from pathlib import Path
import tempfile
import unittest

from manager import conflicts
from manager.storage import create_library


class ConflictMarkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.lib = create_library(Path(self.tmp.name) / "Example library")

    def tearDown(self):
        self.tmp.cleanup()

    def test_clean_library_passes(self):
        self.assertEqual(conflicts.conflicted_files(self.lib.root), [])
        conflicts.check(self.lib.root)

    def test_committed_conflict_is_reported_by_file(self):
        csv = self.lib.appdata / "components.csv"
        csv.write_bytes(
            csv.read_bytes()
            + b"<<<<<<< Updated upstream\r\nA,1\r\n=======\r\nB,2\r\n>>>>>>> Stashed changes\r\n"
        )
        self.assertEqual(conflicts.conflicted_files(self.lib.root), ["appdata/components.csv"])
        with self.assertRaisesRegex(ValueError, "merge-conflict markers in: appdata/components.csv"):
            conflicts.check(self.lib.root)

    def test_binary_and_backup_files_are_ignored(self):
        (self.lib.appdata / "backups" / "old.zip").write_bytes(b"<<<<<<< x\n")
        (self.lib.appdata / "parts.sqlite").write_bytes(b"<<<<<<< x\n")
        self.assertEqual(conflicts.conflicted_files(self.lib.root), [])


if __name__ == "__main__":
    unittest.main()
