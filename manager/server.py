"""Loopback-only UI service; mutations require the process's random capability token."""

from __future__ import annotations

import base64
import json
import mimetypes
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import threading
import traceback
import socketserver
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
import webbrowser

from . import VERSION, supplier, kicad, cad, preferences, conflicts
from .storage import (
    Library,
    create_library,
    SOURCE_ROOT,
    inside,
    slug,
    category_path,
)
from .importer import ImportSession
from .preview import component_preview


class State:
    def __init__(self, library):
        self.library = Library(library) if library else None
        self.token = secrets.token_urlsafe(32)
        self.sessions = {}
        self.lock = threading.RLock()
        self.stopping = False

    def lib(self):
        if not self.library:
            raise ValueError("Open or create a library first")
        return self.library

    def session(self, ident):
        if ident not in self.sessions:
            raise ValueError("Import session expired; start a new import")
        return self.sessions[ident]


def pick_folder():
    if sys.platform == "darwin":
        result = subprocess.run(
            [
                "osascript",
                "-e",
                'POSIX path of (choose folder with prompt "Choose a library folder")',
            ],
            capture_output=True,
            text=True,
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    if os.name == "nt":
        result = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-STA",
                "-Command",
                'Add-Type -AssemblyName System.Windows.Forms; $d=New-Object System.Windows.Forms.FolderBrowserDialog; if($d.ShowDialog() -eq "OK"){ $d.SelectedPath }',
            ],
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import tkinter as t; from tkinter import filedialog; r=t.Tk(); r.withdraw(); print(filedialog.askdirectory()); r.destroy()",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode:
        raise ValueError("Folder picker unavailable; paste a folder path instead")
    return result.stdout.strip()


