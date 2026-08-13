"""End-to-end tests over a real socket.

Exercising the actual HTTP server rather than calling handlers directly, because
the parts most likely to break - routing, status codes, auth, path traversal -
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

from cissy.build import Build
from cissy.payments import PLANS
from cissy.webapp import Application, serve


class ServerTestCase(unittest.TestCase):
    """A real server with one signed-in customer.

    Every test used to carry a shared password. Now it carries a session, made
    the same way a browser makes one: sign up, read the code the demo channel
    hands back, verify. That means the auth path is exercised by every test in
    the file rather than only by the ones about auth.
    """

    signed_in = True

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="cissy_http_"))
        self.web = self.root / "web"
        self.web.mkdir()
        (self.web / "index.html").write_text("<h1>Cissy</h1>", encoding="utf-8")
        (self.web / "landing.html").write_text("<h1>Web2app</h1>", encoding="utf-8")

        self.app = Application(root=self.root, web_dir=self.web, password=None)
        self.httpd = serve(self.app, "127.0.0.1", 0)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

        self.session = ""
        if self.signed_in:
            self.session = self.sign_up("Grace Nabwire", "0700111222")

    def sign_up(self, name, phone, password="a-good-password"):
        """Returns the session cookie for a fresh, verified account."""
        _, body = self.request(
            "POST", "/api/auth/signup",
            {"name": name, "phone": phone, "password": password}, session="",
        )
        _, body, cookie = self.raw(
            "POST", "/api/auth/verify",
            {"phone": body["phone"], "code": body["code"]}, session="",
        )
        return cookie.split(";")[0] if cookie else ""

    def raw(self, method, path, payload=None, headers=None, session=None):
        """Like `request`, but also hands back the Set-Cookie header."""
        body = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(self.base + path, data=body, method=method)
        req.add_header("Content-Type", "application/json")
        cookie = self.session if session is None else session
        if cookie:
            req.add_header("Cookie", cookie)
        for key, value in (headers or {}).items():
            req.add_header(key, value)
        try:
            with urllib.request.urlopen(req, timeout=10) as response:
                return (
                    response.status,
                    json.loads(response.read() or b"{}"),
                    response.headers.get("Set-Cookie", ""),
                )
        except urllib.error.HTTPError as error:
            raw = error.read()
            try:
                return error.code, json.loads(raw or b"{}"), ""
            except json.JSONDecodeError:
                return error.code, {"raw": raw.decode("utf-8", "replace")}, ""

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)
        self.app.accounts.close()
        shutil.rmtree(self.root, ignore_errors=True)

    def request(self, method, path, payload=None, headers=None, session=None):
        status, body, _ = self.raw(method, path, payload, headers, session)
        return status, body


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
        req.add_header("Cookie", self.session)
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


class RunningBuildTest(ServerTestCase):
    """A build outlives the page that started it.

    It runs in a thread on the server, so reloading or closing the tab leaves
    it running. The API has to say so, or a reloaded page shows an idle app
    while Gradle is busy and the only clue is Build being refused.
    """

    def setUp(self):
        super().setUp()
        self.request("POST", "/api/apps", {
            "name": "Portal",
            "website_url": "https://portal.cissytech.com",
            "android_package_id": "com.cissytech.portal",
        })
        _, me = self.request("GET", "/api/auth/session")
        self.owner = me["user"]["id"]

    def fake_running(self, number: int = 1, owner: str | None = None) -> None:
        owner = self.owner if owner is None else owner
        self.app.builds._current = Build(
            number=number,
            app_id="portal",
            owner=owner,
            output="apk",
            version_name="1.0.0",
            version_code=1,
            signed=False,
        )
        self.app.builds._history[(owner, "portal", number)] = self.app.builds._current

    def test_a_running_build_appears_in_the_history(self):
        # It has no directory on disk yet - the record is only written when it
        # finishes - so listing directories alone would hide it.
        self.fake_running()
        status, payload = self.request("GET", "/api/apps/portal/builds")
        self.assertEqual(status, 200)
        self.assertEqual(
            [(b["number"], b["status"]) for b in payload["builds"]], [(1, "running")]
        )

    def test_it_is_not_listed_under_another_app(self):
        self.fake_running()
        self.request("POST", "/api/apps", {
            "name": "Other",
            "website_url": "https://other.cissytech.com",
            "android_package_id": "com.cissytech.other",
        })
        _, payload = self.request("GET", "/api/apps/other/builds")
        self.assertEqual(payload["builds"], [])

    def test_the_log_of_a_running_build_is_readable(self):
        # This is what a reloaded page needs before the stream takes over.
        self.fake_running()
        self.app.builds._current.log("Running Gradle task 'assembleRelease'...")
        status, payload = self.request("GET", "/api/apps/portal/builds/1/log")
        self.assertEqual(status, 200)
        self.assertIn("assembleRelease", payload["lines"][0])

    def test_a_finished_build_serves_its_log_from_disk(self):
        directory = self.app.workspaces.for_user(self.owner).build_dir("portal", 7)
        directory.mkdir(parents=True)
        (directory / "log.txt").write_text("line one\nline two\n", encoding="utf-8")
        _, payload = self.request("GET", "/api/apps/portal/builds/7/log")
        self.assertEqual(payload["lines"], ["line one", "line two"])

    def test_a_build_with_no_log_says_so(self):
        status, payload = self.request("GET", "/api/apps/portal/builds/9/log")
        self.assertEqual(status, 404)
        self.assertIn("No log", payload["error"])


class StaticTest(ServerTestCase):
    def test_the_root_is_the_public_landing_page(self):
        with urllib.request.urlopen(self.base + "/", timeout=10) as response:
            self.assertIn(b"Web2app", response.read())

    def test_serves_the_app_shell(self):
        with urllib.request.urlopen(self.base + "/app", timeout=10) as response:
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
    """The session is the only way in now."""

    signed_in = False

    def test_the_api_is_shut_without_a_session(self):
        for path in ("/api/apps", "/api/billing"):
            status, _ = self.request("GET", path)
            self.assertEqual(status, 401, path)

    def test_asking_who_i_am_is_public_and_answers_nobody(self):
        # Deliberately not a 401. The page calls this before it knows whether
        # to draw the app or the sign-in screen, and an error there would put a
        # failure in front of somebody who has not signed in yet.
        status, body = self.request("GET", "/api/auth/session")
        self.assertEqual(status, 200)
        self.assertIsNone(body["user"])

    def test_a_made_up_session_is_not_a_session(self):
        status, _ = self.request(
            "GET", "/api/apps", session="cissy_session=not-a-real-token"
        )
        self.assertEqual(status, 401)

    def test_signing_up_and_verifying_gets_you_in(self):
        session = self.sign_up("Grace Nabwire", "0700111222")
        status, body = self.request("GET", "/api/apps", session=session)
        self.assertEqual(status, 200)
        self.assertEqual(body["apps"], [])

    def test_the_same_number_cannot_be_claimed_twice(self):
        self.sign_up("Grace Nabwire", "0700111222")
        status, body = self.request(
            "POST", "/api/auth/signup",
            {"name": "Impostor", "phone": "0700111222", "password": "let-me-in-now"},
            session="",
        )
        self.assertGreaterEqual(status, 400)
        self.assertIn("already an account", body["error"])

    def test_a_wrong_password_says_nothing_useful(self):
        self.sign_up("Grace Nabwire", "0700111222")
        _, missing = self.request(
            "POST", "/api/auth/login",
            {"phone": "0755000000", "password": "whatever-this-is"}, session="",
        )
        _, wrong = self.request(
            "POST", "/api/auth/login",
            {"phone": "0700111222", "password": "not-the-password"}, session="",
        )
        # Identical, so this endpoint cannot be used to find out which numbers
        # have accounts.
        self.assertEqual(missing["error"], wrong["error"])

    def test_logging_out_kills_the_session(self):
        session = self.sign_up("Grace Nabwire", "0700111222")
        self.request("POST", "/api/auth/logout", session=session)
        status, _ = self.request("GET", "/api/apps", session=session)
        self.assertEqual(status, 401)

    def test_the_landing_page_needs_no_session(self):
        # A stranger has to be able to read what the product is.
        with urllib.request.urlopen(self.base + "/", timeout=10) as response:
            self.assertEqual(response.status, 200)


class BillingTest(ServerTestCase):
    def setUp(self):
        super().setUp()
        self.make_admin()

    def make_admin(self):
        # The payment simulator is an admin's testing tool - a customer who
        # tries to pay while there is no gateway is refused instead. These
        # tests exercise the simulator, so they run as the admin.
        _, body = self.request("GET", "/api/auth/session")
        self.app.accounts.set_admin(body["user"]["id"])

    def pay(self, plan="starter", phone="0772000000", **extra):
        status, body = self.request(
            "POST", "/api/billing/pay", {"plan": plan, "phone": phone, **extra}
        )
        return status, body

    def test_billing_reports_demo_mode_with_no_key_configured(self):
        # The default has to be the simulator. A half-configured live client
        # would fail mid-payment instead of at startup.
        status, body = self.request("GET", "/api/billing")
        self.assertEqual(status, 200)
        self.assertEqual(body["mode"], "demo")
        self.assertEqual(body["user"]["plan"], "trial")

    def test_a_payment_starts_pending_and_is_readable_by_reference(self):
        status, body = self.pay()
        self.assertEqual(status, 200)
        reference = body["payment"]["reference"]
        self.assertEqual(body["payment"]["status"], "pending")

        status, found = self.request("GET", f"/api/billing/payments/{reference}")
        self.assertEqual(status, 200)
        self.assertEqual(found["payment"]["reference"], reference)

    def test_the_handset_approving_activates_the_plan(self):
        _, body = self.pay()
        reference = body["payment"]["reference"]
        status, after = self.request(
            "POST", f"/api/billing/demo/{reference}", {"action": "approve"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(after["payment"]["status"], "successful")
        self.assertEqual(after["user"]["plan"], "starter")

    def test_the_handset_declining_leaves_no_subscription(self):
        _, body = self.pay(plan="pro")
        reference = body["payment"]["reference"]
        _, after = self.request(
            "POST", f"/api/billing/demo/{reference}", {"action": "decline"}
        )
        self.assertEqual(after["payment"]["status"], "failed")
        self.assertEqual(after["user"]["plan"], "trial")

    def test_the_price_comes_from_the_plan_not_the_request(self):
        # Nothing the browser sends may decide what a subscription costs.
        _, body = self.pay(amount=1)
        self.assertEqual(body["payment"]["amount"], PLANS["starter"].amount)

    def test_an_unknown_plan_is_refused(self):
        status, body = self.pay(plan="platinum")
        self.assertEqual(status, 422)
        self.assertIn("platinum", body["error"])

    def test_an_unusable_phone_number_is_refused(self):
        status, _ = self.pay(phone="ring me")
        self.assertEqual(status, 422)

    def test_a_reference_cannot_walk_out_of_the_payments_directory(self):
        status, _ = self.request(
            "GET", "/api/billing/payments/..%2F..%2Fconfig", None
        )
        self.assertIn(status, (404, 422))

    def test_billing_needs_the_password_like_everything_else(self):
        # Only meaningful on the passworded case, which the subclass covers.
        status, _ = self.request("GET", "/api/billing")
        self.assertEqual(status, 200)

    def test_a_customer_cannot_pay_through_the_simulator(self):
        # With no gateway configured the simulator would hand out plans for
        # free, so a customer is told payments are not on - in words that do
        # not mention environment variables.
        customer = self.sign_up("Amina Kirabo", "0700555666")
        status, body = self.request(
            "POST", "/api/billing/pay",
            {"plan": "starter", "phone": "0772000000"}, session=customer,
        )
        self.assertEqual(status, 409)
        self.assertNotIn("COLLECTO", body["error"])
        self.assertIn("not switched on", body["error"])

    def test_the_trail_never_reaches_a_customer(self):
        # The trail carries the gateway's own words, and those name API keys,
        # IP mismatches and disabled accounts. An admin needs them to work out
        # why a payment failed. A customer is owed none of it, and the page
        # they see would otherwise print it under "What the server has done".
        _, body = self.pay()
        reference = body["payment"]["reference"]

        # The payment belongs to the admin, so read it as the admin first.
        _, mine = self.request("GET", f"/api/billing/payments/{reference}")
        self.assertIn("trail", mine["payment"])

        # Now the same account demoted to an ordinary customer.
        _, session = self.request("GET", "/api/auth/session")
        self.app.accounts.set_admin(session["user"]["id"], False)
        _, theirs = self.request("GET", f"/api/billing/payments/{reference}")
        for leaked in ("trail", "checks", "transaction_id", "mode"):
            self.assertNotIn(leaked, theirs["payment"])
        # What they do need is still there.
        self.assertEqual(theirs["payment"]["status"], "pending")
        self.assertEqual(theirs["payment"]["amount"], PLANS["starter"].amount)


class ExpiredPlanTest(ServerTestCase):
    """A month that has ended is a month that has ended."""

    def test_a_lapsed_plan_cannot_start_a_build(self):
        # The allowance still has builds in it. They belong to a month that is
        # over, and spending them is a subscription that quietly outlives the
        # payment for it.
        _, body = self.request("GET", "/api/auth/session")
        user_id = body["user"]["id"]
        self.app.accounts.activate_plan(
            user_id, plan="starter", builds=25,
            until="2020-01-01T00:00:00+00:00",
        )

        user = self.app.accounts.by_id(user_id)
        self.assertTrue(user.plan_expired)
        self.assertEqual(user.builds_left, 25)

        with self.assertRaises(Exception) as caught:
            self.app.accounts.spend_build(user_id)
        self.assertIn("plan has ended", str(caught.exception))
        # Refused, not silently charged for.
        self.assertEqual(self.app.accounts.by_id(user_id).builds_used, 0)

    def test_a_live_plan_still_builds(self):
        _, body = self.request("GET", "/api/auth/session")
        user_id = body["user"]["id"]
        self.app.accounts.activate_plan(
            user_id, plan="starter", builds=25,
            until="2099-01-01T00:00:00+00:00",
        )
        self.assertEqual(self.app.accounts.spend_build(user_id).builds_used, 1)


class IsolationTest(ServerTestCase):
    """One customer, one workspace, and no way to name somebody else's."""

    def test_another_account_cannot_open_or_see_your_app(self):
        self.request("POST", "/api/apps", {
            "name": "Kampala Shop", "website_url": "https://shop.co.ug",
            "android_package_id": "ug.co.shop",
        })
        other = self.sign_up("David Okello", "0755999888")

        status, body = self.request("GET", "/api/apps", session=other)
        self.assertEqual(body["apps"], [])
        status, _ = self.request("GET", "/api/apps/kampala-shop", session=other)
        # 404 rather than 403: "that exists but is not yours" is more than a
        # stranger needs to learn.
        self.assertEqual(status, 404)

    def test_two_accounts_can_use_the_same_app_name(self):
        _, mine = self.request("POST", "/api/apps", {
            "name": "Portal", "website_url": "https://a.example",
            "android_package_id": "com.a.portal",
        })
        other = self.sign_up("David Okello", "0755999888")
        _, theirs = self.request("POST", "/api/apps", {
            "name": "Portal", "website_url": "https://b.example",
            "android_package_id": "com.b.portal",
        }, session=other)
        # Under the old flat layout the second became "portal-2".
        self.assertEqual(mine["app"]["id"], "portal")
        self.assertEqual(theirs["app"]["id"], "portal")

    def test_the_admin_screens_have_no_route_for_a_customer(self):
        status, _ = self.request("GET", "/api/admin/users")
        self.assertEqual(status, 404)


