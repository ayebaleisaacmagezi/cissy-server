"""Subscription payments: the record on disk, and the loop that chases them.

Because Collecto has no webhook, a payment is not an event that arrives - it is
a question we keep asking until it answers. That makes the two hard parts of
this file:

**The record is written before the call.** If the process dies between sending
`requestToPay` and hearing back, the only thread left to the money is the
reference, and it has to already be on disk. Writing it afterwards would leave
payments that happened and that we have no name for.

**The poller outlives the browser.** Someone can approve a prompt forty seconds
after closing the tab, or while the server is restarting. A sweeper thread walks
every open payment on an interval and picks up where it left off after a
restart, because the state it needs is all in files.

There are no user accounts yet, so a successful payment activates a single
server-wide subscription. That is a placeholder with a real shape: when accounts
land, `subscription.json` moves from the root to the user's directory and
nothing else here changes.
"""

from __future__ import annotations

import json
import os
import secrets
import threading
import time
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from . import collecto
from .collecto import FAILED, PENDING, SUCCESSFUL, Reply
from .errors import NotFoundError, ValidationError

@dataclass(frozen=True)
class Plan:
    id: str
    name: str
    amount: int
    builds: int
    blurb: str


# Amounts are shillings, and they have to be: Collecto charges an integer UGX
# amount and has no currency field. Starter is "$5" as a decision, but the price
# stored here is the pinned shilling figure rather than anything computed from a
# rate - a live rate would move the price daily and charge two customers on the
# same plan differently. Revisit these deliberately when the rate has drifted.
PLANS: dict[str, Plan] = {
    "starter": Plan("starter", "Starter", 19_000, 25, "25 builds a month"),
    "pro": Plan("pro", "Business", 57_000, 100, "100 builds a month"),
}

# How long a prompt is chased before it is called abandoned. It sits on someone's
# handset until they act, and in practice a prompt is either approved within
# seconds or not at all. The cost of the short window: approve at minute three
# and the payment is already abandoned while the money has moved. Rare, and
# recoverable by hand - which is the better trade than holding every failed
# attempt open for five minutes.
CHASE_SECONDS = 2 * 60

# Tight while the customer is plausibly still looking at the phone, slower after.
FAST_INTERVAL = 4.0
SLOW_INTERVAL = 10.0
FAST_WINDOW = 60.0

OPEN = PENDING
ABANDONED = "abandoned"


def _now() -> float:
    return time.time()


def _stamp(at: float | None = None) -> str:
    return datetime.fromtimestamp(at or _now(), timezone.utc).isoformat(
        timespec="seconds"
    )


@dataclass(frozen=True)
class Payment:
    reference: str
    plan: str
    amount: int
    phone: str
    owner: str = ""
    status: str = OPEN
    mode: str = "demo"
    message: str = ""
    transaction_id: str = ""
    created_at: str = ""
    updated_at: str = ""
    started: float = 0.0
    next_poll: float = 0.0
    checks: int = 0
    trail: tuple[str, ...] = ()

    @property
    def settled(self) -> bool:
        return self.status in (SUCCESSFUL, FAILED, ABANDONED)

    @property
    def deadline(self) -> float:
        return self.started + CHASE_SECONDS

    def to_json(self) -> dict[str, Any]:
        plan = PLANS.get(self.plan)
        data = {
            "reference": self.reference,
            "plan": self.plan,
            # The id is a database key. Sent beside it so no screen has to work
            # out on its own that "pro" is read aloud as "Business".
            "plan_name": plan.name if plan else self.plan,
            "amount": self.amount,
            "phone": self.phone,
            "owner": self.owner,
            "status": self.status,
            "mode": self.mode,
            "message": self.message,
            "transaction_id": self.transaction_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "started": self.started,
            "next_poll": self.next_poll,
            "checks": self.checks,
            "trail": list(self.trail),
        }
        data["expires_in"] = max(0, int(self.deadline - _now())) if not self.settled else 0
        return data

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "Payment":
        return cls(
            reference=str(data.get("reference", "")),
            plan=str(data.get("plan", "")),
            amount=int(data.get("amount", 0)),
            phone=str(data.get("phone", "")),
            owner=str(data.get("owner", "")),
            status=str(data.get("status", OPEN)),
            mode=str(data.get("mode", "demo")),
            message=str(data.get("message", "")),
            transaction_id=str(data.get("transaction_id", "")),
            created_at=str(data.get("created_at", "")),
            updated_at=str(data.get("updated_at", "")),
            started=float(data.get("started", 0.0)),
            next_poll=float(data.get("next_poll", 0.0)),
            checks=int(data.get("checks", 0)),
            trail=tuple(str(line) for line in data.get("trail", [])),
        )