def run(library=None, port=0, open_browser=True, desktop=False, parent_pid=None):
    startup_warning = ""
    if desktop and not library:
        remembered = preferences.read().get("lastLibrary")
        if remembered:
            try:
                conflicts.check(Library(remembered).root)
                Library(remembered).rows()
                library = remembered
            except (ValueError, OSError) as exc:
                startup_warning = "Your previous library could not be opened. Choose its folder again. " + str(exc)
    state = State(library)

    def remember():
        nonlocal startup_warning
        if desktop and state.library:
            try:
                preferences.remember_library(state.library.root)
            except OSError:
                startup_warning = "Library opened, but its location could not be remembered."

    def shutdown():
        # Finish the active transaction before refusing new writes and stopping.
        with state.lock:
            state.stopping = True
        server.shutdown()


    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            if args and str(args[1] if len(args) > 1 else "").startswith("5"):
                super().log_message(fmt, *args)

        def send(self, data, status=200, content_type="application/json"):
            if not isinstance(data, bytes):
                data = json.dumps(data, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(data)

        def authorize(self):
            query = parse_qs(urlparse(self.path).query)
            supplied = (
                self.headers.get("X-Library-Token") or query.get("token", [""])[0]
            )
            if not secrets.compare_digest(supplied, state.token):
                raise PermissionError("Invalid local session token")
            origin = self.headers.get("Origin")
            if origin and origin != f"http://127.0.0.1:{self.server.server_port}":
                raise PermissionError("Cross-origin request rejected")

        def do_GET(self):
            try:
                parsed = urlparse(self.path)
                path = parsed.path
                query = parse_qs(parsed.query)
                if path.startswith("/api/"):
                    self.authorize()
                    with state.lock:
                        if path == "/api/preferences":
                            return self.send(preferences.read())
                        if path == "/api/state":
                            return self.send(
                                dict(
                                    version=VERSION,
                                    startupWarning=startup_warning,
                                    library=state.library.state()
                                    if state.library
                                    else None,
                                    cli=kicad.cli_path(),
                                )
                            )
                        if path == "/api/setup":
                            return self.send(kicad.setup_status(state.lib()))
                        if path == "/api/delete-plan":
                            from . import delete

                            return self.send(delete.plan(state.lib(), query["part"][0]))
                        if path == "/api/component-preview":
                            kind = query.get("kind", ["model"])[0]
                            result = component_preview(state.lib(), query["partID"][0], kind)
                            if kind == "step":
                                return self.send(result, content_type="application/octet-stream")
                            return self.send(result)
                        if path == "/api/generic-preview" or (
                            path == "/api/preview" and query.get("kind") == ["generic"]
                        ):
                            generic_root = (
                                state.lib().root / "components/generic_models"
                            )
                            inventory = json.loads(
                                (generic_root / "inventory.json").read_text()
                            )
                            template = next(
                                (
                                    r
                                    for r in inventory
                                    if r["type"] == query.get("type", [""])[0]
                                    and r["package"]
                                    == query.get("package", ["0805"])[0]
                                ),
                                None,
                            )
                            part_id = query.get("partID", [""])[0]
                            existing = (
                                next(
                                    (
                                        r
                                        for r in state.lib().rows()
                                        if r["ID"] == part_id
                                    ),
                                    None,
                                )
                                if part_id
                                else None
                            )
                            if (
                                existing
                                and existing["Type"] == query.get("type", [""])[0]
                                and existing["Package"]
                                == query.get("package", ["0805"])[0]
                                and existing["SymbolFile"].startswith(
                                    "components/generic_models/"
                                )
                                and existing["FootprintFile"].startswith(
                                    "components/generic_models/"
                                )
                            ):
                                fp = cad.footprint_data(
                                    inside(
                                        state.lib().root, existing["FootprintFile"]
                                    ).read_text()
                                )
                                prefix = (
                                    "${MYCO_PARTS_DIR}/../components/generic_models/"
                                )
                                selected = next(((i, model["path"]) for i, model in enumerate(fp["models"])
                                                 if model["path"].startswith(prefix)
                                                 and Path(model["path"]).suffix.lower() in (".step", ".stp")
                                                 and inside(generic_root, model["path"].removeprefix(prefix)).is_file()), (None, ""))
                                template = dict(
                                    symbol=existing["SymbolFile"].removeprefix(
                                        "components/generic_models/"
                                    ),
                                    footprint=existing["FootprintFile"].removeprefix(
                                        "components/generic_models/"
                                    ),
                                    symbolName=existing["Symbol"].split(":", 1)[1],
                                    model=selected[1].removeprefix(prefix),
                                    modelIndex=selected[0],
                                )
                            if template is None:
                                raise ValueError("Select a bundled passive package")
                            symbol_path = inside(generic_root, template["symbol"])
                            footprint_path = inside(generic_root, template["footprint"])
                            if path == "/api/generic-preview":
                                return self.send(
                                    {
                                        "info": dict(
                                            pins=cad.symbol_pins(
                                                cad.flatten_symbol(
                                                    symbol_path.read_text(),
                                                    template["symbolName"],
                                                )
                                            ),
                                            footprint=cad.footprint_data(
                                                footprint_path.read_text()
                                            ),
                                            genericModel=template["model"],
                                            genericModelIndex=template.get("modelIndex", 0),
                                        )
                                    }
                                )
                            if query.get("render", ["symbol"])[0] == "footprint":
                                with tempfile.TemporaryDirectory() as directory:
                                    pretty = Path(directory) / "preview.pretty"
                                    pretty.mkdir()
                                    (pretty / footprint_path.name).write_bytes(
                                        footprint_path.read_bytes()
                                    )
                                    return self.send(
                                        {"svgs": kicad.render("fp", pretty)}
                                    )
                            return self.send(
                                {
                                    "svgs": kicad.render(
                                        "sym", symbol_path, template["symbolName"]
                                    )
                                }
                            )
                        if path == "/api/asset":
                            session = state.session(query["session"][0])
                            asset = session.assets[query["asset"][0]]
                            return self.send(
                                asset["path"].read_bytes(),
                                content_type="application/octet-stream",
                            )
                        if path == "/api/generic-model":
                            p = inside(
                                state.lib().root,
                                "components/generic_models/" + query["path"][0],
                            )
                            if p.suffix.lower() not in (".step", ".stp"):
                                raise ValueError("Not a STEP model")
                            return self.send(
                                p.read_bytes(), content_type="application/octet-stream"
                            )
                        if path == "/api/preview":
                            session = state.session(query["session"][0])
                            kind = query.get("kind", ["symbol"])[0]
                            if kind == "original":
                                asset = session.asset(query["asset"][0], [".kicad_sym"])
                                svgs = kicad.render(
                                    "sym", asset["path"], query.get("name", [""])[0]
                                )
                            elif kind == "footprint":
                                svgs = kicad.render(
                                    "fp", session.root / "preview.pretty"
                                )
                            else:
                                p = session.prepared
                                if not p or not p["symbolText"]:
                                    raise ValueError("Prepare the component first")
                                svgs = kicad.render(
                                    "sym",
                                    session.root / "preview.kicad_sym",
                                    p["row"]["Symbol"].split(":", 1)[1],
                                )
                            return self.send(dict(svgs=svgs))
                    raise ValueError("Unknown API route")
                if path == "/":
                    path = "/index.html"
                file = inside(SOURCE_ROOT / "web", path.lstrip("/"))
                if not file.is_file():
                    return self.send({"error": "Not found"}, 404)
                return self.send(
                    file.read_bytes(),
                    content_type=mimetypes.guess_type(file)[0]
                    or "application/octet-stream",
                )
            except PermissionError as exc:
                self.send({"error": str(exc)}, 403)
            except (ValueError, OSError, KeyError, subprocess.TimeoutExpired) as exc:
                self.send({"error": str(exc)}, 400)
            except Exception as exc:
                traceback.print_exc()
                self.send({"error": str(exc)}, 500)

        def do_POST(self):
            try:
                self.authorize()
                length = int(self.headers.get("Content-Length", "0"))
                if length > 180 * 1024 * 1024:
                    raise ValueError("Upload each file in chunks below 128 MiB")
                data = json.loads(self.rfile.read(length) or b"{}")
                path = urlparse(self.path).path
                if path == "/api/pick-folder":
                    return self.send({"path": pick_folder()})
                if path == "/api/quit":
                    self.send({"ok": True})
                    threading.Thread(target=shutdown, daemon=True).start()
                    return
                if path == "/api/supplier":
                    return self.send(
                        supplier.read_product(
                            data.get("url", ""), data.get("content", "")
                        )
                    )
                with state.lock:
                    if state.stopping:
                        raise ValueError("The application is closing")
                    if path == "/api/preferences":
                        return self.send(preferences.save(data))
                    if path == "/api/open":
                        lib = Library(data["path"])
                        conflicts.check(lib.root)
                        lib.rows()
                        state.library = lib
                        remember()
                        return self.send(lib.state())
                    if path == "/api/create":
                        state.library = create_library(data["path"], install_runtime=False)
                        remember()
                        return self.send(state.lib().state())
                    if path == "/api/import/new":
                        if len(state.sessions) >= 24:
                            raise ValueError(
                                "Close unused import sessions before opening another"
                            )
                        session = ImportSession()
                        state.sessions[session.id] = session
                        session.library_id = state.lib().id
                        return self.send(session.inventory())
                    if path == "/api/import/edit":
                        if len(state.sessions) >= 24:
                            raise ValueError("Close unused import sessions first")
                        session = ImportSession()
                        state.sessions[session.id] = session
                        session.library_id = state.lib().id
                        return self.send(
                            session.load_existing(state.lib(), data["partID"])
                        )
                    if path == "/api/import/close":
                        session = state.sessions.pop(data["session"], None)
                        if session:
                            session.close()
                        return self.send({"ok": True})
                    if path == "/api/import/standard-package":
                        from .standard_packages import select
                        return self.send(select(state.session(data["session"]), data["package"]))
                    if path == "/api/import/upload":
                        session = state.session(data["session"])
                        session.upload(
                            data["name"], base64.b64decode(data["data"], validate=True)
                        )
                        return self.send(session.inventory())
                    if path == "/api/import/inspect":
                        return self.send(
                            state.session(data["session"]).inspect(data["draft"])[0]
                        )
                    if path == "/api/import/prepare":
                        return self.send(
                            state.session(data["session"]).prepare(
                                state.lib(), data["draft"]
                            )
                        )
                    if path == "/api/import/preview":
                        return self.send(state.session(data["session"]).preview(data["draft"], data.get("kind", "symbol")))
                    if path == "/api/import/save":
                        row = state.session(data["session"]).commit(
                            state.lib(), data["token"], data.get("replace", False)
                        )
                        return self.send({"part": row, "library": state.lib().state()})
                    if path == "/api/folder":
                        lib = state.lib()
                        name = category_path(data["path"])
                        for segment in Path(name).parts:
                            slug(segment)
                        if any(
                            name == r["AssetFolder"]
                            or name.startswith(r["AssetFolder"] + "/")
                            for r in lib.rows()
                        ):
                            raise ValueError(
                                "Cannot create categories inside a component folder"
                            )
                        with lib.locked(data.get("revision")):
                            inside(lib.root, name).mkdir(parents=True, exist_ok=True)
                        return self.send(lib.state())
                    if path == "/api/delete":
                        from . import delete

                        lib = state.lib()
                        removed = delete.delete(lib, data["partID"])
                        return self.send({"removed": removed, "library": lib.state()})
                    if path == "/api/connect":
                        return self.send(
                            kicad.connect(state.lib(), switch=data.get("switch", False))
                        )
                    if path == "/api/check-odbc":
                        from .odbc import check_odbc

                        return self.send(check_odbc(state.lib().appdata))
                    if path == "/api/rebuild":
                        lib = state.lib()
                        lib.transaction(lib.compiled(lib.rows()), data.get("revision"))
                        return self.send(lib.state())
                    if path == "/api/recover":
                        return self.send(
                            state.lib().recover(data.get("writerStopped", False))
                        )
                    if path == "/api/bom":
                        from .bom import export

                        return self.send(
                            export(state.lib(), data["input"], data["output"])
                        )
                raise ValueError("Unknown API route")
            except PermissionError as exc:
                self.send({"error": str(exc)}, 403)
            except (ValueError, OSError, KeyError, subprocess.TimeoutExpired) as exc:
                self.send({"error": str(exc)}, 400)
            except Exception as exc:
                traceback.print_exc()
                self.send({"error": str(exc)}, 500)

    class LocalServer(ThreadingHTTPServer):
        def server_bind(self):
            # A local desktop tool must not wait for the machine's DNS search domains.
            socketserver.TCPServer.server_bind(self)
            self.server_name = "localhost"
            self.server_port = self.server_address[1]

    server = LocalServer(("127.0.0.1", port), Handler)
    server.daemon_threads = False
    # A crashed/force-quit desktop host must not leave an orphan worker. The
    # watchdog uses the same transaction-aware shutdown as a normal close.
    watchdog_stop = threading.Event()
    if desktop and parent_pid:
        def watch_parent():
            while not watchdog_stop.wait(0.5):
                if os.getppid() != parent_pid:
                    shutdown()
                    return
        threading.Thread(target=watch_parent, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}/#token={state.token}"
    print("Library Manager: " + url, flush=True)
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        watchdog_stop.set()
        server.server_close()
        for session in state.sessions.values():
            session.close()
