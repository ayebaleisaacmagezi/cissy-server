"""Running a build and reporting what happened.

Builds take minutes, so they run on a background thread and their output is
streamed. One at a time, enforced by a lock — a 4 GB box cannot run two Gradle
builds, and a queue is more machinery than a single-user server needs.

The classifier at the bottom is the difference between a usable tool and a wall
of Gradle output. Every rule there corresponds to a failure that is otherwise
genuinely hard to diagnose from the log.
"""

from __future__ import annotations

import json
import re
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Literal

from . import archive, generate, signing, template
from .config import AppConfig
from .errors import ConflictError
from .process import stream
from .signing import SigningCredentials
from .store import ProjectStore

Output = Literal["apk", "aab"]
Status = Literal["running", "succeeded", "failed"]


@dataclass
class Artifact:
    name: str
    path: Path
    size: int
    kind: str

    def to_json(self) -> dict[str, object]:
        return {
            "name": self.name,
            "kind": self.kind,
            "size": self.size,
        }


@dataclass
class Build:
    """One build, its log, and whatever it produced."""

    number: int
    app_id: str
    output: Output
    version_name: str
    version_code: int
    signed: bool
    status: Status = "running"
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    error: str | None = None
    hint: str | None = None
    lines: list[str] = field(default_factory=list)
    artifacts: list[Artifact] = field(default_factory=list)

    _subscribers: list[Callable[[str], None]] = field(default_factory=list, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def duration(self) -> float:
        return (self.finished_at or time.time()) - self.started_at

    def log(self, line: str) -> None:
        with self._lock:
            self.lines.append(line)
            subscribers = list(self._subscribers)
        for send in subscribers:
            try:
                send(line)
            except Exception:  # noqa: BLE001 - a dead client must not stop a build
                pass

    def subscribe(self, send: Callable[[str], None]) -> list[str]:
        """Attach a listener and return the log so far, without a gap."""
        with self._lock:
            self._subscribers.append(send)
            return list(self.lines)

    def unsubscribe(self, send: Callable[[str], None]) -> None:
        with self._lock:
            if send in self._subscribers:
                self._subscribers.remove(send)

    def to_json(self) -> dict[str, object]:
        return {
            "number": self.number,
            "app_id": self.app_id,
            "status": self.status,
            "output": self.output,
            "version_name": self.version_name,
            "version_code": self.version_code,
            "signed": self.signed,
            "started_at": self.started_at,
            "duration": round(self.duration, 1),
            "error": self.error,
            "hint": self.hint,
            "artifacts": [a.to_json() for a in self.artifacts],
        }


class BuildRunner:
    """Owns the one-at-a-time rule and the currently running build."""

    def __init__(self, store: ProjectStore) -> None:
        self.store = store
        self._lock = threading.Lock()
        self._current: Build | None = None
        self._history: dict[tuple[str, int], Build] = {}

    @property
    def current(self) -> Build | None:
        return self._current

    def get(self, app_id: str, number: int) -> Build | None:
        return self._history.get((app_id, number))

    def start(
        self,
        config: AppConfig,
        *,
        output: Output,
        credentials: SigningCredentials | None,
    ) -> Build:
        with self._lock:
            if self._current and self._current.status == "running":
                running = self._current
                raise ConflictError(
                    f"A build of {running.app_id} is already running. This "
                    f"server runs one at a time — Gradle needs most of the "
                    f"available memory."
                )
            number = self.store.next_build_number(config.id)
            build = Build(
                number=number,
                app_id=config.id,
                output=output,
                version_name=config.version_name,
                version_code=config.version_code,
                signed=credentials is not None,
            )
            self._current = build
            self._history[(config.id, number)] = build

        thread = threading.Thread(
            target=self._run,
            args=(build, config, credentials),
            daemon=True,
            name=f"build-{config.id}-{number}",
        )
        thread.start()
        return build

    # ── the build itself ─────────────────────────────────────────────────

    def _run(
        self,
        build: Build,
        config: AppConfig,
        credentials: SigningCredentials | None,
    ) -> None:
        project_dir = self.store.generated_dir(config.id)
        try:
            if credentials is None:
                build.log(
                    "WARNING: no keystore configured. This build will be signed "
                    "with a debug key and Google Play will reject it."
                )

            generate.generate(config, self.store, build.log)
            signing.apply(project_dir, credentials)

            build.log("$ flutter pub get")
            self._flutter(build, project_dir, ["pub", "get"])

            self._generate_icons(build, config, project_dir)

            args = self._build_args(build, config)
            build.log("$ flutter " + " ".join(args))
            self._flutter(build, project_dir, args)

            self._collect(build, config, project_dir)
            build.status = "succeeded"
            build.log(f"Done in {build.duration:.0f}s")

        except Exception as error:  # noqa: BLE001 - reported, not swallowed
            build.status = "failed"
            build.error = str(error)
            build.hint = classify("\n".join(build.lines[-400:]))
            build.log(f"ERROR: {error}")
            if build.hint:
                build.log(build.hint)
        finally:
            # The passwords must not outlive the build that needed them.
            signing.cleanup(project_dir)
            build.finished_at = time.time()
            self._record(build)
            with self._lock:
                if self._current is build:
                    self._current = None

    def _record(self, build: Build) -> None:
        """Write the outcome and the log to disk.

        In-memory state dies with the process, and the build history is most of
        what the overview screen is for. A failure to record must not turn a
        successful build into a reported failure, so this never raises.
        """
        try:
            directory = self.store.build_dir(build.app_id, build.number)
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "build.json").write_text(
                json.dumps(build.to_json(), indent=2) + "\n", encoding="utf-8"
            )
            (directory / "log.txt").write_text(
                "\n".join(build.lines) + "\n", encoding="utf-8"
            )
        except OSError:
            pass

    def _build_args(self, build: Build, config: AppConfig) -> list[str]:
        target = "appbundle" if build.output == "aab" else "apk"
        args = [
            "build",
            target,
            "--release",
            f"--build-name={config.version_name}",
            f"--build-number={config.version_code}",
        ]
        if target == "apk":
            # A universal APK carries native code for every architecture, which
            # is most of its size — around 40 MB where a single-architecture
            # build is nearer 15 MB. Play never sees these anyway; the bundle
            # is what gets uploaded, and it splits by architecture on its own.
            args.append("--split-per-abi")
        return args

    def _generate_icons(self, build: Build, config: AppConfig, cwd: Path) -> None:
        """Turn the uploaded icon into the sizes Android and iOS actually read.

        Deliberately not fatal. An app whose icon failed to generate is still a
        working app, and failing an otherwise good build over it would be worse
        than shipping the default icon — but it is said loudly, because a
        silently ignored icon is exactly the complaint this was written to fix.
        """
        if not config.icon_file:
            return

        build.log("$ dart run flutter_launcher_icons")
        try:
            code = stream(["dart", "run", "flutter_launcher_icons"], cwd=cwd, on_line=build.log)
        except Exception as error:  # noqa: BLE001 - never sink a whole build
            build.log(f"WARNING: could not run flutter_launcher_icons ({error}).")
            build.log("WARNING: the app will be built with the default Flutter icon.")
            return

        if code != 0:
            build.log(
                "WARNING: icon generation failed, so this build keeps the "
                "default Flutter icon. A PNG at least 512x512 with no "
                "transparency is the usual fix."
            )
        else:
            build.log("Launcher icons generated for Android and iOS.")

    def _flutter(self, build: Build, cwd: Path, args: list[str]) -> None:
        code = stream(["flutter", *args], cwd=cwd, on_line=build.log)
        if code != 0:
            raise RuntimeError(f"flutter {args[0]} exited with code {code}")

    def _collect(self, build: Build, config: AppConfig, project_dir: Path) -> None:
        """Copy the artifacts out of build/ and make the project archive.

        Copied rather than referenced because the next build overwrites
        `build/`, and a download link that quietly starts serving a different
        version is worse than one that 404s.
        """
        destination = self.store.build_dir(config.id, build.number)
        destination.mkdir(parents=True, exist_ok=True)

        if build.output == "aab":
            found = sorted(
                (project_dir / "build" / "app" / "outputs" / "bundle").rglob("*.aab")
            )
            kind = "aab"
        else:
            found = sorted(
                (project_dir / "build" / "app" / "outputs" / "flutter-apk").glob("*.apk"),
                key=_abi_order,
            )
            kind = "apk"

        # The output directory survives between builds, and a change of build
        # flags changes the filenames — so a universal app-release.apk from an
        # earlier build would sit alongside today's split ones and be offered as
        # if it were current. Anything older than this build is not ours.
        fresh = [p for p in found if p.stat().st_mtime >= build.started_at]
        if fresh:
            found = fresh

        if not found:
            raise RuntimeError(
                "The build reported success but produced no artifact. The log "
                "above is the best place to look."
            )

        for source in found:
            target = destination / source.name
            shutil.copy2(source, target)
            build.artifacts.append(
                Artifact(
                    name=source.name,
                    path=target,
                    size=target.stat().st_size,
                    kind=kind,
                )
            )
            build.log(f"Saved {source.name} ({_megabytes(target)})")

        build.log("Packaging the project archive...")
        zip_path = archive.write_archive(
            project_dir,
            destination / f"{config.id}.zip",
            root_name=template.project_name(config),
        )
        build.artifacts.append(
            Artifact(
                name=zip_path.name,
                path=zip_path,
                size=zip_path.stat().st_size,
                kind="zip",
            )
        )
        build.log(f"Saved {zip_path.name} ({_megabytes(zip_path)}) — open this on a Mac for iOS")


