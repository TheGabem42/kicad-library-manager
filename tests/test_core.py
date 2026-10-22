import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from manager.storage import create_library, Library, APPDATA, csv_bytes
from manager.importer import ImportSession, resolve_inline_accessories
from manager import cad, bom, kicad, supplier
from manager.sexpr import parse, children
from manager.preview import component_preview


FOOTPRINT = """(footprint "Header" (version 20241229) (generator pcbnew) (layer "F.Cu")
 (attr through_hole)
 (fp_line (start -2 -2) (end 5 -2) (stroke (width 0.12) (type default)) (layer "F.SilkS"))
 (pad "1" thru_hole circle (at 0 0) (size 1.8 1.8) (drill 1) (layers "*.Cu" "*.Mask"))
 (pad "2" thru_hole oval (at 2.54 0) (size 1.8 2) (drill 1) (layers "*.Cu" "*.Mask"))
 (pad "3" thru_hole circle (at 5.08 0) (size 1.8 1.8) (drill 1) (layers "*.Cu" "*.Mask"))
 (pad "" np_thru_hole circle (at -2.54 0) (size 1.2 1.2) (drill 1.2) (layers "*.Cu" "*.Mask"))
 (model "supplier.step" (offset (xyz 1 2 3)) (scale (xyz 1 1 1)) (rotate (xyz 10 20 30))))"""
SYMBOL = cad.symbol_library(cad.connector_symbol(3), "Header")


