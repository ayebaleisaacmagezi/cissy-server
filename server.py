#!/usr/bin/env python3
"""Entry point.

    python3 server.py                  start the server
    python3 server.py --check-collecto prove the payment credentials work from
                                       this machine, and change nothing

Reads its configuration from the environment:

    CISSY_HOST       interface to bind (default 127.0.0.1)
    CISSY_PORT       port to bind (default 8080)
    CISSY_ROOT       where projects/ and accounts.db live (default alongside this)
    CISSY_PASSWORD   legacy shared password. No longer how anyone signs in; kept
                     only so an old deployment does not refuse to start.

The first account has to come from somewhere, so it comes from here:

    CISSY_ADMIN_PHONE     e.g. 0772000000
    CISSY_ADMIN_PASSWORD  at least 8 characters
    CISSY_ADMIN_NAME      optional, defaults to "Admin"

Set on the very first run only. It creates a verified admin account and adopts
any apps left over from before there were accounts. After that it does nothing.

Payments and SMS run against simulators unless Collecto is configured, so the
whole flow works before there is an account:

    CISSY_COLLECTO_USERNAME   Collecto account username
    CISSY_COLLECTO_KEY        x-api-key, issued against this machine's IP
    CISSY_COLLECTO_REFERER    sent on every call; their WAF requires one
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from cissy import __version__, migrate, toolchain
from cissy.errors import CissyError
from cissy.webapp import Application, serve

HERE = Path(__file__).resolve().parent


def bootstrap(app: Application, root: Path) -> None:
    """Create the first account, if one was asked for and none exists.

    Deliberately does nothing when users already exist. An env var left set in
    a start script must not be able to resurrect or re-password an admin
    account on every restart.
    """
    phone = os.environ.get("CISSY_ADMIN_PHONE", "").strip()
    password = os.environ.get("CISSY_ADMIN_PASSWORD", "")
    if not phone or not password:
        return
    if app.accounts.count():
        return

    from cissy.payments import normalise_phone

    try:
        admin = app.accounts.create_user(
            name=os.environ.get("CISSY_ADMIN_NAME", "Admin"),
            phone=normalise_phone(phone),
            password=password,
            is_admin=True,
            verified=True,
        )
    except CissyError as error:
        print(f"Could not create the admin account: {error.message}", file=sys.stderr)
        return

    print(f"Created admin account {admin.id} on {admin.phone}")
    moved = migrate.adopt(root / "projects", admin.id)
    if moved:
        print(f"Adopted {moved} app(s) left over from before accounts existed.")


def check_collecto() -> int:
    """Answer "will payments work from this box" without charging anybody.

    Its own command because the key is tied to a source IP, so this is a
    question about the machine as much as about the credentials, and it has to
    be asked from the machine itself after every move or deploy.
    """
    from cissy.collecto import Settings, checkup

    ok, lines = checkup(Settings.from_env())
    for line in lines:
        print(line, file=sys.stdout if ok else sys.stderr)
    return 0 if ok else 1


def main() -> int:
    if "--check-collecto" in sys.argv[1:]:
        return check_collecto()

    host = os.environ.get("CISSY_HOST", "127.0.0.1")
    port = int(os.environ.get("CISSY_PORT", "8080"))
    root = Path(os.environ.get("CISSY_ROOT", HERE))
    password = os.environ.get("CISSY_PASSWORD") or None

    app = Application(root=root, web_dir=HERE / "web", password=password)
    bootstrap(app, root)

    if not app.accounts.count() and host not in ("127.0.0.1", "localhost", "::1"):
        # An empty server on a public address is one where the first stranger
        # to find it becomes the admin. Better to refuse and say how to fix it.
        print(
            "Refusing to listen on a public address with no accounts.\n"
            "Create the first one by starting once with:\n"
            "    CISSY_ADMIN_PHONE=0772000000 CISSY_ADMIN_PASSWORD=... python3 server.py",
            file=sys.stderr,
        )
        return 2

    httpd = serve(app, host, port)

    state = toolchain.probe()
    print(f"CissyWeb2App {__version__} on http://{host}:{port}")
    print(f"Toolchain: {state.summary}")
    print(f"Accounts: {app.accounts.count()}")
    if app.collecto_settings.live:
        print(f"Payments and SMS: live, as {app.collecto_settings.username}")
    else:
        print("Payments and SMS: demo. No money moves and no texts are sent.")
    if not state.ok:
        # Not fatal - the UI is still worth reaching, and it explains what is
        # missing far better than a failed build would.
        for tool in state.tools:
            if not tool.ok:
                print(f"  ! {tool.name}: {tool.detail}")

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping.")
    finally:
        httpd.server_close()
        app.accounts.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