class BillingLockedTest(BillingTest):
    """Same billing behaviour, run again for a second account."""

    def setUp(self):
        super().setUp()
        self.session = self.sign_up("David Okello", "0755999888")
        self.make_admin()

    def test_billing_rejects_a_missing_password(self):
        req = urllib.request.Request(self.base + "/api/billing")
        try:
            urllib.request.urlopen(req, timeout=10)
            self.fail("expected 401")
        except urllib.error.HTTPError as error:
            self.assertEqual(error.code, 401)

    def test_the_demo_handset_is_not_a_way_past_the_password(self):
        _, body = self.pay()
        reference = body["payment"]["reference"]
        req = urllib.request.Request(
            self.base + f"/api/billing/demo/{reference}",
            data=b'{"action":"approve"}',
            method="POST",
        )
        req.add_header("Content-Type", "application/json")
        try:
            urllib.request.urlopen(req, timeout=10)
            self.fail("expected 401")
        except urllib.error.HTTPError as error:
            self.assertEqual(error.code, 401)


if __name__ == "__main__":
    unittest.main()

class TokenDownloadTest(ServerTestCase):
    """/d/<token> - the opaque download link.

    The URL carries nothing but a random token, so the only way it may resolve
    is through the session's own workspace: your token works for you, and for
    nobody else it exists at all.
    """

    def setUp(self):
        super().setUp()
        _, body = self.request("POST", "/api/apps", {
            "name": "Shop",
            "website_url": "https://shop.example.com",
            "android_package_id": "com.example.shop",
        })
        self.app_id = body["app"]["id"]

        _, session_body = self.request("GET", "/api/auth/session")
        store = self.app.workspaces.for_user(session_body["user"]["id"])
        directory = store.build_dir(self.app_id, 1)
        directory.mkdir(parents=True)
        (directory / "Shop-1.0.0.apk").write_bytes(b"apk-bytes")
        (directory / "build.json").write_text(json.dumps({
            "number": 1, "app_id": self.app_id, "status": "succeeded",
            "artifacts": [
                {"name": "Shop-1.0.0.apk", "kind": "apk", "size": 9,
                 "token": "t0k3n-abc123"},
            ],
        }), encoding="utf-8")

    def fetch(self, path, session=None):
        req = urllib.request.Request(self.base + path)
        cookie = self.session if session is None else session
        if cookie:
            req.add_header("Cookie", cookie)
        try:
            with urllib.request.urlopen(req, timeout=10) as response:
                return response.status, response.read(), dict(response.headers)
        except urllib.error.HTTPError as error:
            return error.code, error.read(), dict(error.headers)

    def test_the_owner_gets_the_file_with_its_real_name(self):
        status, body, headers = self.fetch("/d/t0k3n-abc123")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"apk-bytes")
        # The pretty filename moved out of the URL and into this header.
        self.assertIn("Shop-1.0.0.apk", headers.get("Content-Disposition", ""))

    def test_the_build_list_carries_the_token_through(self):
        _, body = self.request("GET", f"/api/apps/{self.app_id}/builds")
        self.assertEqual(body["builds"][0]["artifacts"][0]["token"], "t0k3n-abc123")

    def test_somebody_elses_session_finds_nothing(self):
        other = self.sign_up("Okello James", "0700333444")
        status, _, _ = self.fetch("/d/t0k3n-abc123", session=other)
        self.assertEqual(status, 404)

    def test_no_session_means_401_not_404(self):
        # Signed out is "sign in", not "gone" - the link works again after login.
        status, _, _ = self.fetch("/d/t0k3n-abc123", session="")
        self.assertEqual(status, 401)

    def test_an_unknown_token_is_a_404(self):
        status, _, _ = self.fetch("/d/no-such-token")
        self.assertEqual(status, 404)

    def test_the_readable_route_still_works(self):
        # Old builds and anything scripted against the API keep their links.
        status, body, _ = self.fetch(
            f"/api/apps/{self.app_id}/builds/1/artifacts/Shop-1.0.0.apk"
        )
        self.assertEqual(status, 200)
        self.assertEqual(body, b"apk-bytes")