class LibraryCase(unittest.TestCase):
    def test_catalog_previews_preserve_library_and_model_transform(self):
        session, draft = self.connector()
        prepared = session.prepare(self.lib, draft)
        saved = session.commit(self.lib, prepared["token"])
        before = {str(path.relative_to(self.lib.root)): path.read_bytes()
                  for path in self.lib.root.rglob("*") if path.is_file()}
        for kind in ("symbol", "footprint"):
            result = component_preview(self.lib, saved["ID"], kind)
            self.assertTrue(result["svgs"])
            self.assertIn("<svg", result["svgs"][0])
        result = component_preview(self.lib, saved["ID"], "model")
        self.assertEqual(result["model"]["offset"], [1, 2, 3])
        self.assertEqual(result["model"]["rotate"], [10, 20, 30])
        self.assertIn(b"ISO-10303", component_preview(self.lib, saved["ID"], "step"))
        after = {str(path.relative_to(self.lib.root)): path.read_bytes()
                 for path in self.lib.root.rglob("*") if path.is_file()}
        self.assertEqual(before, after)

    def test_catalog_previews_without_cad(self):
        row = self.accessory()
        self.assertEqual(component_preview(self.lib, row["ID"], "symbol"), {"svgs": []})
        self.assertEqual(component_preview(self.lib, row["ID"], "footprint"), {"svgs": []})
        self.assertIsNone(component_preview(self.lib, row["ID"], "model")["model"])
        with self.assertRaises(ValueError):
            component_preview(self.lib, row["ID"], "step")
        with self.assertRaises(ValueError):
            component_preview(self.lib, "missing", "symbol")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.lib = create_library(self.base / "Example library", install_runtime=False)

    def session(self):
        s = ImportSession()
        self.addCleanup(s.close)
        return s

    def accessory(self, mpn="CRIMP"):
        # Historical catalog-only records remain usable by existing BOM recipes.
        rows = self.lib.rows()
        _, added = resolve_inline_accessories(
            {"items": [{"manufacturer": "Test", "mpn": mpn, "quantity": 1}]},
            rows, {"ID": "fixture-parent", "Manufacturer": "Test", "MPN": "fixture-parent"},
        )
        rows += added
        self.lib.transaction({APPDATA + "/components.csv": csv_bytes(rows), **self.lib.compiled(rows)})
        return added[0]

    def connector(self):
        s = self.session()
        s.upload("download/symbol.kicad_sym", SYMBOL.encode())
        s.upload("download/footprint.kicad_mod", FOOTPRINT.encode())
        model = (
            Path(__file__).resolve().parents[1]
            / "resources/generic_models/models-3d/R_0805_2012Metric.step"
        ).read_bytes()
        s.upload("download/header.step", model)
        inv = s.inventory()["assets"]
        key = lambda ext: next(x["id"] for x in inv if x["type"] == ext)
        draft = dict(
            mode="bespoke",
            part=dict(MPN="HDR-3", Manufacturer="Test", Type="connector"),
            symbolAsset=key(".kicad_sym"),
            symbolName="Header",
            footprintAsset=key(".kicad_mod"),
            modelAsset=key(".step"),
            category="components/connectors",
            alternative=True,
            layout="split",
            pinsConfirmed=True,
        )
        return s, draft

    def test_empty_portable_library_no_catalog_seeds(self):
        self.assertEqual(self.lib.rows(), [])
        self.assertEqual(
            len(
                cad.symbols(
                    (
                        self.lib.root
                        / "components/generic_models/symbols/Connectors.kicad_sym"
                    ).read_text()
                )
            ),
            48,
        )
        config = json.loads((self.lib.appdata / "parts.kicad_dbl").read_text())
        self.assertTrue(config["globally_unique_keys"])
        self.assertEqual(
            config["source"]["connection_string"],
            "DSN=VirtusSQLite;Database=${CWD}/parts.sqlite;",
        )

    def test_inference_uses_unique_numbers_and_excludes_npth(self):
        data = cad.footprint_data(FOOTPRINT)
        data["pads"].append(dict(data["pads"][0], index=9))
        inference = cad.infer_connector(
            data, cad.symbol_pins(cad.flatten_symbol(SYMBOL, "Header"))
        )
        self.assertEqual(inference["count"], 3)
        self.assertEqual(inference["mountingHoles"], 1)
        self.assertTrue(inference["eligible"])

    def test_shields_and_extra_tabs_block_generic(self):
        pins = cad.symbol_pins(cad.flatten_symbol(SYMBOL, "Header"))
        pins[2]["name"] = "SHIELD"
        self.assertFalse(
            cad.infer_connector(cad.footprint_data(FOOTPRINT), pins)["eligible"]
        )
        data = cad.footprint_data(FOOTPRINT)
        data["pads"].append(dict(data["pads"][0], number="MP"))
        self.assertFalse(cad.infer_connector(data)["eligible"])

    def test_import_saved_files_transform_recipe_and_archive(self):
        crimp = self.accessory()
        s, d = self.connector()
        d["recipe"] = {
            "items": [{"partID": crimp["ID"], "quantity": 3, "role": "crimp"}]
        }
        r = s.prepare(self.lib, d)
        saved = s.commit(self.lib, r["token"])
        self.assertEqual(self.lib.recipe(saved)["items"][0]["quantity"], 3)
        fp = cad.footprint_data((self.lib.root / saved["FootprintFile"]).read_text())
        self.assertEqual(fp["models"][0]["offset"], [1, 2, 3])
        self.assertEqual(fp["models"][0]["rotate"], [10, 20, 30])
        self.assertTrue(
            fp["models"][0]["path"].startswith(
                "${MYCO_PARTS_DIR}/../components/connectors/"
            )
        )
        original_pads = children(parse(FOOTPRINT), "pad")
        saved_pads = children(
            parse((self.lib.root / saved["FootprintFile"]).read_text()), "pad"
        )
        self.assertEqual(original_pads, saved_pads)
        with zipfile.ZipFile(
            next((self.lib.appdata / "original-downloads").glob("*.zip"))
        ) as z:
            self.assertEqual(
                z.read("originals/download/footprint.kicad_mod"), FOOTPRINT.encode()
            )
        conn = sqlite3.connect(self.lib.appdata / "parts.sqlite")
        try:
            self.assertIn(
                "/connectors/HDR-3",
                conn.execute(
                    "SELECT SearchPath FROM components WHERE ID=?", (saved["ID"],)
                ).fetchone()[0],
            )
        finally:
            conn.close()

    def test_replacement_retains_id_and_requires_explicit_choice(self):
        s, d = self.connector()
        first = s.commit(self.lib, s.prepare(self.lib, d)["token"])
        d["part"]["Description"] = "Updated"
        r = s.prepare(self.lib, d)
        self.assertTrue(r["replacing"])
        with self.assertRaisesRegex(ValueError, "Confirm replacement"):
            s.commit(self.lib, r["token"])
        saved = s.commit(self.lib, r["token"], replace=True)
        self.assertEqual(first["ID"], saved["ID"])
        self.assertEqual(len(self.lib.rows()), 1)
        self.assertGreaterEqual(
            len(list((self.lib.appdata / "backups").glob("*.zip"))), 2
        )

    def test_stale_review_cannot_overwrite(self):
        s, d = self.connector()
        r = s.prepare(self.lib, d)
        self.accessory()
        with self.assertRaisesRegex(ValueError, "changed since review"):
            s.commit(self.lib, r["token"])
        self.assertEqual(len(self.lib.rows()), 1)

    def test_external_asset_changes_invalidate_review(self):
        s, d = self.connector()
        row = s.commit(self.lib, s.prepare(self.lib, d)["token"])
        edit = self.session()
        loaded = edit.load_existing(self.lib, row["ID"])
        reviewed = edit.prepare(self.lib, loaded["draft"])
        path = self.lib.root / row["FootprintFile"]
        path.write_text(path.read_text() + "\n")
        with self.assertRaisesRegex(ValueError, "file changed since review"):
            edit.commit(self.lib, reviewed["token"], True)

    def test_edit_and_category_move_preserve_identity_recipe_and_extra_files(self):
        crimp = self.accessory()
        s, d = self.connector()
        d["recipe"] = {"items": [dict(partID=crimp["ID"], quantity=3)]}
        row = s.commit(self.lib, s.prepare(self.lib, d)["token"])
        old = self.lib.root / row["AssetFolder"]
        (old / "supplier-note.txt").write_text("Keep this note")
        edit = self.session()
        loaded = edit.load_existing(self.lib, row["ID"])
        draft = loaded["draft"]
        draft["category"] = "components/connectors/board headers"
        reviewed = edit.prepare(self.lib, draft)
        updated = edit.commit(self.lib, reviewed["token"], True)
        self.assertEqual(row["ID"], updated["ID"])
        self.assertEqual(row["Symbol"], updated["Symbol"])
        self.assertEqual(
            (self.lib.root / updated["AssetFolder"] / "supplier-note.txt").read_text(),
            "Keep this note",
        )
        self.assertEqual(self.lib.recipe(updated)["items"][0]["quantity"], 3)
        self.assertFalse((old / "component.json").exists())
        self.assertEqual(
            len(list((self.lib.appdata / "original-downloads").glob("*.zip"))), 1
        )

    def test_transaction_rolls_back_catalog_and_new_assets(self):
        s, d = self.connector()
        r = s.prepare(self.lib, d)
        before = (self.lib.appdata / "components.csv").read_bytes()
        replace = self.lib._replace
        failed = [False]

        def fail_once(path, data):
            if path.name == "parts.sqlite" and not failed[0]:
                failed[0] = True
                raise OSError("injected write error")
            return replace(path, data)

        with patch.object(self.lib, "_replace", side_effect=fail_once):
            with self.assertRaisesRegex(OSError, "injected"):
                s.commit(self.lib, r["token"])
        self.assertEqual((self.lib.appdata / "components.csv").read_bytes(), before)
        self.assertFalse((self.lib.root / r["part"]["SymbolFile"]).exists())
        self.assertFalse((self.lib.appdata / "pending-transaction.json").exists())

    def test_save_while_database_is_open_elsewhere(self):
        # Windows can't replace a file another process (KiCad via ODBC) has open.
        s, d = self.connector()
        r = s.prepare(self.lib, d)
        real_replace = os.replace

        def windows_replace(src, dst):
            if str(dst).endswith("parts.sqlite"):
                raise PermissionError(13, "Access is denied", str(dst))
            return real_replace(src, dst)

        with patch("manager.storage.os.replace", side_effect=windows_replace):
            saved = s.commit(self.lib, r["token"])
        conn = sqlite3.connect(self.lib.appdata / "parts.sqlite")
        try:
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM components WHERE ID=?", (saved["ID"],)
                ).fetchone()[0],
                1,
            )
        finally:
            conn.close()
        self.assertFalse((self.lib.appdata / "pending-transaction.json").exists())

    def test_zip_traversal_and_duplicate_members_do_not_add_files(self):
        s = self.session()
        b = io.BytesIO()
        with zipfile.ZipFile(b, "w") as z:
            z.writestr("../bad.kicad_mod", FOOTPRINT)
        with self.assertRaises(ValueError):
            s.upload("bad.zip", b.getvalue())
        self.assertEqual(s.assets, {})
        self.assertEqual(s.originals, {})

    def test_generic_passives_share_assets(self):
        for mpn in ("R-A", "R-B"):
            s = self.session()
            d = dict(
                mode="generic",
                part=dict(
                    Type="resistor",
                    MPN=mpn,
                    Manufacturer="Test",
                    Value="10k",
                    Tolerance="1%",
                    Package="0805",
                    Power_W="0.125",
                ),
                category="components/resistors",
            )
            s.commit(self.lib, s.prepare(self.lib, d)["token"])
        a, b = self.lib.rows()
        self.assertEqual(a["Symbol"], b["Symbol"])
        self.assertEqual(a["FootprintFile"], b["FootprintFile"])
        self.assertEqual(a["AssetFolder"], "")
        self.assertEqual(b["AssetFolder"], "")
        self.assertEqual(list((self.lib.root / "components/resistors").iterdir()), [])

    def test_passive_unit_and_identity_validation(self):
        from manager.passives import package_code

        self.assertEqual(package_code("1608 Metric"), "0603")
        self.assertEqual(package_code("0603 Metric"), "")
        s = self.session()
        draft = dict(
            mode="generic",
            part=dict(
                Type="resistor",
                MPN="R-A",
                Manufacturer="Test",
                Value="",
                Tolerance="1%",
                Package="0805",
                Power_W="0.125",
            ),
            category="components/resistors",
        )
        with self.assertRaisesRegex(ValueError, "numeric passive"):
            s.prepare(self.lib, draft)
        draft["part"]["Value"] = "4k7"
        draft["part"]["Power_W"] = "1/8 W"
        reviewed = s.prepare(self.lib, draft)
        self.assertEqual(reviewed["part"]["Value"], "4.7k")
        self.assertEqual(reviewed["part"]["Power_W"], "0.125")
        draft["part"]["Terminations"] = "4"
        with self.assertRaisesRegex(ValueError, "multi-terminal"):
            s.prepare(self.lib, draft)

    def test_passive_step_only_import_and_edit_preserve_shared_symbol(self):
        s = self.session()
        model = (
            self.lib.root / "components/generic_models/models-3d/R_0805_2012Metric.step"
        )
        s.upload("custom.step", model.read_bytes())
        d = dict(
            mode="bespoke",
            part=dict(
                Type="resistor",
                MPN="CUSTOM-R",
                Manufacturer="Test",
                Value="10k",
                Package="0805",
                Tolerance="1%",
                Power_W="0.125",
            ),
            modelAsset=s.inventory()["assets"][0]["id"],
            category="components/resistors",
            transform=dict(offset=[1, 2, 3], rotate=[0, 0, 90], scale=[1, 1, 1]),
        )
        row = s.commit(self.lib, s.prepare(self.lib, d)["token"])
        self.assertTrue(row["SymbolFile"].startswith("components/generic_models/"))
        self.assertTrue(row["FootprintFile"].startswith(row["AssetFolder"] + "/"))
        original = cad.footprint_data(
            (self.lib.root / row["FootprintFile"]).read_text()
        )
        edit = self.session()
        loaded = edit.load_existing(self.lib, row["ID"])["draft"]
        self.assertEqual(loaded["mode"], "bespoke")
        loaded["part"]["Description"] = "Edited custom model"
        changed = edit.commit(self.lib, edit.prepare(self.lib, loaded)["token"], True)
        self.assertEqual(changed["ID"], row["ID"])
        self.assertEqual(changed["SymbolFile"], row["SymbolFile"])
        saved = cad.footprint_data(
            (self.lib.root / changed["FootprintFile"]).read_text()
        )
        self.assertEqual(saved["models"], original["models"])

    def test_shared_passive_symbol_rejects_three_terminal_footprint(self):
        s = self.session()
        s.upload("wrong.kicad_mod", FOOTPRINT.encode())
        d = dict(
            mode="bespoke",
            part=dict(
                Type="resistor",
                MPN="CUSTOM-R",
                Manufacturer="Test",
                Value="10k",
                Package="0805",
                Tolerance="1%",
                Power_W="0.125",
            ),
            footprintAsset=s.inventory()["assets"][0]["id"],
            category="components/resistors",
        )
        with self.assertRaisesRegex(ValueError, "requires pads 1 and 2"):
            s.prepare(self.lib, d)

    def test_interrupted_transaction_restores_exact_before_bytes(self):
        catalog = self.lib.appdata / "components.csv"
        before = catalog.read_bytes()
        created = self.lib.root / "components/interrupted.txt"
        real_replace = self.lib._replace

        def interrupted(path, data):
            real_replace(path, data)
            if path == catalog:
                raise KeyboardInterrupt("Simulate interrupted process")

        with patch.object(self.lib, "_replace", side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt):
                self.lib.transaction(
                    {
                        "components/interrupted.txt": b"new",
                        APPDATA + "/components.csv": b"incomplete",
                    }
                )
        self.assertTrue(self.lib.state()["recoveryNeeded"])
        self.assertTrue(created.exists())
        self.lib.recover()
        self.assertEqual(catalog.read_bytes(), before)
        self.assertFalse(created.exists())
        self.assertFalse(self.lib.state()["recoveryNeeded"])

    def test_library_relocation_has_no_absolute_asset_links(self):
        s, d = self.connector()
        s.commit(self.lib, s.prepare(self.lib, d)["token"])
        moved = self.base / "Moved elsewhere"
        shutil.copytree(self.lib.root, moved)
        other = Library(moved)
        for r in other.rows():
            self.assertTrue((moved / r["SymbolFile"]).exists())
            self.assertTrue((moved / r["FootprintFile"]).exists())
        self.assertEqual(other.id, self.lib.id)
        self.assertNotIn(
            str(self.lib.root), (moved / APPDATA / "sym-lib-table").read_text()
        )

    def test_manual_folder_move_repairs_paths_and_preserves_recipe(self):
        s, d = self.connector()
        d["recipe"] = {
            "items": [
                {
                    "manufacturer": "Test",
                    "mpn": "MATE",
                    "quantity": 2,
                    "role": "housing",
                }
            ]
        }
        header = s.commit(self.lib, s.prepare(self.lib, d)["token"])
        old = self.lib.root / header["AssetFolder"]
        moved = self.lib.root / "components/connectors/board headers/My asset folder"
        moved.parent.mkdir(parents=True)
        shutil.move(old, moved)
        state = self.lib.state()
        self.assertEqual(state["pathIssues"], [])
        row = next(r for r in self.lib.rows() if r["ID"] == header["ID"])
        self.assertEqual(row["Category"], "components/connectors/board headers")
        self.assertTrue(
            row["FootprintFile"].startswith(
                "components/connectors/board headers/My asset folder/"
            )
        )
        fp = cad.footprint_data((self.lib.root / row["FootprintFile"]).read_text())
        self.assertIn("/board headers/My asset folder/", fp["models"][0]["path"])
        self.assertEqual(fp["models"][0]["offset"], [1, 2, 3])
        self.assertEqual(self.lib.recipe(row)["items"][0]["quantity"], 2)
        self.assertEqual(
            next(r for r in state["parts"] if r["ID"] == row["ID"])["SearchPath"],
            "Example library/components/connectors/board headers/HDR-3",
        )

    def test_duplicate_manual_copy_blocks_reconciliation_and_save(self):
        s, d = self.connector()
        header = s.commit(self.lib, s.prepare(self.lib, d)["token"])
        before = (self.lib.appdata / "components.csv").read_bytes()
        shutil.copytree(
            self.lib.root / header["AssetFolder"],
            self.lib.root / "components/connectors/copy",
        )
        state = self.lib.state()
        self.assertIn("duplicate component", " ".join(state["pathIssues"]))
        self.assertEqual(before, (self.lib.appdata / "components.csv").read_bytes())
        with self.assertRaisesRegex(ValueError, "folder issues"):
            s.prepare(self.lib, d)

    def test_inline_accessories_commit_atomically_and_reuse_manufacturer_mpn(self):
        s, d = self.connector()
        d["recipe"] = {
            "items": [{"manufacturer": "Test", "mpn": "HOUSING", "quantity": 1}]
        }
        r = s.prepare(self.lib, d)
        self.assertEqual(self.lib.rows(), [])
        row = s.commit(self.lib, r["token"])
        self.assertEqual(len(self.lib.rows()), 2)
        accessory = next(r for r in self.lib.rows() if r["Type"] == "accessory")
        self.assertEqual(accessory["AssetFolder"], "")
        self.assertEqual(self.lib.recipe(row)["items"][0]["partID"], accessory["ID"])
        d["part"]["MPN"] = "HDR-OTHER"
        s.commit(self.lib, s.prepare(self.lib, d)["token"])
        self.assertEqual(len(self.lib.rows()), 3)
        self.assertEqual(list((self.lib.root / "components/accessories").iterdir()), [])

    def test_catalog_only_recipe_changes_invalidate_pending_review(self):
        a = self.accessory("A")
        b = self.accessory("B")
        accessory_edit = self.session()
        draft = accessory_edit.load_existing(self.lib, a["ID"])["draft"]
        draft["recipe"] = {"items": [{"partID": b["ID"], "quantity": 1}]}
        a = accessory_edit.commit(
            self.lib, accessory_edit.prepare(self.lib, draft)["token"], True
        )
        s, d = self.connector()
        review = s.prepare(self.lib, d)
        path = self.lib.root / self.lib.recipe_path(a)
        recipe = json.loads(path.read_text())
        recipe["items"][0]["quantity"] = 2
        path.write_text(json.dumps(recipe))
        with self.assertRaisesRegex(ValueError, "changed since review"):
            s.commit(self.lib, review["token"])

    def test_legacy_model_at_uses_inches_without_moving_pads(self):
        legacy = FOOTPRINT.replace("(offset (xyz 1 2 3))", "(at (xyz 0.1 0.2 0.3))")
        model = cad.footprint_data(legacy)["models"][0]
        for actual, expected in zip(model["offset"], [2.54, 5.08, 7.62]):
            self.assertAlmostEqual(actual, expected)

    def test_recipe_cycles_and_unknown_parts_fail(self):
        a = self.accessory("A")
        b = self.accessory("B")
        s = self.session()
        d = dict(
            mode="accessory",
            editingID=a["ID"],
            part=dict(MPN="A", Manufacturer="Test", Type="accessory"),
            recipe={"items": [dict(partID=b["ID"], quantity=1)]},
            category="components/accessories",
        )
        s.commit(self.lib, s.prepare(self.lib, d)["token"], True)
        d["part"]["MPN"] = "B"
        d["editingID"] = b["ID"]
        d["recipe"]["items"][0]["partID"] = a["ID"]
        with self.assertRaisesRegex(ValueError, "cycle"):
            s.prepare(self.lib, d)

    def test_bom_quantities_and_dnp(self):
        crimp = self.accessory()
        s, d = self.connector()
        d["recipe"] = {"items": [dict(partID=crimp["ID"], quantity=3)]}
        header = s.commit(self.lib, s.prepare(self.lib, d)["token"])
        instances = [
            dict(
                ref=f"J{i}", partID=header["ID"], libraryID=self.lib.id, accessoryOf=""
            )
            for i in (1, 2)
        ]
        result = bom.expand(self.lib, instances)
        self.assertEqual(
            {r["MPN"]: r["Quantity"] for r in result["purchasing"]},
            {"HDR-3": 2, "CRIMP": 6},
        )
        self.assertTrue(any("J2" in r["Reason"] for r in result["lines"]))
        xml = self.base / "bom.xml"
        xml.write_text(
            '<export><components><comp ref="R1"><property name="dnp" value="1"/></comp><comp ref="R2"><value>10k</value></comp></components></export>'
        )
        parsed, _ = bom.read_design(xml)
        self.assertEqual([x["ref"] for x in parsed], ["R2"])

    def test_explicit_accessory_not_double_counted_and_mismatch_rejected(self):
        accessory = self.accessory()
        s, d = self.connector()
        d["recipe"] = {"items": [dict(partID=accessory["ID"], quantity=1)]}
        header = s.commit(self.lib, s.prepare(self.lib, d)["token"])
        instances = [
            dict(ref="J1", partID=header["ID"], libraryID=self.lib.id, accessoryOf=""),
            dict(
                ref="A1",
                partID=accessory["ID"],
                libraryID=self.lib.id,
                accessoryOf="J1",
            ),
        ]
        result = bom.expand(self.lib, instances)
        self.assertEqual(
            next(r["Quantity"] for r in result["purchasing"] if r["MPN"] == "CRIMP"), 1
        )
        instances.append(dict(instances[1], ref="A2"))
        with self.assertRaisesRegex(ValueError, "count does not match"):
            bom.expand(self.lib, instances)

    def test_setup_only_writes_supplied_config_and_preserves_unrelated(self):
        config = self.base / "isolated-config"
        config.mkdir()
        (config / "kicad_common.json").write_text(
            json.dumps({"unrelated": {"value": 42}})
        )
        kicad.connect(self.lib, config=config, check_running=False)
        self.assertEqual(
            json.loads((config / "kicad_common.json").read_text())["unrelated"],
            {"value": 42},
        )
        self.assertTrue(kicad.setup_status(self.lib, config)["connected"])
        self.assertTrue(list(config.glob("*.bak")))

    def test_setup_accepts_null_kicad_path_variables(self):
        # A fresh KiCad (seen on Windows) writes "vars": null when no path variables exist.
        for environment in ({"vars": None}, None):
            config = self.base / f"null-vars-{environment is None}"
            config.mkdir()
            (config / "kicad_common.json").write_text(
                json.dumps({"environment": environment, "unrelated": 1})
            )
            self.assertFalse(kicad.setup_status(self.lib, config)["connected"])
            kicad.connect(self.lib, config=config, check_running=False)
            data = json.loads((config / "kicad_common.json").read_text())
            self.assertEqual(
                data["environment"]["vars"]["MYCO_PARTS_DIR"], str(self.lib.appdata)
            )
            self.assertEqual(data["unrelated"], 1)
            self.assertTrue(kicad.setup_status(self.lib, config)["connected"])

    def test_supplier_bespoke_clipboard(self):
        result = supplier.extract(
            "Manufacturer: Acme\nManufacturer Part Number: OP123\nDescription: Precision amplifier\nMouser Part #: 123-OP123\n"
        )
        self.assertEqual(result["MPN"], "OP123")
        self.assertEqual(result["Manufacturer"], "Acme")

    @unittest.skipUnless(kicad.cli_path(), "KiCad CLI unavailable")
    def test_actual_kicad_parses_saved_symbol_and_footprint(self):
        s, d = self.connector()
        r = s.prepare(self.lib, d)
        saved = s.commit(self.lib, r["token"])
        svg = kicad.render(
            "sym", self.lib.root / saved["SymbolFile"], saved["Symbol"].split(":")[1]
        )
        fp = kicad.render("fp", (self.lib.root / saved["FootprintFile"]).parent)
        self.assertIn("<svg", svg[0])
        self.assertIn("<svg", fp[0])


if __name__ == "__main__":
    unittest.main()
