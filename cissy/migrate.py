"""Moving a single-user install onto accounts, once.

The live box has apps sitting directly in `projects/<app-id>/` from before
anyone had an account. Under the new layout that path is where a *user*
directory goes, so those apps would simply vanish from the listing: `list()`
looks for a `config.json` one level down and would not find one.

So they are moved into the admin's directory the first time the new code runs.
Idempotent, and it never overwrites: a name that would collide keeps a suffix
rather than replacing whatever is already there.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Callable

CONFIG_NAME = "config.json"


def legacy_apps(projects: Path) -> list[Path]:
    """Directories that look like an app rather than a user.

    The test is the config file. A user directory never has one at its top
    level, and an app directory always does, so the two are told apart by
    something structural instead of by a naming convention.
    """
    if not projects.is_dir():
        return []
    return sorted(p for p in projects.iterdir() if p.is_dir() and (p / CONFIG_NAME).is_file())


def adopt(projects: Path, owner_id: str, on_log: Callable[[str], None] = print) -> int:
    """Move every loose app under `owner_id`. Returns how many moved."""
    stray = legacy_apps(projects)
    if not stray:
        return 0

    home = projects / owner_id
    home.mkdir(parents=True, exist_ok=True)
    moved = 0
    for source in stray:
        target = home / source.name
        if target.exists():
            # Never clobber. An app already sitting in the right place under
            # this name is somebody's real work, and the loose one is the
            # unexplained thing, so the loose one gets renamed.
            suffix = 2
            while (home / f"{source.name}-{suffix}").exists() and suffix < 100:
                suffix += 1
            target = home / f"{source.name}-{suffix}"
        shutil.move(str(source), str(target))
        on_log(f"  moved {source.name} -> {owner_id}/{target.name}")
        moved += 1
    return moved
