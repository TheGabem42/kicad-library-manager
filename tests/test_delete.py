from pathlib import Path
import sqlite3
import tempfile
import unittest

from manager import delete
from manager.storage import create_library
import test_core


class DeleteComponentTests(unittest.TestCase):
    # reuse the import fixtures without re-running LibraryCase's own tests
    session = test_core.LibraryCase.session
    accessory = test_core.LibraryCase.accessory
    connector = test_core.LibraryCase.connector

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.lib = create_library(self.base / "Example library", install_runtime=False)

    def saved_connector(self, recipe=None):
        session, draft = self.connector()
        if recipe:
            draft["recipe"] = recipe
        return session.commit(self.lib, session.prepare(self.lib, draft)["token"])

    def test_delete_removes_catalog_row_files_and_index(self):
        part = self.saved_connector()
        folder = self.lib.root / part["AssetFolder"]
        plan = delete.plan(self.lib, part["ID"])
        self.assertEqual(plan["folders"], [part["AssetFolder"]])
        self.assertTrue(plan["files"])
        delete.delete(self.lib, part["ID"])
        self.assertNotIn(part["ID"], {r["ID"] for r in self.lib.rows()})
        self.assertFalse(folder.exists())
        self.assertTrue(folder.parent.is_dir())  # the category folder stays
        conn = sqlite3.connect(self.lib.appdata / "parts.sqlite")
        try:
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM components WHERE ID=?", (part["ID"],)
                ).fetchone()[0],
                0,
            )
        finally:
            conn.close()
        self.assertFalse((self.lib.appdata / "pending-transaction.json").exists())
        self.assertTrue(list((self.lib.appdata / "backups").glob("*.zip")))

    def test_accessories_only_this_part_used_are_removed_with_it(self):
        crimp = self.accessory()
        part = self.saved_connector({"items": [dict(partID=crimp["ID"], quantity=3)]})
        plan = delete.plan(self.lib, part["ID"])
        self.assertEqual(plan["accessories"], ["Test CRIMP"])
        delete.delete(self.lib, part["ID"])
        self.assertNotIn(crimp["ID"], {r["ID"] for r in self.lib.rows()})

    def test_accessory_still_used_by_a_part_is_protected(self):
        crimp = self.accessory()
        self.saved_connector({"items": [dict(partID=crimp["ID"], quantity=3)]})
        with self.assertRaisesRegex(ValueError, "accessory of"):
            delete.plan(self.lib, crimp["ID"])

    def test_missing_part_is_reported(self):
        with self.assertRaisesRegex(ValueError, "no longer exists"):
            delete.plan(self.lib, "does-not-exist")


if __name__ == "__main__":
    unittest.main()
