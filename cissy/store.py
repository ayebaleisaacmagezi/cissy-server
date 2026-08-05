"""Where apps live on disk, and how they are read and written.

One directory per app, inside one directory per user:

    projects/<user-id>/<app-id>/
      config.json      the manifest
      assets/          icon, splash, keystore
      generated/       the Flutter project, kept warm between builds
      builds/<n>/      artifacts and log for a single build

A `ProjectStore` is rooted at one user's directory, so it cannot name another
user's app: `app_dir` joins a single sanitised segment and `_safe_id` rejects
anything containing a slash. That makes ownership structural rather than a
check somebody has to remember. Miss one check with a flat layout and a
customer downloads another customer's keystore, which is a key Google will
never reset.

`Workspaces` below hands out one store per user.

Nothing here knows about HTTP; the web layer turns these calls into responses.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

from .config import AppConfig, normalise, slugify, validate
from .errors import ConflictError, NotFoundError, ValidationError

CONFIG_NAME = "config.json"


class ProjectStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    # ── paths ────────────────────────────────────────────────────────────

    def app_dir(self, app_id: str) -> Path:
        return self.root / _safe_id(app_id)

    def assets_dir(self, app_id: str) -> Path:
        return self.app_dir(app_id) / "assets"

    def generated_dir(self, app_id: str) -> Path:
        return self.app_dir(app_id) / "generated"

    def builds_dir(self, app_id: str) -> Path:
        return self.app_dir(app_id) / "builds"

    def build_dir(self, app_id: str, build_number: int) -> Path:
        return self.builds_dir(app_id) / str(build_number)

    def dist_dir(self, app_id: str) -> Path:
        return self.app_dir(app_id) / "dist"

    # ── reading ──────────────────────────────────────────────────────────

    def exists(self, app_id: str) -> bool:
        return (self.app_dir(app_id) / CONFIG_NAME).is_file()

    def get(self, app_id: str) -> AppConfig:
        path = self.app_dir(app_id) / CONFIG_NAME
        if not path.is_file():
            raise NotFoundError(f'No app called "{app_id}" on this server.')
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            raise ValidationError(
                f"The project file for {app_id} is not valid JSON. It may have "
                f"been edited by hand.",
                detail=str(error),
            ) from error
        return AppConfig.from_json(data)

    def list(self) -> list[AppConfig]:
        """Every readable app, newest first.

        A single corrupt config.json must not hide every other app, so
        unreadable directories are skipped rather than raised.
        """
        apps: list[AppConfig] = []
        for entry in sorted(self.root.iterdir()):
            if not (entry / CONFIG_NAME).is_file():
                continue
            try:
                apps.append(self.get(entry.name))
            except (ValidationError, NotFoundError):
                continue
        apps.sort(key=lambda app: app.updated_at, reverse=True)
        return apps

    # ── writing ──────────────────────────────────────────────────────────

    def create(self, config: AppConfig) -> AppConfig:
        config = normalise(config)
        validate(config)

        app_id = self._unique_id(config.id or slugify(config.name))
        config = config.updated(id=app_id)

        directory = self.app_dir(app_id)
        directory.mkdir(parents=True, exist_ok=False)
        (directory / "assets").mkdir(exist_ok=True)
        self._write(config)
        return config

    def save(self, config: AppConfig) -> AppConfig:
        config = normalise(config)
        validate(config)
        if not self.app_dir(config.id).is_dir():
            raise NotFoundError(f'No app called "{config.id}" on this server.')
        config = config.updated()
        self._write(config)
        return config

    def duplicate(self, app_id: str, new_name: str) -> AppConfig:
        """Copy an app's settings and assets under a new name.

        Build history is deliberately not copied — it belongs to the original —
        and neither is the keystore, since a new app needs its own signing
        identity and silently sharing one would be the wrong default.
        """
        source = self.get(app_id)
        new_id = self._unique_id(slugify(new_name))

        copy = normalise(
            source.updated(
                id=new_id,
                name=new_name,
                app_name=new_name,
                version_code=1,
                version_name="1.0.0",
                keystore_file=None,
                key_alias=None,
            )
        )
        validate(copy)

        directory = self.app_dir(new_id)
        directory.mkdir(parents=True, exist_ok=False)
        (directory / "assets").mkdir(exist_ok=True)

        for name in (source.icon_file, source.splash_file):
            if not name:
                continue
            origin = self.assets_dir(app_id) / name
            if origin.is_file():
                shutil.copy2(origin, directory / "assets" / name)

        self._write(copy)
        return copy

    def delete(self, app_id: str) -> None:
        directory = self.app_dir(app_id)
        if not directory.is_dir():
            raise NotFoundError(f'No app called "{app_id}" on this server.')
        shutil.rmtree(directory)

    # ── builds ───────────────────────────────────────────────────────────

    def next_build_number(self, app_id: str) -> int:
        builds = self.builds_dir(app_id)
        if not builds.is_dir():
            return 1
        numbers = [int(p.name) for p in builds.iterdir() if p.name.isdigit()]
        return max(numbers, default=0) + 1

    def build_numbers(self, app_id: str) -> list[int]:
        builds = self.builds_dir(app_id)
        if not builds.is_dir():
            return []
        return sorted(
            (int(p.name) for p in builds.iterdir() if p.name.isdigit()),
            reverse=True,
        )

    def used_version_codes(self, app_id: str) -> set[int]:
        """Version codes this app has already built under.

        Read from the build records rather than tracked on the config, because
        the config only remembers the most recent one. Play refuses an upload
        that reuses any earlier code, not just the last.
        """
        codes: set[int] = set()
        for number in self.build_numbers(app_id):
            record = self.build_dir(app_id, number) / "build.json"
            try:
                data = json.loads(record.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            code = data.get("version_code")
            if isinstance(code, int):
                codes.add(code)
        return codes

    # ── internals ────────────────────────────────────────────────────────

    def _write(self, config: AppConfig) -> None:
        """Write via a temp file so a crash cannot leave a truncated config."""
        path = self.app_dir(config.id) / CONFIG_NAME
        tmp = path.with_suffix(".json.tmp")
        payload = json.dumps(config.to_json(), indent=2, ensure_ascii=False)
        tmp.write_text(payload + "\n", encoding="utf-8")
        os.replace(tmp, path)

    def _unique_id(self, base: str) -> str:
        base = _safe_id(base)
        if not self.app_dir(base).exists():
            return base
        for suffix in range(2, 100):
            candidate = f"{base}-{suffix}"
            if not self.app_dir(candidate).exists():
                return candidate
        raise ConflictError(
            f'Too many apps are already called "{base}". Pick another name.'
        )


class Workspaces:
    """One `ProjectStore` per user, rooted at that user's directory.

    Cached because `ProjectStore.__init__` creates its directory, and a fresh
    one per request would mkdir on every call. The cache is keyed by the
    sanitised id, so a bad id never reaches the filesystem at all.
    """

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._stores: dict[str, ProjectStore] = {}

    def for_user(self, user_id: str) -> ProjectStore:
        safe = _safe_id(user_id)
        store = self._stores.get(safe)
        if store is None:
            store = ProjectStore(self.root / safe)
            self._stores[safe] = store
        return store

    def user_ids(self) -> list[str]:
        """Every user directory that exists on disk.

        Used by the admin view and by disk accounting. The database is the
        authority on who exists; this is the authority on who has taken space.
        """
        if not self.root.is_dir():
            return []
        return sorted(p.name for p in self.root.iterdir() if p.is_dir())

    def disk_used(self, user_id: str) -> int:
        directory = self.root / _safe_id(user_id)
        if not directory.is_dir():
            return 0
        total = 0
        for path in directory.rglob("*"):
            try:
                if path.is_file():
                    total += path.stat().st_size
            except OSError:
                continue
        return total


def _safe_id(app_id: str) -> str:
    """Reject anything that could escape the directory it is joined to.

    Ids arrive from URLs, so `../secrets`, `a/b` or an absolute path would
    otherwise let a request read or write outside its own tree. Rejecting rather
    than silently cleaning means a typo fails loudly instead of hitting the
    wrong app, and it is what keeps one user's store from naming another's.
    """
    cleaned = slugify(app_id)
    if cleaned != app_id.strip().lower():
        raise ValidationError(f'"{app_id}" is not a valid id.')
    return cleaned
