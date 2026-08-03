"""End-to-end tests over a real socket.

Exercising the actual HTTP server rather than calling handlers directly, because
the parts most likely to break — routing, status codes, auth, path traversal —
only exist at that layer.
"""

import json
import shutil
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from cissy.webapp import Application, serve


class ServerTestCase(unittest.TestCase):
    password: str | None = None

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="cissy_http_"))
        self.web = self.root / "web"
        self.web.mkdir()
        (self.web / "index.html").write_text("<h1>Cissy</h1>", encoding="utf-8")

        app = Application(root=self.root, web_dir=self.web, password=self.password)
        self.httpd = serve(app, "127.0.0.1", 0)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)
        shutil.rmtree(self.root, ignore_errors=True)

    def request(self, method, path, payload=None, headers=None):
        body = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(self.base + path, data=body, method=method)
        req.add_header("Content-Type", "application/json")
        if self.password:
            req.add_header("X-Cissy-Password", self.password)
        for key, value in (headers or {}).items():
            req.add_header(key, value)
        try:
            with urllib.request.urlopen(req, timeout=10) as response:
                return response.status, json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as error:
            raw = error.read()
            try:
                return error.code, json.loads(raw or b"{}")
            except json.JSONDecodeError:
                return error.code, {"raw": raw.decode("utf-8", "replace")}


class ApiTest(ServerTestCase):
    def create(self, name="Cissytech Portal", **extra):
        payload = {
            "name": name,
            "website_url": "https://portal.cissytech.com",
            "android_package_id": "com.cissytech.portal",
            **extra,
        }
        return self.request("POST", "/api/apps", payload)

    def test_starts_with_no_apps(self):
        status, body = self.request("GET", "/api/apps")
        self.assertEqual(status, 200)
        self.assertEqual(body["apps"], [])

    def test_creates_and_lists_an_app(self):
        status, body = self.create()
        self.assertEqual(status, 200)
        self.assertEqual(body["app"]["id"], "cissytech-portal")

        _, listed = self.request("GET", "/api/apps")
        self.assertEqual([a["id"] for a in listed["apps"]], ["cissytech-portal"])

    def test_seeds_the_allowed_domain_from_the_url(self):
        # An empty allow-list would push every link to the external browser.
        _, body = self.create()
        self.assertEqual(body["app"]["allowed_domains"], ["portal.cissytech.com"])

    def test_rejects_a_bad_package_id_with_an_explanation(self):
        status, body = self.create(android_package_id="portal")
        self.assertEqual(status, 422)
        self.assertIn("two parts", body["error"])

    def test_missing_app_is_a_404_not_a_crash(self):
        status, body = self.request("GET", "/api/apps/ghost")
        self.assertEqual(status, 404)
        self.assertIn("ghost", body["error"])

    def test_updates_only_the_fields_sent(self):
        self.create()
        status, body = self.request(
            "PUT", "/api/apps/cissytech-portal", {"app_name": "Portal"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["app"]["app_name"], "Portal")
        self.assertEqual(body["app"]["website_url"], "https://portal.cissytech.com")

    def test_an_update_cannot_rename_the_app_id(self):
        # A stale browser tab must not be able to move an app onto another one.
        self.create()
        _, body = self.request(
            "PUT", "/api/apps/cissytech-portal", {"id": "somewhere-else"}
        )
        self.assertEqual(body["app"]["id"], "cissytech-portal")
        self.assertEqual(self.request("GET", "/api/apps/somewhere-else")[0], 404)

    def test_duplicate_creates_a_second_app(self):
        self.create()
        status, body = self.request(
            "POST", "/api/apps/cissytech-portal/duplicate", {"name": "Kampala Shop"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["app"]["id"], "kampala-shop")
        self.assertEqual(len(self.request("GET", "/api/apps")[1]["apps"]), 2)

    def test_delete_removes_it(self):
        self.create()
        self.assertEqual(self.request("DELETE", "/api/apps/cissytech-portal")[0], 200)
        self.assertEqual(self.request("GET", "/api/apps")[1]["apps"], [])

    def test_unknown_endpoint_is_a_404(self):
        status, _ = self.request("GET", "/api/nonsense")
        self.assertEqual(status, 404)

    def test_wrong_method_is_reported_clearly(self):
        status, body = self.request("DELETE", "/api/health")
        self.assertEqual(status, 400)
        self.assertIn("not allowed", body["error"])

    def test_malformed_json_does_not_500(self):
        req = urllib.request.Request(
            self.base + "/api/apps", data=b"{oops", method="POST"
        )
        try:
            urllib.request.urlopen(req, timeout=10)
            self.fail("expected an error")
        except urllib.error.HTTPError as error:
            self.assertEqual(error.code, 422)

    def test_health_reports_the_real_toolchain(self):
        status, body = self.request("GET", "/api/health")
        self.assertEqual(status, 200)
        names = {tool["name"] for tool in body["tools"]}
        self.assertEqual(names, {"Flutter", "Java", "Android SDK"})
        # Whether they are installed depends on the machine; the shape must not.
        self.assertIsInstance(body["ok"], bool)
        self.assertTrue(body["summary"])


class StaticTest(ServerTestCase):
    def test_serves_the_index(self):
        with urllib.request.urlopen(self.base + "/", timeout=10) as response:
            self.assertEqual(response.status, 200)
            self.assertIn(b"Cissy", response.read())

    def test_refuses_to_serve_outside_the_web_directory(self):
        secret = self.root / "secret.txt"
        secret.write_text("password", encoding="utf-8")
        for attempt in ("/../secret.txt", "/..%2fsecret.txt", "/web/../secret.txt"):
            with self.subTest(attempt=attempt):
                try:
                    with urllib.request.urlopen(self.base + attempt, timeout=10) as r:
                        self.assertNotIn(b"password", r.read())
                except urllib.error.HTTPError as error:
                    self.assertIn(error.code, (400, 404))


class AuthTest(ServerTestCase):
    password = "let-me-in"

    def test_rejects_a_missing_password(self):
        req = urllib.request.Request(self.base + "/api/apps")
        try:
            urllib.request.urlopen(req, timeout=10)
            self.fail("expected 401")
        except urllib.error.HTTPError as error:
            self.assertEqual(error.code, 401)

    def test_rejects_a_wrong_password(self):
        req = urllib.request.Request(self.base + "/api/apps")
        req.add_header("X-Cissy-Password", "wrong")
        try:
            urllib.request.urlopen(req, timeout=10)
            self.fail("expected 401")
        except urllib.error.HTTPError as error:
            self.assertEqual(error.code, 401)

    def test_accepts_the_right_password(self):
        status, _ = self.request("GET", "/api/apps")
        self.assertEqual(status, 200)

    def test_static_files_do_not_need_the_password(self):
        # The login screen has to load before anyone can type a password.
        with urllib.request.urlopen(self.base + "/", timeout=10) as response:
            self.assertEqual(response.status, 200)


if __name__ == "__main__":
    unittest.main()
