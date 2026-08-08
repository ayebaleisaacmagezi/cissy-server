"""Running external commands and getting their output out line by line.

Everything the server actually does is a subprocess - `flutter`, `zip`,
`keytool`. Two shapes cover all of it: stream a long-running command's output
somewhere, or capture a short one's.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Callable, Iterable, Mapping

from .errors import ToolchainError

LogSink = Callable[[str], None]


def resolve(program: str) -> str:
    """Find an executable, or explain what to install.

    Resolved rather than run through a shell so that a path containing a space
    or an unexpected `PATH` entry cannot change which binary runs. On Windows
    this also picks up `flutter.bat`, which a bare exec would miss.
    """
    found = shutil.which(program)
    if not found:
        raise ToolchainError(
            f'"{program}" is not installed, or not on this server\'s PATH.',
            detail=(
                "Flutter, the Android SDK and a JDK must all be reachable from "
                "the account running Cissy."
            ),
        )
    return found


def stream(
    command: Iterable[str],
    *,
    cwd: Path | None = None,
    on_line: LogSink,
    env: Mapping[str, str] | None = None,
) -> int:
    """Run a command, passing each output line to `on_line` as it arrives.

    stderr is folded into stdout: Gradle and Flutter both write progress to one
    and errors to the other, and interleaving them in order is what makes the
    log readable.
    """
    argv = list(command)
    argv[0] = resolve(argv[0])

    process = subprocess.Popen(
        argv,
        cwd=str(cwd) if cwd else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=dict(env) if env else None,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )

    assert process.stdout is not None
    for line in process.stdout:
        on_line(line.rstrip("\n"))

    return process.wait()


def capture(command: Iterable[str], *, timeout: float = 60.0) -> tuple[int, str]:
    """Run a short command and return its exit code and combined output."""
    argv = list(command)
    argv[0] = resolve(argv[0])
    try:
        completed = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise ToolchainError(
            f"{argv[0]} did not respond within {timeout:.0f} seconds."
        ) from None
    return completed.returncode, (completed.stdout or "") + (completed.stderr or "")
