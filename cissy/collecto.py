"""Talking to Collecto, and pretending to when there is no account yet.

Collecto is not shaped like Stripe. There is no hosted checkout page, no
redirect, and - this is the one that changes the design - **no webhook**. Every
outcome is something we go and ask for:

    requestToPay        push a PIN prompt to the customer's handset
    requestToPayStatus  ask, with the same reference, whether they approved

So nothing calls us back. Something on our side has to keep asking after the
browser has gone, which is what `payments.py` does with the reply objects here.

Two rules from their reference that are easy to get wrong and expensive to get
wrong:

* A 2xx is not a payment. `data.requestToPay: true` only means the call was
  accepted. The money word is `data.status`, and only `SUCCESSFUL` is money.
* A timeout, a dropped connection or a non-JSON body means *unknown*, not
  failed. Calling it failed keeps someone's money and gives them nothing.

`DemoGateway` implements the same two calls in memory so the whole flow can be
built and shown before an API key exists. It is deliberately not a stub that
always succeeds - it can decline, stall, flake and return rubbish, because those
are the paths worth seeing before real money is involved.
"""

from __future__ import annotations

import json
import os
import secrets
import socket
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import CissyError, ValidationError

DEFAULT_BASE_URL = "https://collecto.cissytech.com/api"
USER_AGENT = "cissy-build/1.0"
MAX_ATTEMPTS = 3

# Status words, normalised. Their docs say collections also answer with
# success/completed/confirmed, so the set is wider than the three documented
# constants.
_SUCCESS = {"successful", "success", "completed", "confirmed", "paid"}
_FAILED = {"failed", "failure", "declined", "cancelled", "canceled", "rejected"}

PENDING = "pending"
SUCCESSFUL = "successful"
FAILED = "failed"


class GatewayError(CissyError):
    """The gateway said no in a way that will not fix itself.

    Only raised for the non-transient cases - a bad request body, a rejected
    key. Anything that might succeed on a later attempt comes back as a pending
    reply instead, because that is the answer that keeps polling alive.
    """

    status = 502


@dataclass(frozen=True)
class Reply:
    """One answer from the gateway, already reduced to what we act on."""

    accepted: bool
    """Whether the call itself worked - `data.requestToPay` and friends."""

    status: str
    """One of pending / successful / failed. Never anything else."""

    message: str = ""
    transaction_id: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def settled(self) -> bool:
        return self.status in (SUCCESSFUL, FAILED)


@dataclass(frozen=True)
class Settings:
    """Everything needed to reach a real Collecto account.

    `referer` is not decoration. Their WAF rejects any request without both a
    Referer and a User-Agent, and urllib sends neither by default - a very dull
    afternoon if you meet it without warning.
    """

    username: str = ""
    api_key: str = ""
    base_url: str = DEFAULT_BASE_URL
    referer: str = "https://web2app.cissytech.com"
    user_agent: str = USER_AGENT
    timeout: float = 20.0

    @property
    def live(self) -> bool:
        return bool(self.username and self.api_key)

    @classmethod
    def from_env(cls, env: Any = None) -> "Settings":
        source = os.environ if env is None else env
        return cls(
            username=source.get("CISSY_COLLECTO_USERNAME", "").strip(),
            api_key=source.get("CISSY_COLLECTO_KEY", "").strip(),
            base_url=source.get("CISSY_COLLECTO_URL", DEFAULT_BASE_URL).rstrip("/"),
            referer=source.get(
                "CISSY_COLLECTO_REFERER", "https://web2app.cissytech.com"
            ),
        )


# ── the real thing ───────────────────────────────────────────────────────


