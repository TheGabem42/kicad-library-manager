"""Versioned library layout, authoritative CSV, and journaled local writes."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import tempfile
import time
import uuid
import zipfile
from contextlib import contextmanager

from .sexpr import Quoted as Q, dump

APPDATA = "appdata"
COLUMNS = [
    "ID",
    "Manufacturer",
    "MPN",
    "Value",
    "Description",
    "Type",
    "Category",
    "AssetFolder",
    "Symbol",
    "Footprint",
    "SymbolFile",
    "FootprintFile",
    "Datasheet",
    "SupplierURL",
    "Distributor_PN",
    "Package",
    "Tolerance",
    "Voltage_VDC",
    "Current_A",
    "Voltage_V",
    "Power_W",
    "Dielectric",
    "Reference",
    "Status",
    "Notes",
    "UnitPrice",
    "PriceCurrency",
    "PriceQuantity",
    "PriceCheckedUTC",
    "CreatedUTC",
    "UpdatedUTC",
]
SOURCE_ROOT = Path(__file__).resolve().parent.parent


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def slug(text):
    text = re.sub(r"[^\w .()-]+", "-", text, flags=re.UNICODE).strip(" .-")
    if (
        not text
        or len(text) > 100
        or text.split(".")[0].upper()
        in {
            "CON",
            "PRN",
            "AUX",
            "NUL",
            *[f"COM{i}" for i in range(1, 10)],
            *[f"LPT{i}" for i in range(1, 10)],
        }
    ):
        raise ValueError("Use a nonempty, portable name of at most 100 characters")
    return text


def relative(text):
    p = PurePosixPath(str(text).replace("\\", "/"))
    if (
        p.is_absolute()
        or ".." in p.parts
        or not p.parts
        or any(":" in x or "\x00" in x for x in p.parts)
    ):
        raise ValueError("Path must stay inside the library")
    return p.as_posix()


def category_path(text):
    """Accept a category relative to components, with or without its prefix."""
    path = relative(str(text).strip())
    segments = path.split("/")
    if segments[0].casefold() == "components":
        segments[0] = "components"
    else:
        segments.insert(0, "components")
    if len(segments) > 1 and segments[1].casefold() in {"generic_models", "logos"}:
        raise ValueError("Shared model and logo folders are reserved; choose a component category")
    for segment in segments:
        if slug(segment) != segment:
            raise ValueError("Use portable category names without reserved characters")
    return "/".join(segments)


def inside(root, text):
    p = root / relative(text)
    if not p.resolve().is_relative_to(root.resolve()):
        raise ValueError("Path leaves the selected library")
    if any(q.is_symlink() for q in [p, *p.parents] if q != root.parent):
        raise ValueError("Library paths must not use symlinks")
    return p


def csv_bytes(rows):
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=COLUMNS)
    writer.writeheader()
    writer.writerows({c: r.get(c, "") for c in COLUMNS} for r in rows)
    return stream.getvalue().encode("utf-8")


def timestamp():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Library:
    def __init__(self, path):
        self.root = Path(path).expanduser().resolve()
        if ";" in str(self.root):
            raise ValueError(
                "The SQLite ODBC connection does not support semicolons in the library path"
            )
        self.appdata = self.root / APPDATA
        marker = self.appdata / "library.json"
        if not marker.is_file():
            raise ValueError(
                "Select a Library Manager folder containing appdata/library.json. Existing importer libraries must be migrated into a new folder explicitly."
            )
        self.manifest = json.loads(marker.read_text())
        if self.manifest.get("formatVersion") != 2:
            raise ValueError("Unsupported library format version")
        if ":" in self.root.name or "/" in self.root.name:
            raise ValueError("Library folder name is not a valid KiCad nickname")

    @property
    def name(self):
        return self.root.name

    @property
    def id(self):
        return self.manifest["id"]

    @property
    def prefix(self):
        return "LM_" + self.id.replace("-", "")[:10]

    def revision(self):
        data = (self.appdata / "components.csv").read_bytes()
        recipe_files = list((self.root / "components").rglob("recipes.json"))
        recipe_files.extend((self.appdata / "recipes").glob("*.json"))
        for p in sorted(recipe_files):
            data += p.relative_to(self.root).as_posix().encode() + p.read_bytes()
        return digest(data)

    def rows(self):
        with (self.appdata / "components.csv").open(
            encoding="utf-8-sig", newline=""
        ) as stream:
            reader = csv.DictReader(stream)
            # The original v2 catalog predates optional connector ratings.
            # Accept only that exact historical schema; opening stays read-only.
            legacy_columns = [c for c in COLUMNS if c not in ("Current_A", "Voltage_V")]
            if reader.fieldnames not in (COLUMNS, legacy_columns):
                raise ValueError(
                    "Unexpected catalog header; no automatic rewrites performed"
                )
            rows = list(reader)
            for row in rows:
                row.setdefault("Current_A", "")
                row.setdefault("Voltage_V", "")
        ids = [r["ID"] for r in rows]
        if len(ids) != len(set(ids)) or any(not x for x in ids):
            raise ValueError("Catalog contains missing or duplicate IDs")
        return rows

    def recipe_path(self, row):
        return (
            row["AssetFolder"] + "/recipes.json"
            if row["AssetFolder"]
            else APPDATA + "/recipes/" + digest(row["ID"].encode())[:24] + ".json"
        )

    def recipe(self, row):
        if row["AssetFolder"] and not inside(self.root, row["AssetFolder"]).is_dir():
            raise ValueError(
                row["MPN"]
                + ": component folder moved or missing. Open or Refresh the manager before exporting a BOM."
            )
        path = inside(self.root, self.recipe_path(row))
        return (
            json.loads(path.read_text())
            if path.exists()
            else {"version": 1, "items": []}
        )

    def categories(self):
        found = []
        for base in ("components",):
            start = self.root / base
            for folder, dirs, _ in os.walk(start):
                p = Path(folder)
                if (p / "component.json").exists():
                    dirs[:] = []
                    continue
                dirs[:] = [
                    d
                    for d in dirs
                    if not d.endswith(".pretty")
                    and not d.startswith(".")
                    and d
                    not in ("generic_models", "symbols", "footprints", "models-3d")
                ]
                found.append(p.relative_to(self.root).as_posix())
        return sorted(set(found))

    def state(self):
        recovery = (self.appdata / "pending-transaction.json").exists() or (
            self.appdata / ".writer.lock"
        ).exists()
        if recovery:
            # A failed save may have left catalog/recipe bytes unreadable. Keep
            # recovery reachable without parsing any partially installed data.
            return dict(
                path=str(self.root),
                name=self.name,
                id=self.id,
                revision="",
                parts=[],
                categories=[],
                recoveryNeeded=True,
            )
        issues = self.reconcile_folders()
        rows = self.rows()
        return dict(
            path=str(self.root),
            name=self.name,
            id=self.id,
            revision=self.revision(),
            parts=[
                dict(
                    r,
                    SearchPath=f"{self.name}/{r['Category']}/{r['MPN']}",
                    recipe=self.recipe(r)
                    if not issues
                    else {"version": 1, "items": []},
                )
                for r in rows
            ],
            categories=self.categories(),
            recoveryNeeded=False,
            pathIssues=issues,
        )

    def reconcile_folders(self):
        """Find intact component folders by identity, then update paths together."""
        if (self.appdata / "pending-transaction.json").exists() or (
            self.appdata / ".writer.lock"
        ).exists():
            return [
                "Library is locked; finish or recover the other save before reconciling folders."
            ]
        revision = self.revision()
        rows = self.rows()
        locations = {}
        issues = []
        fingerprints = {}
        for marker in (self.root / "components").rglob("component.json"):
            try:
                inside(self.root, marker.relative_to(self.root).as_posix())
                raw = marker.read_bytes()
                identity = json.loads(raw)
                if identity.get("libraryID") != self.id:
                    issues.append(
                        "Foreign component folder: "
                        + marker.parent.relative_to(self.root).as_posix()
                    )
                    continue
                locations.setdefault(identity.get("partID"), []).append(marker.parent)
                fingerprints[marker.relative_to(self.root).as_posix()] = digest(raw)
            except (ValueError, OSError) as exc:
                issues.append(
                    "Unreadable component marker: "
                    + str(marker)
                    + " ("
                    + str(exc)
                    + ")"
                )
        moves = {}
        changed = []
        for row in rows:
            if not row["AssetFolder"]:
                continue
            found = locations.get(row["ID"], [])
            if len(found) != 1:
                issues.append(
                    row["MPN"]
                    + (
                        ": duplicate component folders; keep one authoritative copy."
                        if found
                        else ": component folder or component.json is missing."
                    )
                )
                continue
            new = found[0].relative_to(self.root).as_posix()
            old = row["AssetFolder"]
            updated = dict(row)
            if new != old:
                updated.update(
                    AssetFolder=new,
                    Category=found[0].parent.relative_to(self.root).as_posix(),
                )
                moves[old] = new
                for field in ("SymbolFile", "FootprintFile"):
                    if updated[field].startswith(old + "/"):
                        updated[field] = new + updated[field][len(old) :]
            for field in ("SymbolFile", "FootprintFile"):
                if updated[field] and not inside(self.root, updated[field]).is_file():
                    issues.append(
                        row["MPN"]
                        + ": missing "
                        + field
                        + "; move the complete component folder, not individual files."
                    )
            changed.append(updated)
        known = {row["ID"] for row in rows}
        for ident in locations.keys() - known:
            issues.append("Uncatalogued component folder: " + str(ident))
        if issues or not moves:
            return issues
        changed_by_id = {r["ID"]: r for r in changed}
        updated_rows = [changed_by_id.get(r["ID"], r) for r in rows]
        writes = {}
        from .sexpr import parse, children

        for row in updated_rows:
            if not row["FootprintFile"]:
                continue
            path = inside(self.root, row["FootprintFile"])
            original = path.read_bytes()
            node = parse(original.decode("utf-8-sig"))
            altered = False
            for model in children(node, "model"):
                for old, new in sorted(moves.items(), key=lambda pair: -len(pair[0])):
                    prefix = "${MYCO_PARTS_DIR}/../" + old + "/"
                    if str(model[1]).startswith(prefix):
                        model[1] = Q(
                            "${MYCO_PARTS_DIR}/../"
                            + new
                            + "/"
                            + str(model[1])[len(prefix) :]
                        )
                        altered = True
                        break
            if altered:
                writes[row["FootprintFile"]] = (dump(node) + "\n").encode()
                fingerprints[row["FootprintFile"]] = digest(original)
        writes[APPDATA + "/components.csv"] = csv_bytes(updated_rows)
        writes.update(self.compiled(updated_rows, writes, fingerprints))
        self.transaction(writes, revision, expected_files=fingerprints)
        return []

    @contextmanager
    def locked(self, expected=None):
        if (self.appdata / "pending-transaction.json").exists():
            raise ValueError("An interrupted transaction needs recovery before editing")
        lock = self.appdata / ".writer.lock"
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            raise ValueError(
                "This library has an active or interrupted writer. Close the other editor or inspect the lock before recovery."
            )
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(
                    dict(
                        pid=os.getpid(),
                        host=__import__("socket").gethostname(),
                        time=timestamp(),
                    ),
                    f,
                )
            if expected is not None and expected != self.revision():
                raise ValueError(
                    "The library changed since review. Reload before saving."
                )
            yield
        finally:
            lock.unlink(missing_ok=True)

    def transaction(self, writes, expected=None, deletes=(), expected_files=None):
        with self.locked(expected):
            for name, expected_hash in (expected_files or {}).items():
                path = inside(self.root, name)
                actual = digest(path.read_bytes()) if path.is_file() else None
                if actual != expected_hash:
                    raise ValueError("Library file changed since review: " + name)
            self._transaction(writes, deletes)

    def _transaction(self, writes, deletes=()):
        # Generated category files are projections, never working CAD. Remove
        # stale projections in the same backed-up transaction as their indexes.
        if APPDATA + "/category-libraries.json" in writes:
            stale = [
                p.relative_to(self.root).as_posix()
                for p in (self.appdata / "generated/categories").rglob("*.kicad_mod")
                if p.relative_to(self.root).as_posix() not in writes
            ]
            deletes = list(dict.fromkeys([*deletes, *stale]))
        stamp = time.strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8]
        backup = self.appdata / "backups" / f"{stamp}.zip"
        backup.parent.mkdir(parents=True, exist_ok=True)
        targets = list(dict.fromkeys([*writes, *deletes]))
        old = {
            name: inside(self.root, name).read_bytes()
            if inside(self.root, name).is_file()
            else None
            for name in targets
        }
        with zipfile.ZipFile(backup, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr(
                "transaction.json",
                json_bytes(
                    {
                        "targets": targets,
                        "new": [n for n, d in old.items() if d is None],
                    }
                ),
            )
            for name, data in old.items():
                if data is not None:
                    z.writestr("before/" + name, data)
        journal = self.appdata / "pending-transaction.json"
        journal.write_bytes(
            json_bytes({"backup": backup.relative_to(self.root).as_posix()})
        )
        try:
            for name, data in writes.items():
                self._replace(inside(self.root, name), data)
            for name in deletes:
                inside(self.root, name).unlink(missing_ok=True)
        except Exception:
            try:
                for name, data in old.items():
                    path = inside(self.root, name)
                    if data is None:
                        path.unlink(missing_ok=True)
                    else:
                        self._replace(path, data)
                journal.unlink()
            except Exception:
                pass  # Journal and exact backup remain for explicit recovery.
            raise
        else:
            journal.unlink()

    def recover(self, writer_stopped=False):
        """Explicit recovery; never break another process's live lock."""
        import socket

        lock = self.appdata / ".writer.lock"
        if lock.exists():
            data = json.loads(lock.read_text())
            if data.get("host") == socket.gethostname():
                try:
                    os.kill(int(data["pid"]), 0)
                except ProcessLookupError:
                    pass
                else:
                    raise ValueError(
                        "The writer process is still running; close it before recovery"
                    )
            if not writer_stopped:
                raise ValueError(
                    "Confirm that the previous writer has stopped on every synced computer"
                )
        journal = self.appdata / "pending-transaction.json"
        if journal.exists():
            record = json.loads(journal.read_text())
            backup = inside(self.root, record["backup"])
            if not backup.is_relative_to(self.appdata / "backups"):
                raise ValueError("Unexpected recovery archive location")
            with zipfile.ZipFile(backup) as archive:
                manifest = json.loads(archive.read("transaction.json"))
                before = {
                    n: None if n in manifest["new"] else archive.read("before/" + n)
                    for n in manifest["targets"]
                }
            # Validate all destinations before the first recovery write.
            targets = {n: inside(self.root, n) for n in before}
            for name, data in before.items():
                if data is None:
                    targets[name].unlink(missing_ok=True)
                else:
                    self._replace(targets[name], data)
            journal.unlink()
        lock.unlink(missing_ok=True)
        return self.state()

    @staticmethod
    def _replace(path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            try:
                os.replace(temp, path)
            except PermissionError:
                # Windows refuses to replace a file another process has open, and
                # KiCad keeps the parts database open through ODBC while it runs.
                # Write the new database into the existing file through SQLite's
                # own locking instead, so saving works without closing KiCad.
                if path.suffix != ".sqlite" or not path.is_file():
                    raise
                _overwrite_sqlite(path, data)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)

    def compiled(self, rows, pending=None, fingerprints=None):
        """Build friendly chooser indexes, preserving stable source CAD links."""
        from .category_libraries import footprints

        generated, category_entries, footprint_links, category_names = footprints(
            self, rows, pending or {}, fingerprints if fingerprints is not None else {}
        )
        connection = sqlite3.connect(":memory:")
        extra = ["LibraryID", "PartID", "SearchPath", "KiCadFootprint"]
        allcols = COLUMNS + extra
        try:
            connection.execute(
                "CREATE TABLE components ("
                + ",".join('"' + c + '" TEXT' for c in allcols)
                + ", PRIMARY KEY (ID))"
            )
            for row in rows:
                values = [row.get(c, "") for c in COLUMNS] + [
                    self.id,
                    row["ID"],
                    f"{self.name}/{row['Category']}/{row['MPN']}",
                    footprint_links.get(row["ID"], ""),
                ]
                connection.execute(
                    "INSERT INTO components VALUES ("
                    + ",".join("?" for _ in values)
                    + ")",
                    values,
                )
            libraries = []
            for category in sorted({r["Category"] for r in rows if r["Symbol"]}):
                view = "cat_" + digest(category.encode())[:12]
                # Category is a value, escaped as an SQL literal; view name is generated.
                escaped = category.replace("'", "''")
                connection.execute(
                    f"CREATE VIEW {view} AS SELECT * FROM components WHERE Category='{escaped}' AND Symbol<>''"
                )
                libraries.append(
                    dict(
                        name=category_names[category],
                        table=view,
                        key="ID",
                        symbols="Symbol",
                        footprints="KiCadFootprint",
                        fields=[
                            dict(
                                column=c,
                                name=c,
                                visible_on_add=False,
                                visible_in_chooser=c
                                in {
                                    "MPN",
                                    "Manufacturer",
                                    "Description",
                                    "SearchPath",
                                    "Value",
                                    "Package",
                                },
                                show_name=False,
                                inherit_properties=False,
                            )
                            for c in [
                                "LibraryID",
                                "PartID",
                                "MPN",
                                "Manufacturer",
                                "Value",
                                "Description",
                                "Datasheet",
                                "SearchPath",
                                "Package",
                                "Tolerance",
                                "Voltage_VDC",
                                "Current_A",
                                "Voltage_V",
                                "Power_W",
                            ]
                        ],
                    )
                )
            connection.commit()
            payload = connection.serialize()
        finally:
            connection.close()
        config = dict(
            meta={"version": 0},
            name=self.name,
            description="Local component catalog with folder search and accessory BOMs",
            globally_unique_keys=True,
            source=dict(
                type="odbc",
                timeout_seconds=5,
                connection_string="DSN=VirtusSQLite;Database=${CWD}/parts.sqlite;",
            ),
            libraries=libraries,
        )
        sym_entries, fp_entries = {}, {}
        for row in rows:
            if row["Symbol"]:
                nick = row["Symbol"].split(":")[0]
                sym_entries[nick] = "${MYCO_PARTS_DIR}/../" + row["SymbolFile"]
            if row["Footprint"]:
                nick = row["Footprint"].split(":")[0]
                fp_entries[nick] = "${MYCO_PARTS_DIR}/../" + str(
                    PurePosixPath(row["FootprintFile"]).parent
                )
        for pretty in sorted((self.root / "components/logos").rglob("*.pretty")):
            if pretty.is_dir() and not pretty.is_symlink():
                rel = pretty.relative_to(self.root).as_posix()
                fp_entries[self.prefix + "_logos_" + digest(rel.encode())[:10]] = (
                    "${MYCO_PARTS_DIR}/../" + rel
                )

        def table(kind, entries):
            result = [kind, ["version", "7"]]
            if kind == "sym_lib_table":
                result.append(
                    [
                        "lib",
                        ["name", Q(self.name)],
                        ["type", "Database"],
                        ["uri", Q("${MYCO_PARTS_DIR}/parts.kicad_dbl")],
                        ["options", Q("")],
                        ["descr", Q("Managed catalog")],
                    ]
                )
            for name, uri in entries.items():
                hidden = True
                if isinstance(uri, tuple):
                    uri, hidden = uri
                result.append(
                    [
                        "lib",
                        ["name", Q(name)],
                        ["type", "KiCad"],
                        ["uri", Q(uri)],
                        ["options", Q("")],
                        ["descr", Q("Library Manager asset")],
                    ]
                )
                if hidden:
                    # Compatibility sources stay enabled without chooser duplicates.
                    result[-1].append(["hidden"])
            return (dump(result) + "\n").encode()

        return {
            f"{APPDATA}/parts.sqlite": payload,
            f"{APPDATA}/parts.kicad_dbl": json_bytes(config),
            f"{APPDATA}/sym-lib-table": table("sym_lib_table", sym_entries),
            f"{APPDATA}/fp-lib-table": table("fp_lib_table", {**fp_entries, **category_entries}),
            **generated,
        }


