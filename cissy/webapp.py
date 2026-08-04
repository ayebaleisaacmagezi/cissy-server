"""HTTP layer: routing, static files, and turning errors into readable JSON.

A hand-rolled router on top of `http.server` rather than a framework. The API
is a dozen endpoints and the only awkward requirement — streaming a build log —
is easier to do directly than through most frameworks anyway.

Uploads arrive as raw PUT bodies rather than multipart forms. That is the one
concession to having no dependencies, and it costs a single line in the browser:
`fetch(url, {method: 'PUT', body: file})`.
"""

from __future__ import annotations

import hmac
import json
import mimetypes
import re
import shutil
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from queue import Empty, Queue
from typing import Any, Callable
from urllib.parse import parse_qs, unquote, urlparse

from .build import BuildRunner
from .config import AppConfig, slugify
from .errors import CissyError, NotFoundError, ValidationError
from .signing import SigningCredentials
from .store import ProjectStore
from . import generate, toolchain

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
class FileResponse:
    """Send a file back as a download."""

    path: Path
    filename: str


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

    def __init__(self, *, root: Path, web_dir: Path, password: str | None) -> None:
        self.store = ProjectStore(root / "projects")
        self.web_dir = web_dir
        self.password = password
        self.builds = BuildRunner(self.store)
        self.router = Router()
        self._register()

    # ── routes ───────────────────────────────────────────────────────────

    def _register(self) -> None:
        add = self.router.add
        add("GET", "/api/health", self.health)
        add("GET", "/api/apps", self.list_apps)
        add("POST", "/api/apps", self.create_app)
        add("GET", "/api/apps/<app_id>", self.get_app)
        add("PUT", "/api/apps/<app_id>", self.update_app)
        add("DELETE", "/api/apps/<app_id>", self.delete_app)
        add("POST", "/api/apps/<app_id>/duplicate", self.duplicate_app)
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

    def health(self, request: Request) -> dict[str, Any]:
        refresh = "refresh" in request.query
        return toolchain.probe(refresh=refresh).to_json()

    def list_apps(self, request: Request) -> dict[str, Any]:
        return {"apps": [app.to_json() for app in self.store.list()]}

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
        return {"app": self._app_json(self.store.create(config))}

    def get_app(self, request: Request) -> dict[str, Any]:
        return {"app": self._app_json(self.store.get(request.params["app_id"]))}

    def update_app(self, request: Request) -> dict[str, Any]:
        app_id = request.params["app_id"]
        current = self.store.get(app_id)
        data = request.json()

        # The id, timestamps and build history are the server's to set. Taking
        # them from the request would let a stale browser tab rename an app or
        # move it on top of another one.
        data.pop("id", None)
        data.pop("created_at", None)
        data.pop("updated_at", None)

        merged = {**current.to_json(), **data, "id": app_id}
        return {"app": self._app_json(self.store.save(AppConfig.from_json(merged)))}

    def delete_app(self, request: Request) -> dict[str, Any]:
        self.store.delete(request.params["app_id"])
        return {"deleted": True}

    def duplicate_app(self, request: Request) -> dict[str, Any]:
        data = request.json()
        source = self.store.get(request.params["app_id"])
        name = str(data.get("name") or f"{source.name} copy").strip()
        return {"app": self._app_json(self.store.duplicate(source.id, name))}

    def _app_json(self, config: AppConfig) -> dict[str, Any]:
        """The config as the browser sees it, plus the next build's version code.

        Resolved here rather than in the browser so the rule for choosing one
        lives in a single place. The build screen and the actual build would
        otherwise be free to disagree about what is about to be built.
        """
        payload = config.to_json()
        payload["next_version_code"] = config.next_version_code(
            self.store.used_version_codes(config.id)
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

        config = self.store.get(app_id)
        supplied = (request.headers.get("X-Filename") or "").strip()
        suffix = Path(supplied).suffix.lower()
        if suffix not in allowed:
            raise ValidationError(
                f"A {slot} must be one of: {', '.join(sorted(allowed))}."
            )
        if not request.body:
            raise ValidationError("The uploaded file was empty.")

        # The stored name is derived, never taken from the client — a supplied
        # name reaches the filesystem, and "../../config.json" is a valid one.
        name = f"{slot}{suffix}"
        assets = self.store.assets_dir(app_id)
        assets.mkdir(parents=True, exist_ok=True)
        for stale in assets.glob(f"{slot}.*"):
            stale.unlink()
        (assets / name).write_bytes(request.body)

        saved = self.store.save(config.updated(**{field: name}))
        return {"app": self._app_json(saved)}

    def remove_file(self, request: Request) -> dict[str, Any]:
        app_id = request.params["app_id"]
        slot = request.params["slot"]
        if slot not in self._SLOTS:
            raise NotFoundError(f'"{slot}" is not something you can upload.')
        _, field = self._SLOTS[slot]

        config = self.store.get(app_id)
        for stale in self.store.assets_dir(app_id).glob(f"{slot}.*"):
            stale.unlink()

        changes: dict[str, Any] = {field: None}
        if slot == "keystore":
            # An alias without a keystore would read as "signed" everywhere.
            changes["key_alias"] = None
        return {"app": self._app_json(self.store.save(config.updated(**changes)))}

    # ── builds ───────────────────────────────────────────────────────────

    def generate_only(self, request: Request) -> dict[str, Any]:
        """Produce the project without building it.

        Useful on its own: it is how you get the iOS project onto a Mac without
        waiting for an Android build you do not need.
        """
        config = self.store.get(request.params["app_id"])
        lines: list[str] = []
        directory = generate.generate(config, self.store, lines.append)
        return {"path": str(directory), "log": lines}

    def start_build(self, request: Request) -> dict[str, Any]:
        config = self.store.get(request.params["app_id"])
        data = request.json()

        output = data.get("output", "aab")
        if output not in ("apk", "aab"):
            raise ValidationError('Output must be "apk" or "aab".')

        credentials = self._credentials(config, data)

        # Settle the version code before building so the artifact and the stored
        # version can never disagree. This only moves it when the current one
        # has actually been built under — a first build keeps the code it was
        # created with rather than shipping as 2.
        if data.get("bump_version", True):
            resolved = config.next_version_code(self.store.used_version_codes(config.id))
            if resolved != config.version_code:
                config = self.store.save(config.updated(version_code=resolved))

        build = self.builds.start(config, output=output, credentials=credentials)
        return {"build": build.to_json()}

    def _credentials(
        self, config: AppConfig, data: dict[str, Any]
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
            keystore_path=self.store.assets_dir(config.id) / config.keystore_file,
            key_alias=config.key_alias,
            store_password=store_password,
            key_password=key_password,
        )

    def list_builds(self, request: Request) -> dict[str, Any]:
        app_id = request.params["app_id"]
        self.store.get(app_id)

        # A running build has no directory yet — the record is written when it
        # finishes — so listing directories alone would hide the one build the
        # user most wants to see, and a reloaded page would show nothing
        # happening while Gradle was busy.
        numbers = set(self.store.build_numbers(app_id))
        running = self.builds.current
        if running is not None and running.app_id == app_id:
            numbers.add(running.number)

        builds = [self._build_json(app_id, n) for n in sorted(numbers, reverse=True)]
        return {"builds": [b for b in builds if b]}

    def build_log(self, request: Request) -> FileResponse | dict[str, Any]:
        """The log of a build that is no longer streaming.

        Reattaching to a build that finished while the browser was away needs
        the log from somewhere, and the event stream only carries live builds.
        """
        app_id = request.params["app_id"]
        number = _number(request.params["number"])
        live = self.builds.get(app_id, number)
        if live is not None:
            return {"lines": list(live.lines)}

        path = self.store.build_dir(app_id, number) / "log.txt"
        if not path.is_file():
            raise NotFoundError(f"No log was kept for build #{number}.")
        return {"lines": path.read_text(encoding="utf-8").splitlines()}

    def get_build(self, request: Request) -> dict[str, Any]:
        app_id = request.params["app_id"]
        number = _number(request.params["number"])
        found = self._build_json(app_id, number)
        if not found:
            raise NotFoundError(f"No build #{number} for {app_id}.")
        return {"build": found}

    def build_events(self, request: Request) -> EventStream:
        app_id = request.params["app_id"]
        number = _number(request.params["number"])
        build = self.builds.get(app_id, number)
        if build is None:
            raise NotFoundError(
                f"Build #{number} is not running. Its log is on the build itself."
            )
        return EventStream(build)

    def download_artifact(self, request: Request) -> FileResponse:
        app_id = request.params["app_id"]
        number = _number(request.params["number"])
        name = request.params["name"]

        directory = self.store.build_dir(app_id, number)
        path = (directory / name).resolve()
        try:
            path.relative_to(directory.resolve())
        except ValueError:
            raise ValidationError("That is not a valid artifact name.") from None
        if not path.is_file():
            raise NotFoundError(f"{name} is not available. It may have been cleaned up.")
        return FileResponse(path=path, filename=name)

    def _build_json(self, app_id: str, number: int) -> dict[str, Any] | None:
        """Prefer the live build, fall back to what was written to disk.

        In-memory state is lost on restart, but a finished build's record is
        worth keeping — the history is most of the value of the overview screen.
        """
        live = self.builds.get(app_id, number)
        if live is not None:
            return live.to_json()
        record = self.store.build_dir(app_id, number) / "build.json"
        if not record.is_file():
            return None
        try:
            return json.loads(record.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    # ── dispatch ─────────────────────────────────────────────────────────

    def authorised(self, headers: Any) -> bool:
        """Accept the password from a header or a cookie.

        The header is what the page's own requests use. The cookie exists for
        downloads: an artifact is an ordinary link, and a browser following one
        sends no custom headers — which surfaced as Chrome refusing every
        download with "try to sign in to the site".
        """
        if not self.password:
            return True
        if hmac.compare_digest(headers.get("X-Cissy-Password", ""), self.password):
            return True
        return hmac.compare_digest(_cookie(headers, "cissy_password"), self.password)

    def static_file(self, path: str) -> tuple[bytes, str] | None:
        """Resolve a path under web/, refusing anything that escapes it."""
        relative = path.lstrip("/") or "index.html"
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
            if path.startswith("/api/"):
                if not app.authorised(self.headers):
                    self._json(401, {"error": "Wrong or missing password."})
                    return
                self._dispatch(app, method, path, parse_qs(parsed.query))
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
        self, app: Application, method: str, path: str, query: dict[str, list[str]]
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
            headers=self.headers,
        )
        result = handler(request)

        if isinstance(result, FileResponse):
            self._file(result)
            return
        if isinstance(result, EventStream):
            self._events(result.build)
            return
        self._json(200, result if result is not None else {"ok": True})

    def _file(self, response: FileResponse) -> None:
        size = response.path.stat().st_size
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(size))
        self.send_header(
            "Content-Disposition", f'attachment; filename="{response.filename}"'
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
    has no business rejecting over — the password is chosen by whoever starts
    the server.
    """
    raw = headers.get("Cookie", "") or ""
    for part in raw.split(";"):
        key, sep, value = part.strip().partition("=")
        if sep and key == name:
            return unquote(value)
    return ""


def _number(value: str) -> int:
    if not value.isdigit():
        raise ValidationError(f'"{value}" is not a build number.')
    return int(value)


def _domains(url: Any) -> list[str]:
    """Seed the allow-list from the site's own host.

    Almost every app wants exactly this, and an empty allow-list would send
    every link to the external browser — surprising behaviour for a new app.
    """
    if not isinstance(url, str) or not url.strip():
        return []
    host = urlparse(url.strip()).hostname
    return [host.lower()] if host else []
