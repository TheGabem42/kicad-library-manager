"""KiCad CLI previews and explicit, backed-up registration. No startup writes."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from .sexpr import Quoted as Q, parse, dump, children, value
from .storage import json_bytes


def cli_path():
    override = os.environ.get("KICAD_CLI")
    if override and Path(override).is_file():
        return override
    found = shutil.which("kicad-cli")
    if found:
        return found
    candidates = [Path("/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli")]
    if os.name == "nt":
        candidates.extend(
            sorted(
                Path(os.environ.get("ProgramFiles", "C:/Program Files")).glob(
                    "KiCad/*/bin/kicad-cli.exe"
                ),
                reverse=True,
            )
        )
    return next((str(p) for p in candidates if p.is_file()), None)


def render(kind, path, name=None):
    cli = cli_path()
    if not cli:
        raise ValueError(
            "Install KiCad 10 to render native symbol and footprint previews"
        )
    with tempfile.TemporaryDirectory(prefix="library-preview-") as directory:
        cmd = [cli, kind, "export", "svg", "--output", directory]
        if kind == "sym" and name:
            cmd += ["--symbol", name, "--include-hidden-pins"]
        if kind == "fp":
            cmd += [
                "--layers",
                "F.Cu,F.SilkS,F.Fab,Edge.Cuts",
                "--sketch-pads-on-fab-layers",
            ]
        cmd.append(str(path))
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=45)
        files = sorted(Path(directory).rglob("*.svg"))
        if result.returncode or not files:
            raise ValueError(
                "KiCad could not render this asset: "
                + result.stderr[:600]
                + result.stdout[:600]
            )
        return [p.read_text() for p in files]


def config_directory():
    if sys.platform == "darwin":
        return Path.home() / "Library/Preferences/kicad/10.0"
    if os.name == "nt":
        return Path(os.environ.get("APPDATA", str(Path.home()))) / "kicad/10.0"
    return (
        Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
        / "kicad/10.0"
    )


def running():
    if os.name == "nt":
        output = subprocess.run(
            ["tasklist", "/FO", "CSV"], capture_output=True, text=True
        ).stdout.lower()
        return any(
            '"' + name + '.exe"' in output for name in ("kicad", "eeschema", "pcbnew")
        )
    output = subprocess.run(
        ["ps", "-A", "-o", "comm="], capture_output=True, text=True
    ).stdout
    return any(
        Path(line.strip()).name in ("kicad", "eeschema", "pcbnew")
        for line in output.splitlines()
    )


def setup_status(lib, config=None):
    config = Path(config) if config else config_directory()
    data = {}
    error = ""
    try:
        if (config / "kicad_common.json").exists():
            data = json.loads((config / "kicad_common.json").read_text())
    except (OSError, ValueError) as exc:
        error = str(exc)
    # KiCad writes "vars": null (and may write "environment": null) when no path variables exist.
    active = ((data.get("environment") or {}).get("vars") or {}).get("MYCO_PARTS_DIR", "")
    return dict(
        cli=cli_path(),
        config=str(config),
        activeLibrary=active,
        connected=active == str(lib.appdata),
        error=error,
        bomCommand=f'"{sys.executable}" "{Path(__file__).resolve().parent.parent / "app.py"}" bom --library "{lib.root}" --input "%I" --output "%O.csv"',
    )


def connect(lib, config=None, switch=False, check_running=True):
    """User-invoked only. Tests always pass a synthetic configuration directory."""
    config = Path(config) if config else config_directory()
    if check_running and running():
        raise ValueError("Close KiCad before connecting this library")
    if (
        os.environ.get("MYCO_PARTS_DIR")
        and Path(os.environ["MYCO_PARTS_DIR"]).resolve() != lib.appdata
    ):
        raise ValueError(
            "An operating-system MYCO_PARTS_DIR setting points elsewhere; resolve it first"
        )
    common = config / "kicad_common.json"
    data = json.loads(common.read_text()) if common.exists() else {}
    if not isinstance(data.get("environment"), dict):
        data["environment"] = {}
    if not isinstance(data["environment"].get("vars"), dict):
        data["environment"]["vars"] = {}
    vars_ = data["environment"]["vars"]
    current = vars_.get("MYCO_PARTS_DIR")
    if current and current != str(lib.appdata) and not switch:
        raise ValueError(
            "Another library is active. Review and confirm switching this machine to the selected library."
        )
    vars_["MYCO_PARTS_DIR"] = str(lib.appdata)
    writes = {common: json_bytes(data)}
    for filename, kind in [
        ("sym-lib-table", "sym_lib_table"),
        ("fp-lib-table", "fp_lib_table"),
    ]:
        path = config / filename
        tree = parse(path.read_text()) if path.exists() else [kind, ["version", "7"]]
        managed = "Library Manager"
        for entry in children(tree, "lib"):
            if value(entry, "name") == managed:
                if (
                    value(entry, "type") != "Table"
                    or value(entry, "uri") != "${MYCO_PARTS_DIR}/" + filename
                ):
                    raise ValueError(
                        "Existing Library Manager registration points elsewhere"
                    )
                tree.remove(entry)
            elif filename == "sym-lib-table" and value(entry, "name") == lib.name:
                raise ValueError(
                    "Another registered library already uses this folder name"
                )
        tree.append(
            [
                "lib",
                ["name", Q(managed)],
                ["type", "Table"],
                ["uri", Q("${MYCO_PARTS_DIR}/" + filename)],
                ["options", Q("")],
                ["descr", Q("Portable local library")],
            ]
        )
        writes[path] = (dump(tree) + "\n").encode()
    config.mkdir(parents=True, exist_ok=True)
    # A root-folder rename changes the visible nickname, not the catalog identities.
    configured = json.loads((lib.appdata / "parts.kicad_dbl").read_text()).get("name")
    if configured != lib.name:
        lib.transaction(lib.compiled(lib.rows()), lib.revision())
    original = {p: p.read_bytes() if p.exists() else None for p in writes}
    stamp = time.strftime("%Y%m%dT%H%M%S") + "-" + str(time.time_ns())
    for p, old in original.items():
        if old is not None:
            p.with_name(p.name + "." + stamp + ".bak").write_bytes(old)
    try:
        for p, payload in writes.items():
            lib._replace(p, payload)
    except Exception:
        for p, old in original.items():
            if old is None:
                p.unlink(missing_ok=True)
            else:
                lib._replace(p, old)
        raise
    return setup_status(lib, config)