class FilePreviewTest(ServerTestCase):
    """GET on an upload slot - the thumbnail the Branding page shows."""

    def setUp(self):
        super().setUp()
        _, body = self.request("POST", "/api/apps", {
            "name": "Shop",
            "website_url": "https://shop.example.com",
            "android_package_id": "com.example.shop",
        })
        self.app_id = body["app"]["id"]

    def put_file(self, slot, name, payload):
        req = urllib.request.Request(
            f"{self.base}/api/apps/{self.app_id}/files/{slot}",
            data=payload, method="PUT",
        )
        req.add_header("Cookie", self.session)
        req.add_header("X-Filename", name)
        with urllib.request.urlopen(req, timeout=10) as response:
            return response.status

    def get_raw(self, path, session=None):
        req = urllib.request.Request(self.base + path)
        cookie = self.session if session is None else session
        if cookie:
            req.add_header("Cookie", cookie)
        try:
            with urllib.request.urlopen(req, timeout=10) as response:
                return response.status, response.read(), dict(response.headers)
        except urllib.error.HTTPError as error:
            return error.code, error.read(), dict(error.headers)

    def test_an_uploaded_icon_comes_back_inline_as_an_image(self):
        self.put_file("icon", "logo.png", b"png-bytes")
        status, body, headers = self.get_raw(f"/api/apps/{self.app_id}/files/icon")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"png-bytes")
        self.assertEqual(headers.get("Content-Type"), "image/png")
        self.assertIn("inline", headers.get("Content-Disposition", ""))

    def test_an_empty_slot_is_a_404_not_a_broken_image_page(self):
        status, _, _ = self.get_raw(f"/api/apps/{self.app_id}/files/splash")
        self.assertEqual(status, 404)

    def test_the_keystore_has_no_preview(self):
        # It is a secret; nothing in the UI needs to read it back.
        self.put_file("keystore", "upload.jks", b"jks-bytes")
        status, _, _ = self.get_raw(f"/api/apps/{self.app_id}/files/keystore")
        self.assertEqual(status, 404)

    def test_somebody_else_cannot_see_your_icon(self):
        self.put_file("icon", "logo.png", b"png-bytes")
        other = self.sign_up("Okello James", "0700333444")
        status, _, _ = self.get_raw(
            f"/api/apps/{self.app_id}/files/icon", session=other
        )
        self.assertEqual(status, 404)

    def test_downloads_are_still_attachments(self):
        # The inline flag is for previews only - artifacts keep saving to disk.
        _, session_body = self.request("GET", "/api/auth/session")
        store = self.app.workspaces.for_user(session_body["user"]["id"])
        directory = store.build_dir(self.app_id, 1)
        directory.mkdir(parents=True)
        (directory / "Shop-1.0.0.apk").write_bytes(b"apk")
        (directory / "build.json").write_text(json.dumps({
            "artifacts": [{"name": "Shop-1.0.0.apk", "kind": "apk", "size": 3,
                           "token": "tok-xyz"}],
        }), encoding="utf-8")
        status, _, headers = self.get_raw("/d/tok-xyz")
        self.assertEqual(status, 200)
        self.assertIn("attachment", headers.get("Content-Disposition", ""))

    def test_a_stale_save_cannot_unhook_an_uploaded_file(self):
        # The editor's draft is a snapshot from before the upload. Saving it
        # used to wipe the icon off the config, and the next build shipped
        # Flutter's default icon.
        self.put_file("icon", "logo.png", b"png-bytes")
        self.request("PUT", f"/api/apps/{self.app_id}", {
            "app_name": "Shop!", "icon_file": None, "splash_file": None,
        })
        _, body = self.request("GET", f"/api/apps/{self.app_id}")
        self.assertEqual(body["app"]["app_name"], "Shop!")
        self.assertEqual(body["app"]["icon_file"], "icon.png")

    def test_removing_a_file_still_works_through_its_own_endpoint(self):
        self.put_file("icon", "logo.png", b"png-bytes")
        self.request("DELETE", f"/api/apps/{self.app_id}/files/icon")
        _, body = self.request("GET", f"/api/apps/{self.app_id}")
        self.assertIsNone(body["app"]["icon_file"])


