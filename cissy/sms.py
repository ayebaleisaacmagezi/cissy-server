"""Sending a text, and pretending to when there is no Collecto key yet.

`sendSingleSMS` sits on the same Collecto account and the same `x-api-key` as
the payments, so this reuses `collecto.CollectoGateway`'s transport rather than
opening a second way of talking to them. The demo/live switch is the one that
already exists: no key, no live sends.

An SMS is not repeatable. Sending it twice sends two texts and spends twice the
balance, so a timeout is reported as unknown rather than retried, exactly as
`requestToPay` is.

`DemoSms` keeps the last few messages in memory and writes each one to the log,
because a verification code nobody can read is a signup nobody can finish.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any, Callable

from .collecto import CollectoGateway, Settings


@dataclass(frozen=True)
class Sent:
    ok: bool
    message: str = ""
    transaction_id: str = ""


class SmsSender:
    """The real thing."""

    mode = "live"

    def __init__(self, settings: Settings) -> None:
        self._gateway = CollectoGateway(settings)

    def send(self, *, phone: str, message: str, reference: str) -> Sent:
        reply = self._gateway.send_sms(
            phone=phone, message=message, reference=reference
        )
        # `accepted` is `data.sms` from their reply. There is no delivery
        # status to poll for, so this is as far as certainty goes.
        return Sent(
            ok=reply.accepted,
            message=reply.message,
            transaction_id=reply.transaction_id,
        )


class DemoSms:
    """Prints instead of sending, and remembers what it printed.

    The code has to be reachable or nobody can complete a signup, so it goes
    three places: the server log, an in-memory list the admin screen can read,
    and the signup response itself. That last one is why `is_demo` is surfaced
    all the way to the browser: a live server must never do it.
    """

    mode = "demo"

    def __init__(self, on_log: Callable[[str], None] | None = None, keep: int = 50) -> None:
        self._log = on_log or (lambda line: print(line, flush=True))
        self._keep = keep
        self._sent: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    def send(self, *, phone: str, message: str, reference: str) -> Sent:
        with self._lock:
            self._sent.insert(0, {"phone": phone, "message": message, "reference": reference})
            del self._sent[self._keep :]
        self._log(f"[sms demo] to {phone}: {message}")
        return Sent(ok=True, message="Simulated. Nothing was sent.", transaction_id=reference)

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._sent[:limit])


def build_sender(settings: Settings | None = None) -> Any:
    """Live only when Collecto is fully configured.

    Same rule as the payment gateway: default to the simulator rather than a
    half-configured client, so a missing key fails at startup instead of in the
    middle of somebody's signup.
    """
    resolved = settings or Settings.from_env()
    return SmsSender(resolved) if resolved.live else DemoSms()


def verification_message(code: str, minutes: int, app_name: str = "CissyWeb2App") -> str:
    return f"Your {app_name} verification code is {code}. It expires in {minutes} minutes."


def reset_message(code: str, minutes: int, app_name: str = "CissyWeb2App") -> str:
    """Worded apart from the signup code on purpose.

    A reset code can arrive at somebody who never asked for one, which is the
    first sign of an attempt on their account. The text has to say what it is
    for and that ignoring it is safe, otherwise a bare six digits reads as an
    instruction to type them in somewhere.
    """
    return (
        f"Your {app_name} password reset code is {code}. It expires in "
        f"{minutes} minutes. If you did not ask to reset your password, "
        f"ignore this message - your account is unchanged."
    )
