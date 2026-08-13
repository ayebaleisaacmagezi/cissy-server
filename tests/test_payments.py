"""Payments: the reply reader, the poll loop, and the paths that lose money.

Weighted towards the unhappy cases on purpose. The happy path is one line; the
ways a poll-based integration silently takes someone's money and gives them
nothing are several, and each one is a test here.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from cissy import collecto, payments
from cissy.collecto import DemoGateway, Reply
from cissy.errors import ValidationError
from cissy.payments import (
    ABANDONED,
    FAILED,
    OPEN,
    SUCCESSFUL,
    PaymentService,
    PaymentStore,
    Subscription,
)


class FakeGateway:
    """A gateway whose every answer the test dictates."""

    mode = "demo"

    def __init__(self, replies=None, on_pay=None) -> None:
        self.replies = list(replies or [])
        self.on_pay = on_pay or Reply(accepted=True, status=collecto.PENDING)
        self.pay_calls: list[dict] = []
        self.status_calls: list[str] = []

    def request_to_pay(self, *, phone, amount, reference):
        self.pay_calls.append({"phone": phone, "amount": amount, "reference": reference})
        if isinstance(self.on_pay, Exception):
            raise self.on_pay
        return self.on_pay

    def request_to_pay_status(self, *, reference):
        self.status_calls.append(reference)
        if not self.replies:
            return Reply(accepted=True, status=collecto.PENDING)
        return self.replies.pop(0)


class ServiceCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def service(self, gateway) -> PaymentService:
        return PaymentService(
            store=PaymentStore(self.root / "payments"),
            gateway=gateway,
            subscription=Subscription(self.root / "subscription.json"),
        )


class ReadingReplies(unittest.TestCase):
    def test_pending_is_the_answer_for_anything_unrecognised(self):
        # An unknown word is missing information, and the safe reading of
        # missing information about money is "ask again".
        for word in ("", "weird", None, "in_progress", "PENDING"):
            self.assertEqual(collecto.normalise_status(word), collecto.PENDING)

    def test_the_documented_success_words_all_count(self):
        for word in ("SUCCESSFUL", "success", "Completed", "confirmed"):
            self.assertEqual(collecto.normalise_status(word), SUCCESSFUL)

    def test_accepted_and_paid_are_separate_answers(self):
        # The classic way to mark an unpaid subscription paid: reading the
        # boolean flag as though it meant the money arrived.
        reply = collecto._read(
            {"data": {"requestToPay": True, "status": "PENDING"}}, "requestToPay"
        )
        self.assertTrue(reply.accepted)
        self.assertEqual(reply.status, collecto.PENDING)
        self.assertFalse(reply.settled)

    def test_a_refusal_is_not_a_pending_payment(self):
        # Their real shape for a bad key or a wrong source IP: HTTP 200, an
        # envelope that says "success", and the actual answer hidden two levels
        # down. Read only the envelope and this looks like an unanswered prompt.
        reply = collecto._read(
            {
                "status": 200,
                "status_message": "success",
                "data": {
                    "status": "error",
                    "message": "API key/IP mismatch or user is inactive/deleted.",
                },
            },
            "requestToPay",
        )
        self.assertTrue(reply.refused)
        self.assertFalse(reply.accepted)

    def test_silence_is_never_read_as_a_refusal(self):
        # Refusal has to come from a word they sent. A reply with no status at
        # all is missing information, which stays pending.
        reply = collecto._read({"data": {"requestToPay": True}}, "requestToPay")
        self.assertFalse(reply.refused)
        self.assertEqual(reply.status, collecto.PENDING)


class CheckingTheCredentials(unittest.TestCase):
    """`checkup` is what stands between a moved server and silent lost sales."""

    def _balances(self, *replies):
        return mock.patch.object(
            collecto.CollectoGateway, "_post", side_effect=list(replies)
        )

    def _ok(self, message, amount="4,855.00"):
        return Reply(
            accepted=True,
            status=collecto.PENDING,
            message=message,
            raw={"data": {"balance": {"type": "SMS", "amount": amount}}},
        )

    def test_an_unconfigured_server_is_not_reported_as_healthy(self):
        ok, lines = collecto.checkup(collecto.Settings())
        self.assertFalse(ok)
        self.assertIn("simulator", " ".join(lines))

    def test_healthy_credentials_pass(self):
        settings = collecto.Settings(username="web2app", api_key="k")
        with self._balances(self._ok("Your SMS balance is 4,855.00"), self._ok("ok")):
            ok, lines = collecto.checkup(settings)
        self.assertTrue(ok)
        self.assertIn("web2app", lines[0])

    def test_a_refusal_names_the_likely_cause(self):
        # The one that matters: the key is issued against an IP, so this is what
        # a moved box or a new proxy looks like.
        refused = Reply(
            accepted=False,
            status=collecto.PENDING,
            refused=True,
            message="API key/IP mismatch or user is inactive/deleted.",
        )
        settings = collecto.Settings(username="web2app", api_key="k")
        with self._balances(refused, refused):
            ok, lines = collecto.checkup(settings)
        self.assertFalse(ok)
        self.assertIn("IP", " ".join(lines))

    def test_an_empty_sms_balance_is_called_out(self):
        # Credentials are fine, but nobody can complete a signup.
        settings = collecto.Settings(username="web2app", api_key="k")
        with self._balances(self._ok("Your SMS balance is 0", amount="0"), self._ok("ok")):
            ok, lines = collecto.checkup(settings)
        self.assertTrue(ok)
        self.assertIn("Verification codes will not send", " ".join(lines))

    def test_a_comma_formatted_balance_is_still_a_number(self):
        self.assertEqual(collecto._balance(self._ok("x", amount="4,855.00")), 4855.0)

    def test_a_reply_with_no_balance_does_not_explode(self):
        self.assertIsNone(
            collecto._balance(Reply(accepted=True, status=collecto.PENDING))
        )


class StartingAPayment(ServiceCase):
    def test_the_record_exists_before_the_gateway_is_called(self):
        # If the process dies mid-call the reference is the only thread back to
        # the money, so it has to be on disk first.
        seen: list[list[str]] = []
        store = PaymentStore(self.root / "payments")

        class Watcher(FakeGateway):
            def request_to_pay(self, **kwargs):
                seen.append([p.reference for p in store.list()])
                return super().request_to_pay(**kwargs)

        service = self.service(Watcher())
        payment = service.start(plan_id="starter", phone="0772000000")
        self.assertEqual(seen, [[payment.reference]])

    def test_a_successful_send_does_not_settle_anything(self):
        gateway = FakeGateway(
            on_pay=Reply(accepted=True, status=SUCCESSFUL, message="ok")
        )
        service = self.service(gateway)
        payment = service.start(plan_id="starter", phone="0772000000")
        # Even if requestToPay claims success, only the status endpoint counts.
        self.assertEqual(payment.status, OPEN)
        self.assertFalse(Subscription(self.root / "subscription.json").read()["active"])

    def test_a_refused_call_fails_the_payment_immediately(self):
        # The failure this prevents: auth breaks, every prompt silently never
        # sends, and two minutes later each customer is told they were too slow
        # to approve something that never reached their phone.
        gateway = FakeGateway(
            on_pay=Reply(
                accepted=False,
                status=collecto.PENDING,
                refused=True,
                message="API key/IP mismatch or user is inactive/deleted.",
            )
        )
        service = self.service(gateway)
        payment = service.start(plan_id="starter", phone="0772000000")

        self.assertEqual(payment.status, FAILED)
        self.assertIn("Nothing was charged", payment.message)
        # The customer is not shown a sentence about API keys, but an operator
        # reading the trail can see exactly what the gateway said.
        self.assertNotIn("API key", payment.message)
        self.assertIn("API key/IP mismatch", payment.trail[-1])

    def test_a_refused_payment_is_not_chased(self):
        gateway = FakeGateway(
            on_pay=Reply(accepted=False, status=collecto.PENDING, refused=True)
        )
        service = self.service(gateway)
        service.start(plan_id="starter", phone="0772000000")
        self.assertEqual(service.sweep(), 0)
        self.assertEqual(gateway.status_calls, [])

    def test_the_phone_number_is_normalised_before_sending(self):
        gateway = FakeGateway()
        self.service(gateway).start(plan_id="starter", phone="+256 772 000 000")
        self.assertEqual(gateway.pay_calls[0]["phone"], "256772000000")

    def test_a_local_number_gains_the_country_code(self):
        self.assertEqual(payments.normalise_phone("0772000000"), "256772000000")

    def test_nonsense_is_refused_with_something_actionable(self):
        with self.assertRaises(ValidationError) as caught:
            payments.normalise_phone("call me")
        self.assertIn("0772000000", str(caught.exception))

    def test_an_unknown_plan_is_refused(self):
        with self.assertRaises(ValidationError):
            self.service(FakeGateway()).start(plan_id="platinum", phone="0772000000")

    def test_references_do_not_collide(self):
        made = {payments.new_reference() for _ in range(200)}
        self.assertEqual(len(made), 200)


class Polling(ServiceCase):
    def test_success_activates_the_plan(self):
        gateway = FakeGateway([Reply(accepted=True, status=SUCCESSFUL)])
        service = self.service(gateway)
        payment = service.start(plan_id="starter", phone="0772000000")

        settled = service.poll(payment.reference)
        self.assertEqual(settled.status, SUCCESSFUL)
        record = Subscription(self.root / "subscription.json").read()
        self.assertTrue(record["active"])
        self.assertEqual(record["reference"], payment.reference)

    def test_a_settled_payment_is_never_polled_again(self):
        gateway = FakeGateway([Reply(accepted=True, status=SUCCESSFUL)])
        service = self.service(gateway)
        payment = service.start(plan_id="starter", phone="0772000000")

        service.poll(payment.reference)
        service.poll(payment.reference)
        # One activation, one lookup. Polling a paid subscription again is how
        # one payment quietly grants two months.
        self.assertEqual(len(gateway.status_calls), 1)

    def test_an_unreachable_gateway_leaves_the_payment_open(self):
        # The dangerous alternative is calling it failed: the customer may have
        # already approved it.
        gateway = FakeGateway(
            [Reply(accepted=False, status=collecto.PENDING, message="Could not reach")]
        )
        service = self.service(gateway)
        payment = service.start(plan_id="starter", phone="0772000000")

        after = service.poll(payment.reference)
        self.assertEqual(after.status, OPEN)
        self.assertFalse(Subscription(self.root / "subscription.json").read()["active"])

    def test_a_hard_gateway_error_during_lookup_does_not_fail_the_payment(self):
        class Broken(FakeGateway):
            def request_to_pay_status(self, *, reference):
                raise collecto.GatewayError("Collecto rejected the request (403).")

        service = self.service(Broken())
        payment = service.start(plan_id="starter", phone="0772000000")
        after = service.poll(payment.reference)
        self.assertEqual(after.status, OPEN)

    def test_an_explicit_failure_does_fail_the_payment(self):
        gateway = FakeGateway([Reply(accepted=True, status=FAILED, message="Declined")])
        service = self.service(gateway)
        payment = service.start(plan_id="starter", phone="0772000000")
        after = service.poll(payment.reference)
        self.assertEqual(after.status, FAILED)
        self.assertFalse(Subscription(self.root / "subscription.json").read()["active"])

    def test_an_unanswered_prompt_is_abandoned_not_failed(self):
        service = self.service(FakeGateway())
        payment = service.start(plan_id="starter", phone="0772000000")

        # Wind the clock back past the chase window.
        store = service.store
        from dataclasses import replace

        store.save(
            replace(payment, started=payment.started - payments.CHASE_SECONDS - 1)
        )

        after = service.poll(payment.reference)
        self.assertEqual(after.status, ABANDONED)
        self.assertIn("try again", after.message)

    def test_the_sweeper_resumes_payments_it_did_not_start(self):
        # The property that matters when the browser is closed or the server is
        # restarted mid-payment: state lives on disk, not in the request.
        gateway = FakeGateway()
        first = self.service(gateway)
        payment = first.start(plan_id="starter", phone="0772000000")

        from dataclasses import replace

        first.store.save(replace(first.store.get(payment.reference), next_poll=0))

        fresh = self.service(FakeGateway([Reply(accepted=True, status=SUCCESSFUL)]))
        self.assertEqual(fresh.sweep(), 1)
        self.assertEqual(fresh.store.get(payment.reference).status, SUCCESSFUL)

    def test_one_unreadable_record_does_not_stop_the_others(self):
        service = self.service(FakeGateway([Reply(accepted=True, status=SUCCESSFUL)]))
        good = service.start(plan_id="starter", phone="0772000000")
        (self.root / "payments" / "broken.json").write_text("{ not json", encoding="utf-8")

        from dataclasses import replace

        service.store.save(replace(service.store.get(good.reference), next_poll=0))
        service.sweep()
        self.assertEqual(service.store.get(good.reference).status, SUCCESSFUL)


class References(ServiceCase):
    def test_a_reference_cannot_escape_the_payments_directory(self):
        store = PaymentStore(self.root / "payments")
        for bad in ("../secrets", "/etc/passwd", "a/b"):
            with self.assertRaises(ValidationError):
                store.path(bad)


class Demo(ServiceCase):
    def test_the_handset_drives_the_outcome(self):
        gateway = DemoGateway(self.root / "demo")
        service = self.service(gateway)
        payment = service.start(plan_id="pro", phone="0772000000")

        self.assertEqual(service.poll(payment.reference).status, OPEN)
        gateway.act(payment.reference, "approve")
        self.assertEqual(service.poll(payment.reference).status, SUCCESSFUL)

    def test_declining_fails_the_payment(self):
        gateway = DemoGateway(self.root / "demo")
        service = self.service(gateway)
        payment = service.start(plan_id="starter", phone="0772000000")
        gateway.act(payment.reference, "decline")
        self.assertEqual(service.poll(payment.reference).status, FAILED)

    def test_a_dropped_connection_recovers_rather_than_failing(self):
        gateway = DemoGateway(self.root / "demo")
        service = self.service(gateway)
        payment = service.start(plan_id="starter", phone="0772000000")
        gateway.set_scenario(payment.reference, "flaky")

        self.assertEqual(service.poll(payment.reference).status, OPEN)  # the drop
        gateway.act(payment.reference, "approve")
        self.assertEqual(service.poll(payment.reference).status, SUCCESSFUL)

    def test_a_non_json_body_is_treated_as_pending(self):
        gateway = DemoGateway(self.root / "demo")
        service = self.service(gateway)
        payment = service.start(plan_id="starter", phone="0772000000")
        gateway.set_scenario(payment.reference, "garbage")
        self.assertEqual(service.poll(payment.reference).status, OPEN)

    def test_prompts_survive_a_restart(self):
        first = DemoGateway(self.root / "demo")
        service = self.service(first)
        payment = service.start(plan_id="starter", phone="0772000000")
        first.act(payment.reference, "approve")

        second = DemoGateway(self.root / "demo")
        reply = second.request_to_pay_status(reference=payment.reference)
        self.assertEqual(reply.status, SUCCESSFUL)


class Money(unittest.TestCase):
    def test_the_amount_charged_is_the_plan_not_the_request(self):
        # Nothing in the browser gets to say what a subscription costs.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            gateway = FakeGateway()
            service = PaymentService(
                store=PaymentStore(root / "payments"),
                gateway=gateway,
                subscription=Subscription(root / "subscription.json"),
            )
            payment = service.start(plan_id="starter", phone="0772000000")
            self.assertEqual(payment.amount, payments.PLANS["starter"].amount)
            self.assertEqual(gateway.pay_calls[0]["amount"], payment.amount)

    def test_no_secret_reaches_a_stored_payment(self):
        # Whatever else changes, an API key must never end up in a file the
        # download and listing endpoints hand out.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = PaymentService(
                store=PaymentStore(root / "payments"),
                gateway=FakeGateway(),
                subscription=Subscription(root / "subscription.json"),
            )
            payment = service.start(plan_id="starter", phone="0772000000")
            written = json.loads(
                (root / "payments" / f"{payment.reference}.json").read_text("utf-8")
            )
            for banned in ("api_key", "x-api-key", "key", "password", "pin"):
                self.assertNotIn(banned, written)


if __name__ == "__main__":
    unittest.main()
