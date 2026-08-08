"""What this server can actually do, reported from the machine itself.

Every build failure is either "the toolchain is missing" or "the build genuinely
broke", and the two look identical in a Gradle log. This module exists so the
first case is answered before a build is ever started.

Nothing here is hardcoded - the versions come from running the tools.
"""

from __future__ import annotations

import os
import re
import shutil
import threading
from dataclasses import dataclass, field
from pathlib import Path

from .process import capture

_FLUTTER_RE = re.compile(r"Flutter\s+(\S+)")
_DART_RE = re.compile(r"Dart\s+(?:SDK\s+version:\s+)?(\S+)")
_JAVA_RE = re.compile(r'version "?([\d._]+)"?')

# Probing runs several subprocesses and is slow enough to notice, but the answer
# only changes when someone installs something. Cached until asked to refresh.
_lock = threading.Lock()
_cached: "Toolchain | None" = None


@dataclass(frozen=True)
class Tool:
    name: str
    ok: bool
    version: str = ""
    detail: str = ""

    def to_json(self) -> dict[str, object]:
        return {
            "name": self.name,
            "ok": self.ok,
            "version": self.version,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class Toolchain:
    tools: tuple[Tool, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return all(tool.ok for tool in self.tools)

    @property
    def summary(self) -> str:
        if self.ok:
            versions = [f"{t.name} {t.version}" for t in self.tools if t.version]
            return " · ".join(versions) if versions else "Ready"
        missing = [tool.name for tool in self.tools if not tool.ok]
        return f"Not ready - {', '.join(missing)}"

    def to_json(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "summary": self.summary,
            "tools": [tool.to_json() for tool in self.tools],
        }


def probe(refresh: bool = False) -> Toolchain:
    global _cached
    with _lock:
        if _cached is None or refresh:
            _cached = Toolchain(tools=(_flutter(), _java(), _android()))
        return _cached


def _flutter() -> Tool:
    if not shutil.which("flutter"):
        return Tool(
            "Flutter",
            ok=False,
            detail="Not on PATH. Install the Flutter SDK and add its bin/ to PATH.",
        )
    code, output = capture(["flutter", "--version"], timeout=120)
    if code != 0:
        return Tool("Flutter", ok=False, detail=_first_line(output))
    match = _FLUTTER_RE.search(output)
    dart = _DART_RE.search(output)
    version = match.group(1) if match else "unknown"
    detail = f"Dart {dart.group(1)}" if dart else ""
    return Tool("Flutter", ok=True, version=version, detail=detail)


def _java() -> Tool:
    if not shutil.which("java"):
        return Tool(
            "Java",
            ok=False,
            detail="Not on PATH. Android builds need a JDK - 17 is the safe choice.",
        )
    code, output = capture(["java", "-version"], timeout=30)
    if code != 0:
        return Tool("Java", ok=False, detail=_first_line(output))
    match = _JAVA_RE.search(output)
    version = match.group(1) if match else "unknown"

    major = version.split(".")[0]
    if major.isdigit() and int(major) < 17:
        return Tool(
            "Java",
            ok=False,
            version=version,
            detail="Android Gradle Plugin 8+ needs JDK 17 or newer.",
        )
    return Tool("Java", ok=True, version=version)


def _android() -> Tool:
    """Check the SDK by looking for it, not by running sdkmanager.

    `sdkmanager --list` reaches the network and can take tens of seconds, which
    is far too slow for something the UI polls. The directories being present is
    a good enough signal; a genuinely broken SDK surfaces in the build log.
    """
    home = os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT")
    if not home:
        return Tool(
            "Android SDK",
            ok=False,
            detail="ANDROID_HOME is not set for the account running Cissy.",
        )

    root = Path(home)
    if not root.is_dir():
        return Tool(
            "Android SDK",
            ok=False,
            detail=f"ANDROID_HOME points at {home}, which does not exist.",
        )

    platforms = root / "platforms"
    installed = (
        sorted(p.name for p in platforms.iterdir() if p.is_dir())
        if platforms.is_dir()
        else []
    )
    if not installed:
        return Tool(
            "Android SDK",
            ok=False,
            detail=(
                "No platforms installed. Run: sdkmanager "
                '"platform-tools" "platforms;android-36" "build-tools;36.0.0"'
            ),
        )

    licences = root / "licenses"
    if not licences.is_dir():
        return Tool(
            "Android SDK",
            ok=False,
            version=installed[-1],
            detail="Licences not accepted. Run: yes | sdkmanager --licenses",
        )

    return Tool("Android SDK", ok=True, version=installed[-1], detail=str(root))


def _first_line(text: str) -> str:
    for line in text.splitlines():
        if line.strip():
            return line.strip()
    return "No output."