class PaymentStore:
    """One JSON file per payment, named by reference."""

    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)

    def path(self, reference: str) -> Path:
        safe = "".join(c for c in reference if c.isalnum() or c in "-_")
        if not safe or safe != reference:
            raise ValidationError("That is not a valid payment reference.")
        return self.directory / f"{safe}.json"

    def save(self, payment: Payment) -> Payment:
        payment = replace(payment, updated_at=_stamp())
        path = self.path(payment.reference)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payment.to_json(), indent=2), encoding="utf-8")
        os.replace(tmp, path)
        return payment

    def get(self, reference: str) -> Payment:
        path = self.path(reference)
        if not path.is_file():
            raise NotFoundError(f"No payment with reference {reference}.")
        return Payment.from_json(json.loads(path.read_text(encoding="utf-8")))

    def list(self) -> list[Payment]:
        found: list[Payment] = []
        for path in self.directory.glob("*.json"):
            try:
                found.append(
                    Payment.from_json(json.loads(path.read_text(encoding="utf-8")))
                )
            except (OSError, json.JSONDecodeError, ValueError):
                continue
        found.sort(key=lambda p: p.created_at, reverse=True)
        return found

    def open_payments(self) -> list[Payment]:
        return [p for p in self.list() if not p.settled]

    def for_owner(self, owner: str, limit: int = 20) -> list[Payment]:
        """One customer's payments. Never anybody else's."""
        return [p for p in self.list() if p.owner == owner][:limit]


