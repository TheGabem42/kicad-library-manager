"""Check CSV/SQLite links and optionally query through the native ODBC driver manager."""

from pathlib import Path
import ctypes as C
import ctypes.util
import json
import os

ROOT = Path(__file__).resolve().parent


def connection_string(root):
    # KiCad 10.0.1 SCH_IO_DATABASE::ensureConnection substitutes only ${CWD}.
    root = Path(root).resolve()
    raw = json.loads((root / "parts.kicad_dbl").read_text())["source"][
        "connection_string"
    ]
    if ";" in str(root):
        raise ValueError("ODBC library path must not contain semicolons")
    expanded = raw.replace("${CWD}", str(root))
    if "${" in expanded:
        raise ValueError(
            "Unsupported database connection variable: KiCad only expands ${CWD} here"
        )
    return expanded


def manager_candidates():
    """ODBC driver managers to try, most specific first."""
    if os.name == "nt":
        return ["odbc32.dll"]
    found = [
        "/Applications/KiCad/KiCad.app/Contents/Frameworks/libodbc.2.dylib",
        ctypes.util.find_library("odbc"),
        "/opt/homebrew/lib/libodbc.2.dylib",
        "/usr/local/lib/libodbc.2.dylib",
    ]
    return [m for m in dict.fromkeys(found) if m and (Path(m).exists() or "/" not in m)]


def load_manager(candidates):
    """Load the first ODBC manager that loads. KiCad.app's bundled libodbc depends
    on @rpath libraries that only resolve inside KiCad, so outside KiCad it can
    fail to load; Homebrew's unixODBC (installed with sqliteodbc) is equivalent."""
    errors = []
    for manager in candidates:
        try:
            return manager, (C.WinDLL(manager) if os.name == "nt" else C.CDLL(manager))
        except OSError as exc:
            errors.append(f"{manager}: {str(exc).splitlines()[0]}")
    raise RuntimeError("No ODBC driver manager could be loaded" + (": " + "; ".join(errors) if errors else ""))


def check_odbc(root, driver=None, manager=None):
    manager, dll = load_manager([manager] if manager else manager_candidates())
    handle = C.c_void_p
    short = C.c_short
    integer = C.c_int
    signatures = {
        "SQLAllocHandle": ([short, handle, C.POINTER(handle)], short),
        "SQLSetEnvAttr": ([handle, integer, handle, integer], short),
        "SQLDriverConnectW": (
            [
                handle,
                handle,
                handle,
                short,
                handle,
                short,
                C.POINTER(short),
                C.c_ushort,
            ],
            short,
        ),
        "SQLExecDirectW": ([handle, handle, integer], short),
        "SQLFetch": ([handle], short),
        "SQLGetData": (
            [handle, C.c_ushort, short, handle, C.c_ssize_t, C.POINTER(C.c_ssize_t)],
            short,
        ),
        "SQLGetDiagRecW": (
            [
                short,
                handle,
                short,
                handle,
                C.POINTER(integer),
                handle,
                short,
                C.POINTER(short),
            ],
            short,
        ),
        "SQLDisconnect": ([handle], short),
        "SQLFreeHandle": ([short, handle], short),
    }
    for name, (args, result) in signatures.items():
        fn = getattr(dll, name)
        fn.argtypes = args
        fn.restype = result

    def wide(text):
        raw = text.encode("utf-16-le") + b"\0\0"
        return (C.c_ushort * (len(raw) // 2)).from_buffer_copy(raw)

    def decode(buf):
        return bytes(buf).decode("utf-16-le").split("\0", 1)[0]

    def check(code, typ, h):
        if code in (0, 1):
            return
        state = (C.c_ushort * 6)()
        msg = (C.c_ushort * 2048)()
        native = integer()
        size = short()
        dll.SQLGetDiagRecW(typ, h, 1, state, C.byref(native), msg, 2048, C.byref(size))
        raise RuntimeError(f"ODBC {decode(state)}: {decode(msg)} (return {code})")

    env = handle()
    dbc = handle()
    stmt = handle()
    try:
        check(dll.SQLAllocHandle(1, None, C.byref(env)), 1, env)
        check(dll.SQLSetEnvAttr(env, 200, handle(3), 0), 1, env)
        check(dll.SQLAllocHandle(2, env, C.byref(dbc)), 1, env)
        cs = connection_string(root)
        if driver is not None:
            # Explicit diagnostic override; normal checks always use the real library config.
            drvalue = driver.replace("}", "}}")
            cs = f"Driver={{{drvalue}}};Database={Path(root).resolve() / 'parts.sqlite'};"
        connection = wide(cs)
        out = (C.c_ushort * 2048)()
        size = short()
        check(
            dll.SQLDriverConnectW(
                dbc, None, connection, -3, out, 2048, C.byref(size), 0
            ),
            2,
            dbc,
        )
        check(dll.SQLAllocHandle(3, dbc, C.byref(stmt)), 2, dbc)
        tables = [
            entry["table"]
            for entry in json.loads((Path(root) / "parts.kicad_dbl").read_text())[
                "libraries"
            ]
        ]
        sql = wide(
            " UNION ALL ".join(
                "SELECT '"
                + table.replace("'", "''")
                + "', COUNT(*) FROM \""
                + table.replace('"', '""')
                + '"'
                for table in tables
            )
            if tables
            else "SELECT 'components', COUNT(*) FROM components"
        )
        check(dll.SQLExecDirectW(stmt, sql, -3), 3, stmt)
        rows = []
        while True:
            status = dll.SQLFetch(stmt)
            if status == 100:
                break
            check(status, 3, stmt)
            row = []
            for col in (1, 2):
                value = (C.c_ushort * 256)()
                length = C.c_ssize_t()
                check(
                    dll.SQLGetData(
                        stmt, col, -8, value, C.sizeof(value), C.byref(length)
                    ),
                    3,
                    stmt,
                )
                row.append(decode(value))
            rows.append(row)
        return {
            "manager": manager,
            "configuration": "parts.kicad_dbl"
            if driver is None
            else "explicit driver override",
            "counts": rows,
        }
    finally:
        if stmt.value:
            dll.SQLFreeHandle(3, stmt)
        if dbc.value:
            dll.SQLDisconnect(dbc)
            dll.SQLFreeHandle(2, dbc)
        if env.value:
            dll.SQLFreeHandle(1, env)
