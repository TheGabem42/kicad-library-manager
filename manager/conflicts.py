"""Detect git merge-conflict markers committed into a library.

When a synced/git-shared library hits a merge conflict and the result is
committed, raw <<<<<<< / ======= / >>>>>>> lines end up in components.csv,
category-libraries.json and the KiCad library tables. The catalog then can't
be parsed (the UI shows a blank page) and KiCad reports missing tables. Check
for this when a library is opened and explain it instead.
"""
import re
from pathlib import Path

MARKER = re.compile(rb"^(<<<<<<< |=======\r?$|>>>>>>> )", re.MULTILINE)
SKIP_DIRS = {"backups", "original-downloads", "generic_models"}
BINARY = {".sqlite", ".step", ".stp", ".wrl", ".zip", ".png", ".jpg", ".pdf"}


def conflicted_files(library_root):
    root = Path(library_root)
    found = []
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if not path.is_file() or path.suffix.lower() in BINARY or SKIP_DIRS & set(rel.parts):
            continue
        try:
            if MARKER.search(path.read_bytes()):
                found.append(rel.as_posix())
        except OSError:
            continue
    return found


def check(library_root):
    """Raise ValueError naming the files if the library contains conflict markers."""
    files = conflicted_files(library_root)
    if files:
        raise ValueError(
            "This library contains unresolved merge-conflict markers in: "
            + ", ".join(files)
            + ". Resolve the conflict in those files (or restore them from version "
            "control), then open the library again."
        )
