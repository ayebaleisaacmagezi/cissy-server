#!/usr/bin/env python3
"""Entry point.

    python3 server.py

Reads its configuration from the environment:

    CISSY_PASSWORD   shared password. Without it the server refuses to listen
                     on anything but localhost.
    CISSY_HOST       interface to bind (default 127.0.0.1)
    CISSY_PORT       port to bind (default 8080)
    CISSY_ROOT       where projects/ lives (default alongside this file)
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from cissy import __version__, toolchain
from cissy.webapp import Application, serve

HERE = Path(__file__).resolve().parent


def main() -> int:
    host = os.environ.get("CISSY_HOST", "127.0.0.1")
    port = int(os.environ.get("CISSY_PORT", "8080"))
    root = Path(os.environ.get("CISSY_ROOT", HERE))
    password = os.environ.get("CISSY_PASSWORD") or None

    # Refusing rather than warning: this endpoint runs builds and handles
    # signing keys, and an unauthenticated one reachable from the internet is
    # a problem you find out about too late.
    if password is None and host not in ("127.0.0.1", "localhost", "::1"):
        print(
            f"Refusing to listen on {host} without a password.\n"
            f"Set CISSY_PASSWORD, or bind to 127.0.0.1 and reach it over an "
            f"SSH tunnel:\n"
            f"    ssh -L {port}:127.0.0.1:{port} you@your-server",
            file=sys.stderr,
        )
        return 2

    app = Application(root=root, web_dir=HERE / "web", password=password)
    httpd = serve(app, host, port)

    state = toolchain.probe()
    print(f"Cissy {__version__} on http://{host}:{port}")
    print(f"Toolchain: {state.summary}")
    if not state.ok:
        # Not fatal — the UI is still worth reaching, and it explains what is
        # missing far better than a failed build would.
        for tool in state.tools:
            if not tool.ok:
                print(f"  ! {tool.name}: {tool.detail}")
    if password is None:
        print("No password set — localhost only.")

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping.")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