class FirebaseUploadTest(ServerTestCase):
    """Uploading a Firebase configuration file.

    The extension check is not the point. A file from the wrong Firebase app
    parses perfectly and produces an app that installs and never receives a
    notification, so it has to be read and compared here.
    """

    def setUp(self):
        super().setUp()
        _, body = self.request("POST", "/api/apps", {
            "name": "Shop",
            "website_url": "https://shop.example.com",
            "android_package_id": "com.example.shop",
        })
        self.app_id = body["app"]["id"]

    def android_file(self, package="com.example.shop"):
        return json.dumps({
            "project_info": {"project_id": "shop-mobile", "project_number": "1"},
            "client": [{
                "client_info": {
                    "mobilesdk_app_id": "1:1:android:a",
                    "android_client_info": {"package_name": package},
                },
            }],
        }).encode("utf-8")

    def put_file(self, slot, name, payload):
        req = urllib.request.Request(
            f"{self.base}/api/apps/{self.app_id}/files/{slot}",
            data=payload, method="PUT",
        )
        req.add_header("Cookie", self.session)
        req.add_header("X-Filename", name)
        try:
            with urllib.request.urlopen(req, timeout=10) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def test_a_matching_file_is_accepted(self):
        status, body = self.put_file(
            "firebase_android", "google-services.json", self.android_file()
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            body["app"]["firebase_android_file"], "firebase_android.json"
        )

    def test_a_file_for_another_app_is_refused(self):
        status, body = self.put_file(
            "firebase_android", "google-services.json",
            self.android_file(package="com.other.app"),
        )
        self.assertEqual(status, 422)
        self.assertIn("com.other.app", body["error"])
        self.assertIn("com.example.shop", body["error"])

    def test_a_refused_file_is_not_stored(self):
        self.put_file("firebase_android", "google-services.json",
                      self.android_file(package="com.other.app"))
        _, body = self.request("GET", f"/api/apps/{self.app_id}")
        self.assertIsNone(body["app"]["firebase_android_file"])

    def test_something_that_is_not_a_firebase_file_is_refused(self):
        status, body = self.put_file(
            "firebase_android", "google-services.json", b'{"hello": "world"}'
        )
        self.assertEqual(status, 422)
        self.assertIn("Firebase", body["error"])

    def test_the_status_reports_the_project(self):
        self.put_file("firebase_android", "google-services.json",
                      self.android_file())
        _, body = self.request("GET", f"/api/apps/{self.app_id}")
        android = body["app"]["firebase"]["android"]
        self.assertTrue(android["ok"])
        self.assertEqual(android["project_id"], "shop-mobile")

    def test_the_status_notices_a_package_changed_after_the_upload(self):
        # Recomputed on every read rather than stored, because the thing it
        # disagrees with is editable on another page.
        self.put_file("firebase_android", "google-services.json",
                      self.android_file())
        self.request("PUT", f"/api/apps/{self.app_id}",
                     {"android_package_id": "com.example.renamed"})
        _, body = self.request("GET", f"/api/apps/{self.app_id}")
        android = body["app"]["firebase"]["android"]
        self.assertFalse(android["ok"])
        self.assertIn("com.example.renamed", android["problem"])

    def test_nothing_uploaded_reports_nothing(self):
        _, body = self.request("GET", f"/api/apps/{self.app_id}")
        self.assertIsNone(body["app"]["firebase"]["android"])
        self.assertIsNone(body["app"]["firebase"]["ios"])

    def test_the_file_can_be_removed(self):
        self.put_file("firebase_android", "google-services.json",
                      self.android_file())
        _, body = self.request(
            "DELETE", f"/api/apps/{self.app_id}/files/firebase_android"
        )
        self.assertIsNone(body["app"]["firebase_android_file"])

    def test_an_upload_leaves_the_package_id_alone(self):
        # The check reads android_package_id and the store writes
        # firebase_android_file. Confusing the two writes the filename into
        # the package id, which passes validation and breaks the build.
        self.put_file("firebase_android", "google-services.json",
                      self.android_file())
        _, body = self.request("GET", f"/api/apps/{self.app_id}")
        self.assertEqual(body["app"]["android_package_id"], "com.example.shop")


