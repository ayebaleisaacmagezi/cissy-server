"""HTTP layer: routing, static files, and turning errors into readable JSON.

A hand-rolled router on top of `http.server` rather than a framework. The API
is a dozen endpoints and the only awkward requirement - streaming a build log -
is easier to do directly than through most frameworks anyway.

Uploads arrive as raw PUT bodies rather than multipart forms. That is the one
concession to having no dependencies, and it costs a single line in the browser:
`fetch(url, {method: 'PUT', body: file})`.
"""

from __future__ import annotations

import json
import mimetypes
import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from queue import Empty, Queue
from typing import Any, Callable
from urllib.parse import parse_qs, unquote, urlparse

from .accounts import Accounts, AuthError, ForbiddenError, User
from .build import BuildRunner
from .collecto import CollectoGateway, DemoGateway, Settings as CollectoSettings
from .config import AppConfig, slugify
from .errors import CissyError, ConflictError, NotFoundError, ValidationError
from .payments import PLANS, PaymentService, PaymentStore, Subscription
from .signing import SigningCredentials
from .sms import DemoSms, build_sender, verification_message
from .store import ProjectStore, Workspaces
from . import accounts as accounts_mod
from . import generate, toolchain

# Windows' MIME registry predates WebP; without this the logos are served as
# application/octet-stream.
mimetypes.add_type("image/webp", ".webp")

# Routes anybody may reach without being signed in. Everything else needs a
# session. An allow-list rather than a deny-list, because the failure mode of
# forgetting to add a route here is a locked door, and the failure mode of
# forgetting to remove one is an open one.
PUBLIC_ROUTES = frozenset({
    "/api/health",
    "/api/auth/signup",
    "/api/auth/verify",
    "/api/auth/resend",
    "/api/auth/login",
    "/api/auth/session",
})

SESSION_COOKIE = "cissy_session"

# 20 MB covers an icon, a splash and a keystore many times over, and stops a
# stray request from filling memory.
MAX_BODY = 20 * 1024 * 1024


@dataclass
class Request:
    method: str
    path: str
    query: dict[str, list[str]]
    params: dict[str, str]
    body: bytes
    headers: Any
    user: User | None = None
    store: ProjectStore | None = None
    ip: str = ""

    @property
    def owner(self) -> User:
        """The signed-in user, or a 401.

        Handlers reach for this rather than checking for None themselves, so a
        forgotten check is a type error at the call site instead of a silent
        `None` reaching the filesystem.
        """
        if self.user is None:
            raise AuthError("Sign in first.")
        return self.user

    @property
    def workspace(self) -> ProjectStore:
        if self.store is None:
            raise AuthError("Sign in first.")
        return self.store

    def json(self) -> dict[str, Any]:
        if not self.body:
            return {}
        try:
            data = json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValidationError("The request body is not valid JSON.") from error
        if not isinstance(data, dict):
            raise ValidationError("The request body must be a JSON object.")
        return data


@dataclass
class Cookie:
    """A response plus the session cookie to set or clear.

    Kept as its own type because the dispatcher writes headers, and until now
    nothing on this server had ever needed to send one.
    """

    payload: dict[str, Any]
    token: str | None = None
    clear: bool = False
    max_age: int = accounts_mod.SESSION_DAYS * 86400


@dataclass
class FileResponse:
    """Send a file back - as a download by default, inline for previews."""

    path: Path
    filename: str
    media_type: str = "application/octet-stream"
    inline: bool = False


@dataclass
class EventStream:
    """Stream a build's log as server-sent events."""

    build: Any


Handler = Callable[[Request], Any]


class Router:
    def __init__(self) -> None:
        self._routes: list[tuple[str, re.Pattern[str], Handler]] = []

    def add(self, method: str, pattern: str, handler: Handler) -> None:
        regex = re.compile("^" + re.sub(r"<(\w+)>", r"(?P<\1>[^/]+)", pattern) + "$")
        self._routes.append((method, regex, handler))

    def match(self, method: str, path: str) -> tuple[Handler, dict[str, str]] | None:
        allowed = False
        for route_method, regex, handler in self._routes:
            found = regex.match(path)
            if not found:
                continue
            if route_method != method:
                allowed = True
                continue
            return handler, found.groupdict()
        if allowed:
            raise CissyError(f"{method} is not allowed on this address.")
        return None