class Subscription:
    """What a paid-for month looks like while there are no accounts.

    Deliberately thin. Its only job is to prove the loop closes - money moves,
    something changes - and to be easy to move under a user id later.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def read(self) -> dict[str, Any]:
        if not self.path.is_file():
            return {"plan": "free", "active": False}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"plan": "free", "active": False}

    def activate(self, payment: Payment) -> dict[str, Any]:
        plan = PLANS.get(payment.plan)
        until = datetime.now(timezone.utc) + timedelta(days=30)
        record = {
            "plan": payment.plan,
            "plan_name": plan.name if plan else payment.plan,
            "builds": plan.builds if plan else 0,
            "active": True,
            "since": _stamp(),
            "until": until.isoformat(timespec="seconds"),
            "reference": payment.reference,
            "amount": payment.amount,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(record, indent=2), encoding="utf-8")
        return record


class PaymentService:
    """Starts payments and keeps asking about them until they answer."""

    def __init__(
        self,
        *,
        store: PaymentStore,
        gateway: Any,
        subscription: Subscription,
        on_paid: Callable[[Payment], None] | None = None,
    ) -> None:
        self.store = store
        self.gateway = gateway
        self.subscription = subscription
        # Called once, when a payment first reaches SUCCESSFUL. This is where a
        # user's plan gets switched on. It is a hook rather than a direct call
        # into accounts so that this module stays ignorant of who anyone is.
        self.on_paid = on_paid
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    # ── starting one ─────────────────────────────────────────────────────

    def start(self, *, plan_id: str, phone: str, owner: str = "") -> Payment:
        plan = PLANS.get(plan_id)
        if plan is None:
            raise ValidationError(f'There is no "{plan_id}" plan.')
        number = normalise_phone(phone)

        payment = Payment(
            reference=new_reference(),
            plan=plan.id,
            amount=plan.amount,
            phone=number,
            owner=owner,
            mode=getattr(self.gateway, "mode", "demo"),
            created_at=_stamp(),
            started=_now(),
            next_poll=_now() + FAST_INTERVAL,
            trail=(f"{_stamp()} · created, about to send the prompt",),
        )
        # Written first, on purpose. A crash on the next line must still leave
        # us able to find out what happened to this reference.
        payment = self.store.save(payment)

        try:
            reply = self.gateway.request_to_pay(
                phone=number, amount=plan.amount, reference=payment.reference
            )
        except collecto.GatewayError as error:
            payment = replace(
                payment,
                status=FAILED,
                message=error.message,
                trail=payment.trail + (f"{_stamp()} · {error.message}",),
            )
            return self.store.save(payment)

        if reply.refused:
            # The gateway declined the call outright, so no prompt reached any
            # handset and nothing can arrive later. Leaving it open would chase
            # a payment that does not exist for two minutes and then tell the
            # customer they were too slow to approve a prompt they never got.
            return self.store.save(
                replace(
                    payment,
                    status=FAILED,
                    # Their wording names a key, an IP or a disabled account.
                    # That is an operator's problem, and the trail is where
                    # operators look; the customer gets something they can act on.
                    message=(
                        "We could not start that payment. Nothing was charged, "
                        "so please try again."
                    ),
                    trail=payment.trail
                    + (f"{_stamp()} · requestToPay refused: {reply.message}",),
                )
            )

        note = reply.message or ("prompt sent" if reply.accepted else "no confirmation")
        payment = replace(
            payment,
            transaction_id=reply.transaction_id or payment.transaction_id,
            message=note,
            trail=payment.trail + (f"{_stamp()} · requestToPay → {note}",),
        )
        # Note what is *not* here: the reply's status is not trusted to settle
        # anything. requestToPay returning PENDING is normal, and a 2xx is not
        # a payment. Only the status endpoint moves this out of pending.
        return self.store.save(payment)

    # ── chasing them ─────────────────────────────────────────────────────

    def poll(self, reference: str) -> Payment:
        """Ask about one payment and record what came back."""
        with self._lock:
            payment = self.store.get(reference)
            if payment.settled:
                return payment

            if _now() >= payment.deadline:
                # Nobody touched the prompt. Not a failure on their part, and
                # not a state to leave open forever: they can simply start again.
                return self.store.save(
                    replace(
                        payment,
                        status=ABANDONED,
                        # Not "they did not approve it". Nobody refused
                        # anything - the prompt sat on a handset and nothing
                        # came back, which is the ordinary way this ends. Copy
                        # that implies a decision blames someone for a thing
                        # they did not do.
                        message="The payment did not complete in time. Nothing was charged.",
                        trail=payment.trail
                        + (f"{_stamp()} · gave up after {CHASE_SECONDS // 60} minutes",),
                    )
                )

            try:
                reply = self.gateway.request_to_pay_status(reference=reference)
            except collecto.GatewayError as error:
                # A hard rejection from the gateway on a *read* tells us nothing
                # about the money, so the payment stays open and we try again.
                return self.store.save(
                    replace(
                        payment,
                        checks=payment.checks + 1,
                        next_poll=_now() + SLOW_INTERVAL,
                        trail=payment.trail + (f"{_stamp()} · lookup failed: {error.message}",),
                    )
                )

            return self.store.save(self._apply(payment, reply))

    def _apply(self, payment: Payment, reply: Reply) -> Payment:
        age = _now() - payment.started
        interval = FAST_INTERVAL if age < FAST_WINDOW else SLOW_INTERVAL
        line = f"{_stamp()} · check {payment.checks + 1} → {reply.status}"
        if reply.message:
            line += f" ({reply.message})"

        payment = replace(
            payment,
            checks=payment.checks + 1,
            next_poll=_now() + interval,
            transaction_id=reply.transaction_id or payment.transaction_id,
            message=reply.message or payment.message,
            trail=(payment.trail + (line,))[-40:],
        )

        if reply.status == SUCCESSFUL:
            record = self.subscription.activate(payment)
            settled = replace(
                payment,
                status=SUCCESSFUL,
                message="Payment received.",
                trail=payment.trail
                + (f"{_stamp()} - SUCCESSFUL, {record['plan_name']} active until {record['until']}",),
            )
            if self.on_paid is not None:
                # Never allowed to undo the payment. The money moved; a failure
                # to switch the plan on is a support problem, not a reason to
                # mark a paid subscription unpaid.
                try:
                    self.on_paid(settled)
                except Exception:  # noqa: BLE001
                    pass
            return settled
        if reply.status == FAILED:
            return replace(
                payment,
                status=FAILED,
                message=reply.message or "The payment was not completed.",
            )
        return payment

    def sweep(self) -> int:
        """One pass over everything still open. Returns how many were checked."""
        checked = 0
        for payment in self.store.open_payments():
            if payment.next_poll and _now() < payment.next_poll:
                continue
            try:
                self.poll(payment.reference)
            except Exception:  # noqa: BLE001 - one bad record must not stop the rest
                continue
            checked += 1
        return checked

    # ── the thread ───────────────────────────────────────────────────────

    def start_worker(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name="cissy-payments", daemon=True
        )
        self._thread.start()

    def stop_worker(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.sweep()
            except Exception:  # noqa: BLE001 - the sweeper must never die
                pass
            self._stop.wait(2.0)


# ── helpers ──────────────────────────────────────────────────────────────


def new_reference() -> str:
    """Our reference, and the only key back to a payment.

    Readable enough to quote in a support message and unique enough that two
    payments in the same second cannot collide.
    """
    day = datetime.now(timezone.utc).strftime("%Y%m%d")
    return f"CISSY-{day}-{secrets.token_hex(4).upper()}"


def normalise_phone(raw: str) -> str:
    """To the MSISDN form the gateway wants: 256772000000.

    People type +256, 0772…, and numbers with spaces in. Sending any of those
    through unchanged produces a rejection with a message nobody can act on.
    """
    text = "".join(c for c in str(raw) if c.isdigit() or c == "+").lstrip("+")
    if text.startswith("0"):
        text = "256" + text[1:]
    elif len(text) == 9 and text.startswith("7"):
        # A bare subscriber number. The field asks for exactly these nine digits
        # under a fixed +256, so one that arrives without the code is Ugandan
        # rather than a nine-digit number from somewhere else. Without this it
        # would pass the length check and go to the gateway as a wrong MSISDN.
        text = "256" + text
    if not text.isdigit() or not 9 <= len(text) <= 15:
        raise ValidationError(
            "That does not look like a mobile money number. "
            "It should be nine digits, for example 772 000 000."
        )
    return text