def _overwrite_sqlite(path, data):
    """Copy a serialized SQLite database into an existing (possibly open) database file."""
    source = sqlite3.connect(":memory:")
    target = sqlite3.connect(path, timeout=15)
    try:
        source.deserialize(data)
        source.backup(target)
    except sqlite3.OperationalError as exc:
        raise PermissionError(
            "KiCad is busy reading the parts database; try saving again in a moment"
        ) from exc
    finally:
        target.close()
        source.close()


def create_library(path, install_runtime=False):
    root = Path(path).expanduser().resolve()
    slug(root.name)
    if root.exists() and any(
        p.name not in (".DS_Store", "desktop.ini") for p in root.iterdir()
    ):
        raise ValueError(
            "Create a library in an empty folder; existing libraries are never overwritten"
        )
    root.mkdir(parents=True, exist_ok=True)
    try:
        for name in (
            "components/connectors",
            "components/ICs",
            "components/accessories",
            "components/resistors",
            "components/capacitors",
            "components/logos",
            APPDATA + "/backups",
            APPDATA + "/original-downloads",
        ):
            (root / name).mkdir(parents=True, exist_ok=True)
        (root / APPDATA / "library.json").write_bytes(
            json_bytes(dict(formatVersion=2, id=str(uuid.uuid4()), created=timestamp()))
        )
        (root / APPDATA / "components.csv").write_bytes(csv_bytes([]))
        resources = SOURCE_ROOT / "resources/generic_models"
        if not resources.exists():
            raise ValueError("Bundled generic models are missing")
        shutil.copytree(
            resources, root / "components/generic_models", dirs_exist_ok=True
        )
        lib = Library(root)
        for name, data in lib.compiled([]).items():
            target = root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        if install_runtime:
            install_portable_runtime(root)
        return lib
    except Exception:
        # Only these owned additions are created in the initially empty destination.
        for name in ("components", APPDATA, "Open Library.app", "Open Library.bat"):
            path = root / name
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink(missing_ok=True)
        raise


def install_portable_runtime(root):
    """Compatibility entry point: new libraries are data-only, never app copies."""
    appdata = Path(root) / APPDATA
    (appdata / "Start here.txt").write_text(
        "Open KiCad Library Manager from Applications or Spotlight, then choose this library folder.\n"
        "Keep components and appdata together. The manager is not needed while using saved parts in KiCad.\n"
    )