# Nearly every phone in use is arm64, so it goes first — it is the one to send
# someone who just wants to install the app.
_ABI_PRIORITY = ("arm64-v8a", "armeabi-v7a", "x86_64", "x86")


def _abi_order(path: Path) -> tuple[int, str]:
    for index, abi in enumerate(_ABI_PRIORITY):
        if abi in path.name:
            return (index, path.name)
    return (len(_ABI_PRIORITY), path.name)


def _megabytes(path: Path) -> str:
    return f"{path.stat().st_size / 1_048_576:.1f} MB"


# ── failure classification ───────────────────────────────────────────────

# Each rule exists because the raw log for that failure is misleading or buries
# the cause hundreds of lines up. Ordered most-specific first.
_RULES: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(r"heap space|OutOfMemoryError|Expiring Daemon because JVM", re.I),
        "Gradle ran out of memory. This server has limited RAM — lower "
        "org.gradle.jvmargs in the generated android/gradle.properties, or add "
        "swap. Nothing on the server was changed.",
    ),
    (
        re.compile(r"No space left on device|ENOSPC", re.I),
        "The disk is full. Old build artifacts are the usual cause — delete "
        "some builds and try again.",
    ),
    (
        re.compile(r"licen[cs]e.*not.*accepted|You have not accepted the license", re.I),
        "The Android SDK licences have not been accepted for the account "
        "running this server. Run: yes | sdkmanager --licenses",
    ),
    (
        re.compile(r"Failed to read key .* from store|Keystore was tampered", re.I),
        "The keystore password or key alias is wrong. They are not stored "
        "between builds, so check what you typed for this one.",
    ),
    (
        re.compile(r"keystore password was incorrect|Given final block not properly", re.I),
        "The keystore password is wrong.",
    ),
    (
        re.compile(r"SDK location not found|ANDROID_HOME|sdk\.dir", re.I),
        "The Android SDK cannot be found. ANDROID_HOME must be set for the "
        "account running this server, not just in an interactive shell.",
    ),
    (
        re.compile(r"getDefaultProguardFile\('proguard-android\.txt'\)", re.I),
        "The WebView plugin is too old for the Android Gradle Plugin this "
        "Flutter version installs. AGP 9 removed a ProGuard setting that "
        "flutter_inappwebview 6.1.5 still uses, and the fix is only in the "
        "6.2.0 beta. Nothing in your app configuration caused this.",
    ),
    (
        re.compile(r"A problem occurred evaluating project ':flutter_", re.I),
        "A Flutter plugin failed to configure itself for this Android Gradle "
        "Plugin version. The plugin and the line number are named just above "
        "this in the log — it is a toolchain mismatch, not your configuration.",
    ),
    (
        re.compile(r"Could not resolve|Could not GET|Connection timed out", re.I),
        "A dependency could not be downloaded. This is usually the server's "
        "network rather than anything in your configuration — try again.",
    ),
    (
        re.compile(r"Unsupported class file major version|invalid source release", re.I),
        "The JDK version does not match what the Android Gradle Plugin expects. "
        "JDK 17 is the safe choice.",
    ),
    (
        re.compile(r"Execution failed for task ':app:lint", re.I),
        "The Android lint step failed. The specific complaint is in the log "
        "just above this line.",
    ),
]


def classify(log: str) -> str | None:
    """A plain sentence explaining a failure, or None if it is not recognised.

    Returning None matters: a wrong explanation on top of a real log is worse
    than no explanation, so unrecognised failures are left to speak for
    themselves.
    """
    for pattern, message in _RULES:
        if pattern.search(log):
            return message
    return None