class DocsTest(ServerTestCase):
    """The documentation site under web/docs/.

    Signed out, because somebody evaluating the product reads the docs before
    they have an account.
    """

    signed_in = False

    def setUp(self):
        super().setUp()
        docs = self.web / "docs"
        docs.mkdir()
        (docs / "index.html").write_text("<h1>Documentation</h1>", encoding="utf-8")
        (docs / "studio.html").write_text("<h1>Studio</h1>", encoding="utf-8")
        (docs / "docs.css").write_text("body { color: red; }", encoding="utf-8")

    def get(self, path):
        req = urllib.request.Request(self.base + path)
        try:
            with urllib.request.urlopen(req, timeout=10) as response:
                return response.status, response.read().decode()
        except urllib.error.HTTPError as error:
            return error.code, error.read().decode()

    def test_the_index_is_served_without_a_session(self):
        status, body = self.get("/docs")
        self.assertEqual(status, 200)
        self.assertIn("Documentation", body)

    def test_a_trailing_slash_is_the_same_page(self):
        self.assertEqual(self.get("/docs/")[0], 200)

    def test_a_page_works_without_its_extension(self):
        # Which is what anybody links to.
        status, body = self.get("/docs/studio")
        self.assertEqual(status, 200)
        self.assertIn("Studio", body)

    def test_the_extension_still_works(self):
        self.assertEqual(self.get("/docs/studio.html")[0], 200)

    def test_assets_are_served(self):
        status, body = self.get("/docs/docs.css")
        self.assertEqual(status, 200)
        self.assertIn("color: red", body)

    def test_a_page_that_does_not_exist_is_still_a_404(self):
        # The extensionless rule must not turn every typo into a page.
        self.assertEqual(self.get("/docs/nonsense")[0], 404)
        self.assertEqual(self.get("/docs/nonsense.html")[0], 404)

    def test_it_cannot_be_used_to_escape_the_web_directory(self):
        for path in ("/docs/../../accounts.db", "/docs/../server.py"):
            with self.subTest(path=path):
                self.assertNotEqual(self.get(path)[0], 200)


