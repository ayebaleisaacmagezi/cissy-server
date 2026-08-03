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
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from .config import AppConfig, slugify
from .errors import CissyError, NotFoundError, ValidationError
from .store import ProjectStore
from . import toolchain

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
        return {"app": self.store.create(config).to_json()}

    def get_app(self, request: Request) -> dict[str, Any]:
        return {"app": self.store.get(request.params["app_id"]).to_json()}

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
        return {"app": self.store.save(AppConfig.from_json(merged)).to_json()}

    def delete_app(self, request: Request) -> dict[str, Any]:
        self.store.delete(request.params["app_id"])
        return {"deleted": True}

    def duplicate_app(self, request: Request) -> dict[str, Any]:
        data = request.json()
        source = self.store.get(request.params["app_id"])
        name = str(data.get("name") or f"{source.name} copy").strip()
        return {"app": self.store.duplicate(source.id, name).to_json()}

    # ── dispatch ─────────────────────────────────────────────────────────

    def authorised(self, headers: Any) -> bool:
        if not self.password:
            return True
        supplied = headers.get("X-Cissy-Password", "")
        return hmac.compare_digest(supplied, self.password)

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
        self._json(200, result if result is not None else {"ok": True})

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


def _domains(url: Any) -> list[str]:
    """Seed the allow-list from the site's own host.

    Almost every app wants exactly this, and an empty allow-list would send
    every link to the external browser — surprising behaviour for a new app.
    """
    if not isinstance(url, str) or not url.strip():
        return []
    host = urlparse(url.strip()).hostname
    return [host.lower()] if host else []