class Application:
    """Owns the store, the routes and the shared password."""

    def __init__(
        self,
        *,
        root: Path,
        web_dir: Path,
        password: str | None,
        collecto: CollectoSettings | None = None,
    ) -> None:
        self.root = Path(root)
        self.workspaces = Workspaces(root / "projects")
        self.accounts = Accounts(root / "accounts.db")
        self.web_dir = web_dir
        # Kept only so an existing single-user deployment does not lock itself
        # out on upgrade. It is not how customers sign in.
        self.password = password
        self.builds = BuildRunner()

        # Live only when both a username and a key are present. The default is
        # the simulator rather than a half-configured client, because a client
        # missing its key fails at the worst possible moment - mid-payment -
        # instead of at startup.
        settings = collecto or CollectoSettings.from_env()
        self.collecto_settings = settings
        gateway: Any = (
            CollectoGateway(settings)
            if settings.live
            else DemoGateway(root / "payments" / "_demo")
        )
        self.gateway = gateway
        self.payments = PaymentService(
            store=PaymentStore(root / "payments"),
            gateway=gateway,
            subscription=Subscription(root / "subscription.json"),
            on_paid=self._on_paid,
        )
        self.payments.start_worker()
        self.sms = build_sender(settings)

        self.router = Router()
        self._register()

    def _on_paid(self, payment: Any) -> None:
        """A payment cleared. Switch that account onto the plan it bought.

        Called from the payment sweeper's thread, which may be running long
        after the browser closed. That is the point: the plan comes on because
        the money arrived, not because anybody was watching.
        """
        if not payment.owner:
            return
        plan = PLANS.get(payment.plan)
        if plan is None:
            return
        until = (
            datetime.now(timezone.utc) + timedelta(days=30)
        ).isoformat(timespec="seconds")
        self.accounts.activate_plan(
            payment.owner, plan=plan.id, builds=plan.builds, until=until
        )

    # ── routes ───────────────────────────────────────────────────────────

    def _register(self) -> None:
        add = self.router.add
        add("GET", "/api/health", self.health)

        add("POST", "/api/auth/signup", self.signup)
        add("POST", "/api/auth/verify", self.verify)
        add("POST", "/api/auth/resend", self.resend)
        add("POST", "/api/auth/login", self.login)
        add("GET", "/api/auth/session", self.session)
        add("POST", "/api/auth/logout", self.logout)

        add("GET", "/api/admin/users", self.admin_users)
        add("POST", "/api/admin/users/<user_id>/grant", self.admin_grant)
        add("GET", "/api/admin/sms", self.admin_sms)

        add("GET", "/api/apps", self.list_apps)
        add("POST", "/api/apps", self.create_app)
        add("GET", "/api/apps/<app_id>", self.get_app)
        add("PUT", "/api/apps/<app_id>", self.update_app)
        add("DELETE", "/api/apps/<app_id>", self.delete_app)
        add("POST", "/api/apps/<app_id>/duplicate", self.duplicate_app)
        add("GET", "/api/apps/<app_id>/files/<slot>", self.get_file)
        add("PUT", "/api/apps/<app_id>/files/<slot>", self.upload_file)
        add("DELETE", "/api/apps/<app_id>/files/<slot>", self.remove_file)
        add("POST", "/api/apps/<app_id>/generate", self.generate_only)
        add("POST", "/api/apps/<app_id>/build", self.start_build)
        add("GET", "/api/apps/<app_id>/builds", self.list_builds)
        add("GET", "/api/apps/<app_id>/builds/<number>", self.get_build)
        add("GET", "/api/apps/<app_id>/builds/<number>/events", self.build_events)
        add("GET", "/api/apps/<app_id>/builds/<number>/log", self.build_log)
        add(
            "GET",
            "/api/apps/<app_id>/builds/<number>/artifacts/<name>",
            self.download_artifact,
        )
        add("GET", "/d/<token>", self.download_by_token)
        add("GET", "/api/billing", self.billing)
        add("POST", "/api/billing/pay", self.start_payment)
        add("GET", "/api/billing/payments/<reference>", self.get_payment)
        add("POST", "/api/billing/payments/<reference>/check", self.check_payment)
        add("POST", "/api/billing/demo/<reference>", self.demo_handset)

    def health(self, request: Request) -> dict[str, Any]:
        refresh = "refresh" in request.query
        return toolchain.probe(refresh=refresh).to_json()

    # ── accounts ─────────────────────────────────────────────────────────

    @property
    def demo_sms(self) -> bool:
        return isinstance(self.sms, DemoSms)

    def signup(self, request: Request) -> dict[str, Any]:
        """Create the account, then send a code. The account is not usable yet.

        Written in that order on purpose. The phone number's UNIQUE constraint
        is what stops two accounts on one number, and it can only do that once
        the row exists, so the row goes in first and `verified` stays false
        until a code comes back.
        """
        data = request.json()
        phone = accounts_mod_phone(data.get("phone"))

        existing = self.accounts.by_phone(phone)
        if existing is not None and existing.verified:
            raise CissyError(
                "There is already an account on that number. Try logging in.",
            )
        if existing is not None:
            # Started before and never finished. Let them carry on rather than
            # stranding the number forever. Safe to reset the password here
            # because an unverified account cannot be logged into at all, so
            # there is nothing yet to take over.
            self.accounts.set_password(existing.id, str(data.get("password") or ""))
            user = existing
        else:
            user = self.accounts.create_user(
                name=str(data.get("name") or ""),
                phone=phone,
                password=str(data.get("password") or ""),
            )

        try:
            sent = self._send_code(phone, request.ip, purpose="signup")
        except accounts_mod.RateLimited as error:
            # Pressing the button twice is the commonest reason to land here,
            # and "429 wait 59 seconds" reads like a failure when in fact the
            # code is already on its way. Send them to the code screen.
            wait = self.accounts.seconds_until_resend(phone)
            return {
                "phone": phone,
                "user_id": user.id,
                "sent": True,
                "demo": self.demo_sms,
                "note": error.message,
                "resend_in": wait,
            }
        return {"phone": phone, "user_id": user.id, **sent}

    def resend(self, request: Request) -> dict[str, Any]:
        data = request.json()
        phone = accounts_mod_phone(data.get("phone"))
        if self.accounts.by_phone(phone) is None:
            # Deliberately the same shape as success. Otherwise this endpoint
            # answers "does this number have an account?" for anyone who asks.
            return {"phone": phone, "sent": True, "demo": self.demo_sms}
        return {"phone": phone, **self._send_code(phone, request.ip, purpose="signup")}

    def _send_code(self, phone: str, ip: str, *, purpose: str) -> dict[str, Any]:
        code = self.accounts.issue_code(phone, purpose=purpose, ip=ip)
        message = verification_message(code, accounts_mod.CODE_MINUTES)
        sent = self.sms.send(phone=phone, message=message, reference=f"verify-{phone}")
        payload: dict[str, Any] = {"sent": sent.ok, "demo": self.demo_sms}
        if self.demo_sms:
            # Only ever in demo mode, and the flag is what the browser keys off
            # to show it. A live server must never hand the code back over the
            # same channel that is being verified.
            payload["code"] = code
            payload["message"] = message
        elif not sent.ok:
            payload["error"] = sent.message or "The code could not be sent."
        return payload

    def verify(self, request: Request) -> Cookie:
        data = request.json()
        phone = accounts_mod_phone(data.get("phone"))
        user = self.accounts.by_phone(phone)
        if user is None:
            raise ValidationError("Sign up first.")
        if not self.accounts.check_code(phone, str(data.get("code") or ""), purpose="signup"):
            raise ValidationError("That code is not right. Check it and try again.")

        self.accounts.mark_verified(user.id)
        user = self.accounts.by_id(user.id) or user
        token, _ = self.accounts.open_session(user.id)
        return Cookie({"user": user.to_json()}, token=token)

    def login(self, request: Request) -> Cookie:
        data = request.json()
        phone = accounts_mod_phone(data.get("phone"))
        user = self.accounts.verify_password(phone, str(data.get("password") or ""))
        if user is None:
            # One message for both "no such number" and "wrong password", so
            # this endpoint cannot be used to enumerate who has an account.
            raise AuthError("That number and password do not match.")
        if not user.verified:
            self._send_code(phone, request.ip, purpose="signup")
            raise ForbiddenError(
                "That number was never confirmed. We have sent a new code."
            )
        token, _ = self.accounts.open_session(user.id)
        return Cookie({"user": user.to_json()}, token=token)

    def session(self, request: Request) -> dict[str, Any]:
        """Who am I. Public, and answers null rather than 401 when nobody.

        The browser calls this before it knows whether to draw the app or the
        sign-in screen, and a 401 there would put a password dialog in front of
        somebody who has not asked for one yet.
        """
        return {
            "user": request.user.to_json() if request.user else None,
            "demo_sms": self.demo_sms,
            "payments_mode": getattr(self.gateway, "mode", "demo"),
        }

    def logout(self, request: Request) -> Cookie:
        token = _cookie(request.headers, SESSION_COOKIE)
        if token:
            self.accounts.close_session(token)
        return Cookie({"ok": True}, clear=True)

    # ── admin ────────────────────────────────────────────────────────────

    def _admin(self, request: Request) -> User:
        user = request.owner
        if not user.is_admin:
            raise NotFoundError("No API endpoint at that address.")
        return user

    def admin_users(self, request: Request) -> dict[str, Any]:
        """The only place one person's details are shown to another.

        Reached on its own URLs rather than by a flag on a customer screen, so
        the answer to "can a customer get here?" is "there is no route", not
        "there is a check".
        """
        self._admin(request)
        rows = []
        for user in self.accounts.list_users():
            store = self.workspaces.for_user(user.id)
            rows.append({
                **user.to_json(),
                "apps": [app.name for app in store.list()],
                "disk": self.workspaces.disk_used(user.id),
            })
        running = self.builds.current
        return {
            "users": rows,
            "sms_today": self.accounts.sms_sent_today(),
            "building": running.to_json() if running else None,
        }

    def admin_grant(self, request: Request) -> dict[str, Any]:
        self._admin(request)
        extra = int(request.json().get("builds") or 0)
        if not 0 < extra <= 100:
            raise ValidationError("Grant between 1 and 100 builds.")
        user = self.accounts.grant_builds(request.params["user_id"], extra)
        return {"user": user.to_json()}

    def admin_sms(self, request: Request) -> dict[str, Any]:
        self._admin(request)
        if not isinstance(self.sms, DemoSms):
            raise NotFoundError("There is no message log on a live SMS channel.")
        return {"messages": self.sms.recent()}

    # ── apps ─────────────────────────────────────────────────────────────

    def list_apps(self, request: Request) -> dict[str, Any]:
        return {"apps": [app.to_json() for app in request.workspace.list()]}

    def create_app(self, request: Request) -> dict[str, Any]:
        data = request.json()
        name = str(data.get("name") or "").strip()
        if not name:
            raise ValidationError("The new app needs a name.")

        package = str(data.get("android_package_id") or "").strip().lower()
        config = AppConfig(
            id=slugify(name),
            name=name,
            app_name=str(data.get("app_name") or name).strip(),
            website_url=str(data.get("website_url") or "").strip(),
            android_package_id=package,
            ios_bundle_id=str(data.get("ios_bundle_id") or package).strip(),
            allowed_domains=tuple(_domains(data.get("website_url"))),
        )
        store = request.workspace
        return {"app": self._app_json(store.create(config), store)}

    def get_app(self, request: Request) -> dict[str, Any]:
        store = request.workspace
        return {"app": self._app_json(store.get(request.params["app_id"]), store)}

    def update_app(self, request: Request) -> dict[str, Any]:
        app_id = request.params["app_id"]
        store = request.workspace
        current = store.get(app_id)
        data = request.json()

        # The id, timestamps and build history are the server's to set. Taking
        # them from the request would let a stale browser tab rename an app or
        # move it on top of another one.
        data.pop("id", None)
        data.pop("created_at", None)
        data.pop("updated_at", None)
        # The uploaded files change only through their own endpoints. The
        # editor's draft is a snapshot from page load, so accepting these here
        # let a save made after an upload quietly unhook the icon - and the
        # next build shipped Flutter's default one.
        data.pop("icon_file", None)
        data.pop("splash_file", None)
        data.pop("keystore_file", None)

        merged = {**current.to_json(), **data, "id": app_id}
        return {"app": self._app_json(store.save(AppConfig.from_json(merged)), store)}

    def delete_app(self, request: Request) -> dict[str, Any]:
        request.workspace.delete(request.params["app_id"])
        return {"deleted": True}

    def duplicate_app(self, request: Request) -> dict[str, Any]:
        data = request.json()
        store = request.workspace
        source = store.get(request.params["app_id"])
        name = str(data.get("name") or f"{source.name} copy").strip()
        return {"app": self._app_json(store.duplicate(source.id, name), store)}

    def _app_json(self, config: AppConfig, store: ProjectStore) -> dict[str, Any]:
        """The config as the browser sees it, plus the next build's version code.

        Resolved here rather than in the browser so the rule for choosing one
        lives in a single place. The build screen and the actual build would
        otherwise be free to disagree about what is about to be built.
        """
        payload = config.to_json()
        payload["next_version_code"] = config.next_version_code(
            store.used_version_codes(config.id)
        )
        return payload

    # ── files ────────────────────────────────────────────────────────────

    # Uploads arrive as raw PUT bodies. Multipart parsing is the one thing that
    # would have justified a dependency, and this avoids it for one line of
    # browser code.
    _SLOTS = {
        "icon": ({".png"}, "icon_file"),
        "splash": ({".png", ".jpg", ".jpeg"}, "splash_file"),
        "keystore": ({".jks", ".keystore", ".p12", ".pfx"}, "keystore_file"),
    }

    def upload_file(self, request: Request) -> dict[str, Any]:
        app_id = request.params["app_id"]
        slot = request.params["slot"]
        if slot not in self._SLOTS:
            raise NotFoundError(f'"{slot}" is not something you can upload.')
        allowed, field = self._SLOTS[slot]

        store = request.workspace
        config = store.get(app_id)
        supplied = (request.headers.get("X-Filename") or "").strip()
        suffix = Path(supplied).suffix.lower()
        if suffix not in allowed:
            raise ValidationError(
                f"A {slot} must be one of: {', '.join(sorted(allowed))}."
            )
        if not request.body:
            raise ValidationError("The uploaded file was empty.")

        # The stored name is derived, never taken from the client - a supplied
        # name reaches the filesystem, and "../../config.json" is a valid one.
        name = f"{slot}{suffix}"
        assets = store.assets_dir(app_id)
        assets.mkdir(parents=True, exist_ok=True)
        for stale in assets.glob(f"{slot}.*"):
            stale.unlink()
        (assets / name).write_bytes(request.body)

        saved = store.save(config.updated(**{field: name}))
        return {"app": self._app_json(saved, store)}

    def get_file(self, request: Request) -> FileResponse:
        """The uploaded icon or splash, served inline for the preview.

        Image slots only: a keystore is a secret, and nothing in the UI needs
        to read one back.
        """
        app_id = request.params["app_id"]
        slot = request.params["slot"]
        if slot not in ("icon", "splash"):
            raise NotFoundError(f'"{slot}" has no preview.')
        _, field = self._SLOTS[slot]

        store = request.workspace
        config = store.get(app_id)
        name = getattr(config, field)
        if not name:
            raise NotFoundError(f"No {slot} has been uploaded.")
        path = store.assets_dir(app_id) / name
        if not path.is_file():
            raise NotFoundError(f"The {slot} file is missing on the server.")
        kind, _ = mimetypes.guess_type(name)
        return FileResponse(
            path=path,
            filename=name,
            media_type=kind or "application/octet-stream",
            inline=True,
        )

    def remove_file(self, request: Request) -> dict[str, Any]:
        app_id = request.params["app_id"]
        slot = request.params["slot"]
        if slot not in self._SLOTS:
            raise NotFoundError(f'"{slot}" is not something you can upload.')
        _, field = self._SLOTS[slot]

        store = request.workspace
        config = store.get(app_id)
        for stale in store.assets_dir(app_id).glob(f"{slot}.*"):
            stale.unlink()

        changes: dict[str, Any] = {field: None}
        if slot == "keystore":
            # An alias without a keystore would read as "signed" everywhere.
            changes["key_alias"] = None
        return {"app": self._app_json(store.save(config.updated(**changes)), store)}

    # ── builds ───────────────────────────────────────────────────────────

    def generate_only(self, request: Request) -> dict[str, Any]:
        """Produce the project without building it.

        Useful on its own: it is how you get the iOS project onto a Mac without
        waiting for an Android build you do not need.
        """
        store = request.workspace
        config = store.get(request.params["app_id"])
        lines: list[str] = []
        generate.generate(config, store, lines.append)
        # The absolute path is deliberately not returned. It names a directory
        # on the server, and with more than one customer on the box that is a
        # detail nobody outside it needs.
        return {"log": lines}

    def start_build(self, request: Request) -> dict[str, Any]:
        user = request.owner
        store = request.workspace
        config = store.get(request.params["app_id"])
        data = request.json()

        # APK is the default for the same reason it is first in the picker:
        # a phone you can hand someone beats a Play upload you are not ready for.
        output = data.get("output", "apk")
        if output not in ("apk", "aab"):
            raise ValidationError('Output must be "apk" or "aab".')

        credentials = self._credentials(config, data, store)

        # Settle the version code before building so the artifact and the stored
        # version can never disagree. This only moves it when the current one
        # has actually been built under - a first build keeps the code it was
        # created with rather than shipping as 2.
        if data.get("bump_version", True):
            resolved = config.next_version_code(store.used_version_codes(config.id))
            if resolved != config.version_code:
                config = store.save(config.updated(version_code=resolved))

        # Charged before the build starts, and given back if it never got off
        # the ground. Charging afterwards means a crash between the two is a
        # free build; charging without a refund path means a machine failure is
        # billed to the customer.
        user = self.accounts.spend_build(user.id)
        try:
            build = self.builds.start(
                config,
                output=output,
                credentials=credentials,
                store=store,
                owner=user.id,
            )
        except CissyError:
            self.accounts.refund_build(user.id)
            raise
        return {"build": build.to_json(), "user": user.to_json()}

    def _credentials(
        self, config: AppConfig, data: dict[str, Any], store: ProjectStore
    ) -> SigningCredentials | None:
        if not config.keystore_file or not config.key_alias:
            return None
        store_password = str(data.get("store_password") or "")
        key_password = str(data.get("key_password") or "")
        if not store_password or not key_password:
            raise ValidationError(
                "This app has an upload keystore, so both passwords are needed. "
                "They are never stored, so they have to be entered for each build."
            )
        return SigningCredentials(
            keystore_path=store.assets_dir(config.id) / config.keystore_file,
            key_alias=config.key_alias,
            store_password=store_password,
            key_password=key_password,
        )

    def list_builds(self, request: Request) -> dict[str, Any]:
        app_id = request.params["app_id"]
        store = request.workspace
        store.get(app_id)

        # A running build has no directory yet - the record is written when it
        # finishes - so listing directories alone would hide the one build the
        # user most wants to see, and a reloaded page would show nothing
        # happening while Gradle was busy.
        numbers = set(store.build_numbers(app_id))
        running = self.builds.current
        # Both halves matter: the same app id under a different account is a
        # different app, and must not show up as this one's running build.
        if (
            running is not None
            and running.app_id == app_id
            and running.owner == request.owner.id
        ):
            numbers.add(running.number)

        builds = [self._build_json(request, n) for n in sorted(numbers, reverse=True)]
        return {"builds": [b for b in builds if b]}

    def build_log(self, request: Request) -> FileResponse | dict[str, Any]:
        """The log of a build that is no longer streaming.

        Reattaching to a build that finished while the browser was away needs
        the log from somewhere, and the event stream only carries live builds.
        """
        app_id = request.params["app_id"]
        number = _number(request.params["number"])
        live = self.builds.get(request.owner.id, app_id, number)
        if live is not None:
            return {"lines": list(live.lines)}

        path = request.workspace.build_dir(app_id, number) / "log.txt"
        if not path.is_file():
            raise NotFoundError(f"No log was kept for build #{number}.")
        return {"lines": path.read_text(encoding="utf-8").splitlines()}

    def get_build(self, request: Request) -> dict[str, Any]:
        app_id = request.params["app_id"]
        number = _number(request.params["number"])
        found = self._build_json(request, number)
        if not found:
            raise NotFoundError(f"No build #{number} for {app_id}.")
        return {"build": found}

    def build_events(self, request: Request) -> EventStream:
        app_id = request.params["app_id"]
        number = _number(request.params["number"])
        build = self.builds.get(request.owner.id, app_id, number)
        if build is None:
            raise NotFoundError(
                f"Build #{number} is not running. Its log is on the build itself."
            )
        return EventStream(build)

    def download_artifact(self, request: Request) -> FileResponse:
        app_id = request.params["app_id"]
        number = _number(request.params["number"])
        name = request.params["name"]

        directory = request.workspace.build_dir(app_id, number)
        path = (directory / name).resolve()
        try:
            path.relative_to(directory.resolve())
        except ValueError:
            raise ValidationError("That is not a valid artifact name.") from None
        if not path.is_file():
            raise NotFoundError(f"{name} is not available. It may have been cleaned up.")
        return FileResponse(path=path, filename=name)

    def download_by_token(self, request: Request) -> FileResponse:
        """An artifact by its opaque token - the URL the browser shows.

        The readable path route above still works; this one exists so hovering
        a download link reveals nothing about apps, builds or the API. Same
        rule as everywhere: the search never leaves the session's own
        workspace, so somebody else's token finds nothing here.
        """
        token = request.params["token"]
        store = request.workspace
        for config in store.list():
            for number in store.build_numbers(config.id):
                directory = store.build_dir(config.id, number)
                try:
                    record = json.loads(
                        (directory / "build.json").read_text(encoding="utf-8")
                    )
                except (OSError, json.JSONDecodeError):
                    continue
                for artifact in record.get("artifacts", []):
                    if not token or artifact.get("token") != token:
                        continue
                    # The name comes from our own record, but it still names a
                    # file on disk - held to the same rule as the route above.
                    path = (directory / str(artifact.get("name"))).resolve()
                    try:
                        path.relative_to(directory.resolve())
                    except ValueError:
                        continue
                    if path.is_file():
                        return FileResponse(path=path, filename=path.name)
        raise NotFoundError("That download link does not point at anything of yours.")

    def _build_json(self, request: Request, number: int) -> dict[str, Any] | None:
        """Prefer the live build, fall back to what was written to disk.

        In-memory state is lost on restart, but a finished build's record is
        worth keeping - the history is most of the value of the overview screen.
        """
        app_id = request.params["app_id"]
        live = self.builds.get(request.owner.id, app_id, number)
        if live is not None:
            return live.to_json()
        record = request.workspace.build_dir(app_id, number) / "build.json"
        if not record.is_file():
            return None
        try:
            return json.loads(record.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    # ── billing ──────────────────────────────────────────────────────────

    def billing(self, request: Request) -> dict[str, Any]:
        user = request.owner
        return {
            "mode": getattr(self.gateway, "mode", "demo"),
            "user": user.to_json(),
            "plans": [
                {
                    "id": plan.id,
                    "name": plan.name,
                    "amount": plan.amount,
                    "builds": plan.builds,
                    "blurb": plan.blurb,
                }
                for plan in PLANS.values()
            ],
            "payments": [
                self._payment_json(p, user)
                for p in self.payments.store.for_owner(user.id)
            ],
        }

    def start_payment(self, request: Request) -> dict[str, Any]:
        user = request.owner
        # Without a payment gateway the simulator answers instead, which for a
        # customer would mean plans switched on for free - and a banner about
        # environment variables is not their problem. Admins keep the
        # simulator; everyone else gets a sentence they can act on.
        if isinstance(self.gateway, DemoGateway) and not user.is_admin:
            raise ConflictError(
                "Mobile money payments are not switched on for this server "
                "yet. Nothing was charged - contact support to activate a "
                "plan."
            )
        data = request.json()
        payment = self.payments.start(
            plan_id=str(data.get("plan") or ""),
            # Defaults to the number the account was verified on, which is the
            # one we know reaches this person.
            phone=str(data.get("phone") or user.phone),
            owner=user.id,
        )
        # A demo scenario is chosen up front so the awkward paths - a decline,
        # a prompt nobody answers, a dropped connection - can be shown on
        # purpose instead of waited for.
        scenario = str(data.get("scenario") or "").strip()
        if scenario and isinstance(self.gateway, DemoGateway):
            self.gateway.set_scenario(payment.reference, scenario)
        return {"payment": payment.to_json()}

    def _payment_json(self, payment: Any, user: User) -> dict[str, Any]:
        """A payment as the person looking at it is allowed to see it.

        The trail is written for whoever has to work out why a payment went
        wrong, so it carries the gateway's own words - which name API keys, IP
        mismatches and disabled accounts. That belongs to an operator. The
        counters and the gateway id are ours too: a customer needs to know where
        their money is, not how many times we asked about it.
        """
        data = payment.to_json()
        if user.is_admin:
            return data
        for key in ("trail", "checks", "transaction_id", "mode", "owner"):
            data.pop(key, None)
        return data

    def get_payment(self, request: Request) -> dict[str, Any]:
        payment = self._own_payment(request)
        return {
            "payment": self._payment_json(payment, request.owner),
            "prompt": self._demo_prompt(payment.reference),
            "user": request.owner.to_json(),
        }

    def _own_payment(self, request: Request):
        """Somebody else's reference is a 404, not a 403.

        A reference is guessable enough to be worth probing for, and "that one
        exists but is not yours" is more than a stranger needs to learn.
        """
        payment = self.payments.store.get(request.params["reference"])
        if payment.owner and payment.owner != request.owner.id:
            raise NotFoundError("No payment with that reference.")
        return payment

    def check_payment(self, request: Request) -> dict[str, Any]:
        """Poll now rather than waiting for the sweeper.

        Only ever an impatience shortcut. The sweeper is what makes a payment
        finish when nobody is watching, and this endpoint disappearing would
        change nothing about whether people get what they paid for.
        """
        reference = self._own_payment(request).reference
        payment = self.payments.poll(reference)
        return {
            "payment": self._payment_json(payment, request.owner),
            "prompt": self._demo_prompt(reference),
            "user": request.owner.to_json(),
        }

    def demo_handset(self, request: Request) -> dict[str, Any]:
        """Stand in for the customer tapping their PIN.

        There is no live equivalent and there must never be one - this exists
        only while `DemoGateway` is in play.
        """
        if not isinstance(self.gateway, DemoGateway):
            raise NotFoundError("The demo handset only exists in demo mode.")
        reference = self._own_payment(request).reference
        action = str(request.json().get("action") or "")
        self.gateway.act(reference, action)
        payment = self.payments.poll(reference)
        return {
            "payment": self._payment_json(payment, request.owner),
            "prompt": self._demo_prompt(reference),
            "user": self.accounts.by_id(request.owner.id).to_json(),
        }

    def _demo_prompt(self, reference: str) -> dict[str, Any] | None:
        if not isinstance(self.gateway, DemoGateway):
            return None
        return self.gateway.prompt(reference)

    # ── dispatch ─────────────────────────────────────────────────────────

    def resolve_user(self, headers: Any) -> User | None:
        """Whoever this request belongs to, or nobody.

        The cookie is the real mechanism, because artifact downloads are plain
        links and a browser following one sends no custom headers. The header
        form exists for anything scripted against the API.
        """
        token = headers.get("X-Cissy-Session", "") or _cookie(headers, SESSION_COOKIE)
        return self.accounts.session_user(token) if token else None

    def static_file(self, path: str) -> tuple[bytes, str] | None:
        """Resolve a path under web/, refusing anything that escapes it.

        Two named pages rather than a general fallback: `/` is the public
        landing page and `/app` is the signed-in shell. A blanket
        "unknown path returns index.html" would make every typo look like a
        working page.
        """
        clean = path.rstrip("/") or "/"
        if clean == "/":
            relative = "landing.html"
        elif clean == "/app":
            relative = "index.html"
        else:
            relative = path.lstrip("/")
        candidate = (self.web_dir / relative).resolve()
        try:
            candidate.relative_to(self.web_dir.resolve())
        except ValueError:
            return None
        if not candidate.is_file():
            return None
        kind, _ = mimetypes.guess_type(candidate.name)
        return candidate.read_bytes(), kind or "application/octet-stream"


class RequestHandler(BaseHTTPRequestHandler):
    server_version = "Cissy"
    application: Application

    # Logging every static asset drowns the build output that matters.
    def log_message(self, fmt: str, *args: Any) -> None:
        pass

    def do_GET(self) -> None:
        self._handle("GET")

    def do_POST(self) -> None:
        self._handle("POST")

    def do_PUT(self) -> None:
        self._handle("PUT")

    def do_DELETE(self) -> None:
        self._handle("DELETE")

    def _handle(self, method: str) -> None:
        app = self.application
        parsed = urlparse(self.path)
        path = parsed.path

        try:
            # /d/ is the opaque artifact download - an API route in all but
            # name, kept short because the whole point is what the URL shows.
            if path.startswith("/api/") or path.startswith("/d/"):
                user = app.resolve_user(self.headers)
                if user is None and path not in PUBLIC_ROUTES:
                    self._json(401, {"error": "Sign in to continue."})
                    return
                self._dispatch(app, method, path, parse_qs(parsed.query), user)
                return

            if method != "GET":
                self._json(405, {"error": f"{method} is not allowed here."})
                return

            found = app.static_file(path)
            if found is None:
                self._bytes(404, b"Not found", "text/plain; charset=utf-8")
                return
            payload, kind = found
            self._bytes(200, payload, kind)

        except CissyError as error:
            self._json(error.status, error.to_json())
        except BrokenPipeError:
            # The browser navigated away mid-response. Nothing to report.
            pass
        except Exception as error:  # noqa: BLE001 - last line of defence
            self._json(
                500,
                {
                    "error": "The server hit an unexpected problem.",
                    "detail": f"{type(error).__name__}: {error}",
                },
            )

    def _dispatch(
        self,
        app: Application,
        method: str,
        path: str,
        query: dict[str, list[str]],
        user: User | None = None,
    ) -> None:
        matched = app.router.match(method, path)
        if matched is None:
            raise NotFoundError(f"No API endpoint at {path}.")
        handler, params = matched

        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            raise ValidationError("That upload is too large (limit is 20 MB).")
        body = self.rfile.read(length) if length else b""

        request = Request(
            method=method,
            path=path,
            query=query,
            params=params,
            body=body,
            user=user,
            # The workspace is chosen here from the session, never from the URL.
            # There is no route that names a user, so there is nothing to tamper
            # with: you cannot ask for somebody else's directory.
            store=app.workspaces.for_user(user.id) if user else None,
            ip=self._client_ip(),
            headers=self.headers,
        )
        result = handler(request)

        if isinstance(result, FileResponse):
            self._file(result)
            return
        if isinstance(result, EventStream):
            self._events(result.build)
            return
        if isinstance(result, Cookie):
            self._session(result)
            return
        self._json(200, result if result is not None else {"ok": True})

    def _client_ip(self) -> str:
        """The caller's address, trusting Caddy's header only for the last hop.

        `X-Forwarded-For` can be set by anyone, so only the *last* entry is
        meaningful and only because the reverse proxy appends it. This is used
        for SMS rate limiting, so getting it wrong means either throttling
        everyone as one caller or throttling nobody.
        """
        forwarded = self.headers.get("X-Forwarded-For", "")
        if forwarded:
            return forwarded.split(",")[-1].strip()
        return self.client_address[0] if self.client_address else "unknown"

    def _over_https(self) -> bool:
        return self.headers.get("X-Forwarded-Proto", "").lower() == "https"

    def _session(self, result: Cookie) -> None:
        body = json.dumps(result.payload, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")

        # HttpOnly so no script can read it, which is the real difference
        # between this and the shared password it replaces.
        #
        # Secure only when the request actually arrived over HTTPS, which Caddy
        # tells us. Setting it unconditionally would silently break every login
        # on a plain-HTTP box: the browser accepts the response and discards the
        # cookie, so the symptom is "logging in does nothing" with no error
        # anywhere.
        secure = "; Secure" if self._over_https() else ""
        if result.clear:
            self.send_header(
                "Set-Cookie",
                f"{SESSION_COOKIE}=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax{secure}",
            )
        elif result.token:
            self.send_header(
                "Set-Cookie",
                f"{SESSION_COOKIE}={result.token}; Path=/; "
                f"Max-Age={result.max_age}; HttpOnly; SameSite=Lax{secure}",
            )
        self.end_headers()
        self.wfile.write(body)

    def _file(self, response: FileResponse) -> None:
        size = response.path.stat().st_size
        self.send_response(200)
        self.send_header("Content-Type", response.media_type)
        self.send_header("Content-Length", str(size))
        disposition = "inline" if response.inline else "attachment"
        self.send_header(
            "Content-Disposition", f'{disposition}; filename="{response.filename}"'
        )
        self.end_headers()
        # Streamed in chunks: an AAB can be tens of megabytes and reading it
        # whole would hold all of it in memory on a box that has little to spare.
        with response.path.open("rb") as handle:
            shutil.copyfileobj(handle, self.wfile, length=256 * 1024)

    def _events(self, build: Any) -> None:
        """Stream the build log as server-sent events.

        A queue rather than writing straight from the callback: the build thread
        must never block on a slow or vanished browser.
        """
        queue: "Queue[str | None]" = Queue()

        def send(line: str) -> None:
            queue.put(line)

        backlog = build.subscribe(send)

        self.send_response(200)
        # charset matters: without it the browser sniffs the encoding, and
        # sniffing means holding roughly the first kilobyte back before any of
        # it reaches the page. A build that has only logged a few lines would
        # show an empty console until Gradle got noisy.
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        # Tells nginx not to buffer, if one is ever put in front of this. It is
        # ignored by everything else.
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "close")
        self.end_headers()

        try:
            # Padding for anything that still insists on a full buffer before
            # releasing the first byte. Sent as a comment, which every SSE
            # reader discards.
            self.wfile.write(b": " + b" " * 2048 + b"\n\n")
            self.wfile.flush()
            for line in backlog:
                self._event("line", line)
            while True:
                if build.status != "running" and queue.empty():
                    break
                try:
                    line = queue.get(timeout=15)
                except Empty:
                    # A comment keeps proxies from closing an idle connection
                    # during the long silent stretch of a Gradle build.
                    self.wfile.write(b": keep-alive\n\n")
                    self.wfile.flush()
                    continue
                if line is None:
                    break
                self._event("line", line)
            self._event("done", json.dumps(build.to_json()))
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            build.unsubscribe(send)

    def _event(self, name: str, data: str) -> None:
        payload = "".join(f"data: {part}\n" for part in data.split("\n"))
        self.wfile.write(f"event: {name}\n{payload}\n".encode("utf-8"))
        self.wfile.flush()

    # ── responses ────────────────────────────────────────────────────────

    def _json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._bytes(status, body, "application/json; charset=utf-8")

    def _bytes(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def serve(app: Application, host: str, port: int) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (RequestHandler,), {"application": app})
    return ThreadingHTTPServer((host, port), handler)


def _cookie(headers: Any, name: str) -> str:
    """One cookie's value, or an empty string.

    Parsed by hand rather than with http.cookies, which raises on values this
    has no business rejecting over - the password is chosen by whoever starts
    the server.
    """
    raw = headers.get("Cookie", "") or ""
    for part in raw.split(";"):
        key, sep, value = part.strip().partition("=")
        if sep and key == name:
            return unquote(value)
    return ""


def accounts_mod_phone(value: Any) -> str:
    """The same MSISDN normaliser the payments use.

    One function for both, because the number somebody signs up with has to be
    the number a mobile-money prompt can reach.
    """
    from .payments import normalise_phone

    return normalise_phone(str(value or ""))


def _number(value: str) -> int:
    if not value.isdigit():
        raise ValidationError(f'"{value}" is not a build number.')
    return int(value)


def _domains(url: Any) -> list[str]:
    """Seed the allow-list from the site's own host.

    Almost every app wants exactly this, and an empty allow-list would send
    every link to the external browser - surprising behaviour for a new app.
    """
    if not isinstance(url, str) or not url.strip():
        return []
    host = urlparse(url.strip()).hostname
    return [host.lower()] if host else []
