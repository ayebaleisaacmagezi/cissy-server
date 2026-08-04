"""Zipping a generated project for download.

The exclusions are the whole point. Two of them keep the archive small; two of
them keep signing material out of a file that lands in someone's Downloads
folder and gets forwarded around.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

# Rebuildable, and together roughly 300 MB against ~2 MB for the source.
_JUNK_DIRS = {"build", ".dart_tool", ".gradle", ".idea", "__pycache__"}

# key.properties holds both passwords in plain text; the keystore is the key
# itself. Neither can be undone once it has left the server.
_SECRET_NAMES = {"key.properties"}
_SECRET_SUFFIXES = {".jks", ".keystore", ".p12", ".pfx"}


def should_include(relative: Path) -> bool:
    if any(part in _JUNK_DIRS for part in relative.parts):
        return False
    if relative.name in _SECRET_NAMES:
        return False
    if relative.suffix.lower() in _SECRET_SUFFIXES:
        return False
    return True


def write_archive(project_dir: Path, target: Path, *, root_name: str) -> Path:
    """Zip `project_dir` into `target`, returning the archive path.

    Everything is nested under `root_name` so unzipping produces one folder
    rather than scattering a Flutter project across the current directory.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".zip.tmp")

    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in sorted(project_dir.rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(project_dir)
            if not should_include(relative):
                continue
            archive.write(path, Path(root_name) / relative)

    tmp.replace(target)
    return target