class PushDocsTest(ServerTestCase):
    """The generated integration guide."""

    def setUp(self):
        super().setUp()
        _, body = self.request("POST", "/api/apps", {
            "name": "Shop",
            "website_url": "https://shop.example.com",
            "android_package_id": "com.example.shop",
        })
        self.app_id = body["app"]["id"]

    def upload_config(self, project="shop-mobile"):
        payload = json.dumps({
            "project_info": {"project_id": project, "project_number": "1"},
            "client": [{
                "client_info": {
                    "mobilesdk_app_id": "1:1:android:a",
                    "android_client_info": {"package_name": "com.example.shop"},
                },
            }],
        }).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base}/api/apps/{self.app_id}/files/firebase_android",
            data=payload, method="PUT",
        )
        req.add_header("Cookie", self.session)
        req.add_header("X-Filename", "google-services.json")
        urllib.request.urlopen(req, timeout=10).read()

    def guide(self, stack="node"):
        return self.request(
            "GET", f"/api/apps/{self.app_id}/push-docs?stack={stack}")

    def test_it_lists_the_stacks_it_covers(self):
        _, body = self.guide()
        ids = [s["id"] for s in body["stacks"]]
        self.assertIn("node", ids)
        self.assertIn("rest", ids)

    def test_the_project_id_comes_from_the_uploaded_file(self):
        # Not from anything typed in, so the guide cannot describe a project
        # the app is not actually built against.
        self.upload_config()
        _, body = self.guide()
        self.assertTrue(body["configured"])
        self.assertIn("shop-mobile", body["guide"]["code"])

    def test_without_a_file_it_says_so_rather_than_inventing_one(self):
        _, body = self.guide()
        self.assertFalse(body["configured"])
        self.assertIn("your-project-id", body["guide"]["code"])

    def test_the_example_uses_a_configured_category(self):
        self.upload_config()
        self.request("PUT", f"/api/apps/{self.app_id}", {
            "push_enabled": True,
            "push_topics": [{"id": "orders", "label": "Orders", "default": True}],
        })
        _, body = self.guide()
        self.assertEqual(body["guide"]["topic"], "orders")
        self.assertIn("orders", body["guide"]["code"])

    def test_every_stack_renders(self):
        for stack in ("node", "php", "laravel", "python", "dotnet",
                      "wordpress", "rest"):
            with self.subTest(stack=stack):
                status, body = self.guide(stack)
                self.assertEqual(status, 200)
                self.assertTrue(body["guide"]["code"].strip())

    def test_every_guide_warns_about_the_service_account_key(self):
        # The one mistake that would matter, in the place it is read.
        for stack in ("node", "php", "python", "rest"):
            with self.subTest(stack=stack):
                _, body = self.guide(stack)
                joined = " ".join(body["guide"]["notes"])
                self.assertIn("server only", joined)

    def test_an_unknown_stack_is_a_404(self):
        status, _ = self.guide("cobol")
        self.assertEqual(status, 404)