class CollectoGateway:
    """HTTP client for the two collection endpoints.

    Retries are split by whether the call is safe to repeat. A status lookup is
    a read, so it retries freely. `requestToPay` moves money and pushes a prompt
    to somebody's phone: retrying it after a timeout risks two prompts and two
    debits for one subscription, so it retries only when the connection was
    refused outright - which proves the request never arrived.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.mode = "live"

    def request_to_pay(self, *, phone: str, amount: int, reference: str) -> Reply:
        return self._post(
            "requestToPay",
            {
                "phone": phone,
                "amount": amount,
                "reference": reference,
                "paymentOption": "mobilemoney",
            },
            flag="requestToPay",
            repeatable=False,
        )

    def send_sms(self, *, phone: str, message: str, reference: str) -> Reply:
        """One text. Not repeatable, because a retry sends a second message.

        Their reply carries no status word to poll, only the `data.sms` flag
        saying the send was accepted, so `Reply.status` here stays pending and
        means nothing. Read `accepted`.
        """
        return self._post(
            "sendSingleSMS",
            {"phone": phone, "message": message, "reference": reference},
            flag="sms",
            repeatable=False,
        )

    def request_to_pay_status(self, *, reference: str) -> Reply:
        # Their field is called transactionId, but the reference is what goes in
        # it - the docs are explicit that it is "the reference you passed to
        # requestToPay", not the PMT… id that came back.
        return self._post(
            "requestToPayStatus",
            {"transactionId": reference},
            flag="requestToPayStatus",
            repeatable=True,
        )

    # ── transport ────────────────────────────────────────────────────────

    def _post(
        self, endpoint: str, payload: dict[str, Any], *, flag: str, repeatable: bool
    ) -> Reply:
        settings = self.settings
        if not settings.live:
            raise GatewayError(
                "Collecto is not configured. Set CISSY_COLLECTO_USERNAME and "
                "CISSY_COLLECTO_KEY, or run in demo mode."
            )

        url = f"{settings.base_url}/{settings.username}/{endpoint}"
        body = json.dumps(payload).encode("utf-8")
        last_note = ""

        for attempt in range(MAX_ATTEMPTS):
            try:
                text = self._send(url, body)
            except _Unreached as error:
                # Refused or unresolvable: nothing was delivered, so repeating
                # is safe even for a payment.
                last_note = str(error)
                if attempt + 1 < MAX_ATTEMPTS:
                    time.sleep(_backoff(attempt))
                    continue
                break
            except _Transient as error:
                # Might have been processed. Only re-ask if asking is harmless.
                last_note = str(error)
                if repeatable and attempt + 1 < MAX_ATTEMPTS:
                    time.sleep(_backoff(attempt))
                    continue
                break
            except _Rejected as error:
                raise GatewayError(
                    f"Collecto rejected the request ({error.status}).",
                    detail=error.body[:400],
                ) from error

            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                # Documented case: treat as pending and look it up, never as a
                # failure and never by resending.
                return Reply(
                    accepted=False,
                    status=PENDING,
                    message="Collecto returned something that was not JSON.",
                )
            if not isinstance(data, dict):
                return Reply(accepted=False, status=PENDING, message="Odd reply shape.")
            return _read(data, flag)

        return Reply(
            accepted=False,
            status=PENDING,
            message=f"Could not reach Collecto ({last_note}). Will keep checking.",
        )

    def _send(self, url: str, body: bytes) -> str:
        settings = self.settings
        request = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "x-api-key": settings.api_key,
                # Both required on every call - their gateway drops requests
                # missing either one before any handler sees them.
                "Referer": settings.referer,
                "User-Agent": settings.user_agent,
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=settings.timeout) as response:
                return response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as error:
            text = error.read().decode("utf-8", "replace")
            if error.code == 429 or error.code >= 500:
                raise _Transient(f"HTTP {error.code}") from error
            raise _Rejected(error.code, text) from error
        except urllib.error.URLError as error:
            reason = error.reason
            if isinstance(reason, (ConnectionRefusedError, socket.gaierror)):
                raise _Unreached(str(reason)) from error
            raise _Transient(str(reason)) from error
        except (TimeoutError, socket.timeout) as error:
            raise _Transient("timed out") from error


def _read(data: dict[str, Any], flag: str) -> Reply:
    """Pull the outcome out of a collections reply.

    Collections nest under `data`; the flag next to it says whether the call was
    accepted, and `data.status` says what happened to the money. Those two are
    separate answers and conflating them is the classic way to mark an unpaid
    subscription paid.
    """
    inner = data.get("data")
    inner = inner if isinstance(inner, dict) else {}
    word = str(inner.get("status") or data.get("status") or "").strip().lower()
    return Reply(
        accepted=bool(inner.get(flag, False)),
        status=normalise_status(word),
        message=str(inner.get("message") or data.get("status_message") or ""),
        transaction_id=str(inner.get("transactionId") or ""),
        raw=data,
    )


def normalise_status(word: Any) -> str:
    """Reduce a gateway status word to one of three.

    Unknown words become pending rather than failed, on purpose: an unrecognised
    string is missing information, and the safe reading of missing information
    about a payment is "ask again".
    """
    text = str(word or "").strip().lower()
    if text in _SUCCESS:
        return SUCCESSFUL
    if text in _FAILED:
        return FAILED
    return PENDING


def _backoff(attempt: int) -> float:
    return min(4.0, 0.5 * (2**attempt))


class _Transient(Exception):
    """Might have been processed - do not repeat anything that moves money."""


class _Unreached(Exception):
    """Provably never delivered."""


class _Rejected(Exception):
    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"HTTP {status}")
        self.status = status
        self.body = body


# ── the stand-in ─────────────────────────────────────────────────────────


class DemoGateway:
    """A Collecto that lives in this process, for building against with no key.

    It answers the same two calls with the same shapes and - more usefully - the
    same bad behaviour: a prompt nobody answers, a decline, a connection that
    drops, a body that is not JSON. Building only against the happy path is how
    you discover on launch day that an unanswered prompt locks an account
    forever.

    Prompts are written to disk rather than held in memory so that restarting
    the server mid-payment demonstrates the thing that actually matters: the
    poller picking the payment back up and finishing it.
    """

    # Scenario is chosen by the demo caller, not guessed from the number, so a
    # demo can be driven deliberately.
    SCENARIOS = ("approve", "decline", "silent", "flaky", "garbage")

    def __init__(self, directory: Path) -> None:
        self.mode = "demo"
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    # -- gateway surface ----------------------------------------------------

    def request_to_pay(self, *, phone: str, amount: int, reference: str) -> Reply:
        with self._lock:
            prompt = self._read(reference)
            if prompt is None:
                prompt = {
                    "reference": reference,
                    "phone": phone,
                    "amount": amount,
                    "status": PENDING,
                    "scenario": "approve",
                    "created_at": time.time(),
                    "faults": 0,
                    "transaction_id": "DEMO" + secrets.token_hex(3).upper(),
                }
                self._write(prompt)
        return Reply(
            accepted=True,
            status=PENDING,
            message="Request to pay sent successfully",
            transaction_id=prompt["transaction_id"],
        )

    def request_to_pay_status(self, *, reference: str) -> Reply:
        with self._lock:
            prompt = self._read(reference)
            if prompt is None:
                return Reply(accepted=False, status=PENDING, message="Unknown reference")

            scenario = prompt.get("scenario", "approve")
            faults = int(prompt.get("faults", 0))

            # The two ugly transports, each fired once so a demo shows the
            # recovery rather than getting stuck in it.
            if scenario == "flaky" and faults < 1:
                prompt["faults"] = faults + 1
                self._write(prompt)
                return Reply(
                    accepted=False,
                    status=PENDING,
                    message="Could not reach Collecto (simulated drop). Will keep checking.",
                )
            if scenario == "garbage" and faults < 1:
                prompt["faults"] = faults + 1
                self._write(prompt)
                return Reply(
                    accepted=False,
                    status=PENDING,
                    message="Collecto returned something that was not JSON.",
                )

            status = str(prompt.get("status", PENDING))
            return Reply(
                accepted=True,
                status=normalise_status(status),
                message=prompt.get("message", ""),
                transaction_id=prompt.get("transaction_id", ""),
            )

    # -- the pretend handset -----------------------------------------------

    def prompts(self) -> list[dict[str, Any]]:
        found = []
        for path in sorted(self.directory.glob("*.json")):
            try:
                found.append(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                continue
        found.sort(key=lambda p: p.get("created_at", 0), reverse=True)
        return found

    def prompt(self, reference: str) -> dict[str, Any] | None:
        with self._lock:
            return self._read(reference)

    def act(self, reference: str, action: str) -> dict[str, Any]:
        """Approve or decline on the customer's behalf.

        This is the demo's whole point: it stands in for the moment someone taps
        their PIN, so the poll loop can be watched doing its job.
        """
        with self._lock:
            prompt = self._read(reference)
            if prompt is None:
                raise ValidationError("No demo prompt with that reference.")
            if action == "approve":
                prompt["status"] = SUCCESSFUL
                prompt["message"] = "Payment approved on the handset"
            elif action == "decline":
                prompt["status"] = FAILED
                prompt["message"] = "Declined on the handset"
            else:
                raise ValidationError('Action must be "approve" or "decline".')
            self._write(prompt)
            return prompt

    def set_scenario(self, reference: str, scenario: str) -> None:
        if scenario not in self.SCENARIOS:
            raise ValidationError(f'"{scenario}" is not a demo scenario.')
        with self._lock:
            prompt = self._read(reference)
            if prompt is None:
                return
            prompt["scenario"] = scenario
            self._write(prompt)

    def forget(self, reference: str) -> None:
        path = self._path(reference)
        if path.is_file():
            path.unlink()

    # -- disk ---------------------------------------------------------------

    def _path(self, reference: str) -> Path:
        # References are generated by us and are hex plus dashes, but this is
        # reached from a URL, so it is checked rather than trusted.
        safe = "".join(c for c in reference if c.isalnum() or c in "-_")
        if not safe or safe != reference:
            raise ValidationError("That is not a valid payment reference.")
        return self.directory / f"{safe}.json"

    def _read(self, reference: str) -> dict[str, Any] | None:
        path = self._path(reference)
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def _write(self, prompt: dict[str, Any]) -> None:
        path = self._path(prompt["reference"])
        path.write_text(json.dumps(prompt, indent=2), encoding="utf-8")
