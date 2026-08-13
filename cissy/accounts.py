"""Users, sessions, phone verification and the build allowance.

This is the one part of the server that is not JSON files on disk, and the
reason is a single line of schema: `phone TEXT NOT NULL UNIQUE`. Two people
signing up on the same number at the same moment is exactly the case a file
layout gets wrong, and the fix in files is a lock you have to remember to take
everywhere. Here the database refuses it. `sqlite3` is standard library, so the
deploy is still `git clone` and `python3 server.py`.

Passwords go through `hashlib.scrypt`, which is also standard library. Session
tokens are stored as SHA-256 digests rather than in the clear, so a copy of the
database is not a set of working logins.

Nothing in here knows about HTTP.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from .config import slugify
from .errors import CissyError, ConflictError, NotFoundError, ValidationError
from .payments import PLANS

# Cost parameters for scrypt. n=16384 costs roughly 16 MB and a few tens of
# milliseconds per hash, which is the right trade on a 4 GB box that will never
# see more than a handful of logins a minute.
SCRYPT_N = 1 << 14
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_LEN = 32

SESSION_DAYS = 30
CODE_MINUTES = 10
CODE_ATTEMPTS = 5

# Every SMS costs real money, so the limits are load-bearing rather than
# decorative. A resend button with no cooldown is a button that drains the
# balance.
RESEND_COOLDOWN_SECONDS = 60
SMS_PER_PHONE_PER_DAY = 5
SMS_PER_IP_PER_HOUR = 10

TRIAL_BUILDS = 3
TRIAL_PLAN = "trial"


def plan_label(plan: str) -> str:
    """What a plan is called on a screen.

    One place, because the alternative is every view deciding on its own what
    "pro" is called and them slowly disagreeing.
    """
    if plan == TRIAL_PLAN:
        return "Free trial"
    known = PLANS.get(plan)
    return known.name if known else plan


class AuthError(CissyError):
    """Not signed in, or signed in as somebody who may not do this."""

    status = 401


class ForbiddenError(CissyError):
    status = 403


class RateLimited(CissyError):
    status = 429


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp(when: datetime | None = None) -> str:
    return (when or _now()).isoformat(timespec="seconds")


def _parse(text: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(text))
    except (TypeError, ValueError):
        return None


# ── passwords ────────────────────────────────────────────────────────────


def hash_password(password: str) -> str:
    """scrypt, with the salt and cost parameters stored alongside the digest.

    Written as one self-describing string so the cost can be raised later
    without invalidating everybody's password: an old hash still says how it
    was made.
    """
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=SCRYPT_LEN,
    )
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${salt.hex()}${digest.hex()}"


def check_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt_hex, digest_hex = str(stored).split("$")
        if scheme != "scrypt":
            return False
        candidate = hashlib.scrypt(
            password.encode("utf-8"),
            salt=bytes.fromhex(salt_hex),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(bytes.fromhex(digest_hex)),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(candidate.hex(), digest_hex)


def _token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# ── the user record ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class User:
    id: str
    name: str
    phone: str
    verified: bool
    is_admin: bool
    plan: str
    builds_used: int
    builds_limit: int
    plan_until: str | None
    created_at: str

    @property
    def builds_left(self) -> int:
        return max(0, self.builds_limit - self.builds_used)

    @property
    def on_trial(self) -> bool:
        return self.plan == TRIAL_PLAN

    @property
    def plan_expired(self) -> bool:
        """A paid plan whose month has run out drops back to trial rules."""
        if self.on_trial or not self.plan_until:
            return False
        until = _parse(self.plan_until)
        return until is not None and until < _now()

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "phone": self.phone,
            "verified": self.verified,
            "is_admin": self.is_admin,
            "plan": self.plan,
            # The id is for the database. Sent alongside it so no screen has to
            # decide for itself that "pro" should be read out as "Business".
            "plan_name": plan_label(self.plan),
            "builds_used": self.builds_used,
            "builds_limit": self.builds_limit,
            "builds_left": self.builds_left,
            "plan_until": self.plan_until,
            "plan_expired": self.plan_expired,
            "created_at": self.created_at,
        }


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    phone         TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    verified      INTEGER NOT NULL DEFAULT 0,
    is_admin      INTEGER NOT NULL DEFAULT 0,
    plan          TEXT NOT NULL DEFAULT 'trial',
    builds_used   INTEGER NOT NULL DEFAULT 0,
    builds_limit  INTEGER NOT NULL DEFAULT 3,
    plan_until    TEXT,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS sessions_user ON sessions (user_id);

CREATE TABLE IF NOT EXISTS codes (
    phone      TEXT PRIMARY KEY,
    code       TEXT NOT NULL,
    purpose    TEXT NOT NULL,
    attempts   INTEGER NOT NULL DEFAULT 0,
    sent_at    TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sms_log (
    phone TEXT NOT NULL,
    ip    TEXT NOT NULL,
    at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS sms_phone ON sms_log (phone);
CREATE INDEX IF NOT EXISTS sms_ip ON sms_log (ip);
"""

_COLUMNS = (
    "id, name, phone, verified, is_admin, plan, builds_used, builds_limit, "
    "plan_until, created_at"
)


def _row_to_user(row: sqlite3.Row | None) -> User | None:
    if row is None:
        return None
    return User(
        id=row["id"],
        name=row["name"],
        phone=row["phone"],
        verified=bool(row["verified"]),
        is_admin=bool(row["is_admin"]),
        plan=row["plan"],
        builds_used=int(row["builds_used"]),
        builds_limit=int(row["builds_limit"]),
        plan_until=row["plan_until"],
        created_at=row["created_at"],
    )


class Accounts:
    """Everything about who somebody is and what they are allowed to build."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # One connection shared across request threads, guarded by a lock. At
        # this scale that is simpler than a pool and removes any question about
        # which thread owns what.
        self._db = sqlite3.connect(str(self.path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._lock = threading.Lock()
        with self._lock:
            self._db.executescript(SCHEMA)
            self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # ── users ────────────────────────────────────────────────────────────

    def count(self) -> int:
        with self._lock:
            return int(self._db.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"])

    def create_user(
        self, *, name: str, phone: str, password: str, is_admin: bool = False,
        verified: bool = False,
    ) -> User:
        name = " ".join(str(name).split())
        if len(name) < 2:
            raise ValidationError("Please give the name you want to be called by.")
        if len(str(password)) < 8:
            raise ValidationError("Choose a password of at least 8 characters.")

        user_id = self._unique_id(name)
        record = (
            user_id,
            name,
            phone,
            hash_password(password),
            int(verified),
            int(is_admin),
            TRIAL_PLAN,
            0,
            TRIAL_BUILDS,
            None,
            _stamp(),
        )
        with self._lock:
            try:
                self._db.execute(
                    "INSERT INTO users (id, name, phone, password_hash, verified, "
                    "is_admin, plan, builds_used, builds_limit, plan_until, created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    record,
                )
            except sqlite3.IntegrityError as error:
                # The UNIQUE constraint is the whole reason this is a database.
                raise ConflictError(
                    "There is already an account on that number. Try logging in, "
                    "or use “Forgot password”."
                ) from error
            self._db.commit()
        found = self.by_id(user_id)
        assert found is not None
        return found

    def _unique_id(self, name: str) -> str:
        """Readable and path-safe, because it becomes a directory name.

        A slug alone would collide between two people called the same thing, so
        a short random tail settles it without a counter to keep.
        """
        base = slugify(name)[:24].strip("-") or "user"
        for _ in range(20):
            candidate = f"{base}-{secrets.token_hex(3)}"
            with self._lock:
                exists = self._db.execute(
                    "SELECT 1 FROM users WHERE id = ?", (candidate,)
                ).fetchone()
            if not exists:
                return candidate
        raise ConflictError("Could not allocate an account id. Try again.")

    def by_id(self, user_id: str) -> User | None:
        with self._lock:
            row = self._db.execute(
                f"SELECT {_COLUMNS} FROM users WHERE id = ?", (user_id,)
            ).fetchone()
        return _row_to_user(row)

    def by_phone(self, phone: str) -> User | None:
        with self._lock:
            row = self._db.execute(
                f"SELECT {_COLUMNS} FROM users WHERE phone = ?", (phone,)
            ).fetchone()
        return _row_to_user(row)

    def list_users(self) -> list[User]:
        with self._lock:
            rows = self._db.execute(
                f"SELECT {_COLUMNS} FROM users ORDER BY created_at DESC"
            ).fetchall()
        return [u for u in (_row_to_user(r) for r in rows) if u]

    def verify_password(self, phone: str, password: str) -> User | None:
        with self._lock:
            row = self._db.execute(
                "SELECT password_hash FROM users WHERE phone = ?", (phone,)
            ).fetchone()
        if row is None:
            # Still spend the time, so a missing account and a wrong password
            # take the same wall-clock. Otherwise the response time answers
            # "does this number have an account?" for anybody who asks.
            hash_password(password)
            return None
        if not check_password(password, row["password_hash"]):
            return None
        return self.by_phone(phone)

    def set_password(self, user_id: str, password: str) -> None:
        if len(str(password)) < 8:
            raise ValidationError("Choose a password of at least 8 characters.")
        with self._lock:
            self._db.execute(
                "UPDATE users SET password_hash = ? WHERE id = ?",
                (hash_password(password), user_id),
            )
            self._db.commit()

    def mark_verified(self, user_id: str) -> None:
        with self._lock:
            self._db.execute("UPDATE users SET verified = 1 WHERE id = ?", (user_id,))
            self._db.commit()

    def set_admin(self, user_id: str, is_admin: bool = True) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE users SET is_admin = ? WHERE id = ?", (int(is_admin), user_id)
            )
            self._db.commit()

    # ── the build allowance ──────────────────────────────────────────────

    def spend_build(self, user_id: str) -> User:
        """Take one build off the allowance, or refuse.

        Deliberately a single statement with the check in the WHERE clause. Read
        the count, decide, then write would let two builds started at the same
        moment both pass the check and both spend the last one.
        """
        with self._lock:
            cursor = self._db.execute(
                "UPDATE users SET builds_used = builds_used + 1 "
                "WHERE id = ? AND builds_used < builds_limit",
                (user_id,),
            )
            self._db.commit()
            spent = cursor.rowcount
        user = self.by_id(user_id)
        if user is None:
            raise NotFoundError("That account no longer exists.")
        if not spent:
            raise ForbiddenError(
                "You have used all the builds on your plan. "
                "Everything you have already built stays downloadable."
            )
        return user

    def refund_build(self, user_id: str) -> None:
        """Give one back when a build never actually ran.

        A build refused by the toolchain or killed before it started cost the
        customer nothing, so charging for it is simply wrong.
        """
        with self._lock:
            self._db.execute(
                "UPDATE users SET builds_used = MAX(0, builds_used - 1) WHERE id = ?",
                (user_id,),
            )
            self._db.commit()

    def activate_plan(self, user_id: str, *, plan: str, builds: int, until: str) -> User:
        """A payment cleared. Reset the allowance and start the month.

        The counter goes back to zero rather than the limit going up, so a month
        is a month rather than an ever-growing balance.
        """
        with self._lock:
            self._db.execute(
                "UPDATE users SET plan = ?, builds_limit = ?, builds_used = 0, "
                "plan_until = ? WHERE id = ?",
                (plan, int(builds), until, user_id),
            )
            self._db.commit()
        user = self.by_id(user_id)
        if user is None:
            raise NotFoundError("That account no longer exists.")
        return user

    def grant_builds(self, user_id: str, extra: int) -> User:
        """Admin's escape hatch for when a payment fails at midnight."""
        with self._lock:
            self._db.execute(
                "UPDATE users SET builds_limit = builds_limit + ? WHERE id = ?",
                (int(extra), user_id),
            )
            self._db.commit()
        user = self.by_id(user_id)
        if user is None:
            raise NotFoundError("That account no longer exists.")
        return user

    # ── sessions ─────────────────────────────────────────────────────────

    def open_session(self, user_id: str) -> tuple[str, datetime]:
        """Returns the raw token. Only its digest is kept."""
        token = secrets.token_urlsafe(32)
        expires = _now() + timedelta(days=SESSION_DAYS)
        with self._lock:
            self._db.execute(
                "INSERT INTO sessions (token_hash, user_id, created_at, expires_at) "
                "VALUES (?,?,?,?)",
                (_token_digest(token), user_id, _stamp(), _stamp(expires)),
            )
            self._db.commit()
        return token, expires

    def session_user(self, token: str) -> User | None:
        if not token:
            return None
        with self._lock:
            row = self._db.execute(
                "SELECT user_id, expires_at FROM sessions WHERE token_hash = ?",
                (_token_digest(token),),
            ).fetchone()
        if row is None:
            return None
        expires = _parse(row["expires_at"])
        if expires is None or expires < _now():
            self.close_session(token)
            return None
        return self.by_id(row["user_id"])

    def close_session(self, token: str) -> None:
        with self._lock:
            self._db.execute(
                "DELETE FROM sessions WHERE token_hash = ?", (_token_digest(token),)
            )
            self._db.commit()

    def close_all_sessions(self, user_id: str) -> None:
        """Used after a password change, which should log other devices out."""
        with self._lock:
            self._db.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
            self._db.commit()

    def purge_expired(self) -> int:
        with self._lock:
            cursor = self._db.execute(
                "DELETE FROM sessions WHERE expires_at < ?", (_stamp(),)
            )
            self._db.commit()
            return cursor.rowcount

    # ── verification codes ───────────────────────────────────────────────

    def issue_code(self, phone: str, *, purpose: str, ip: str) -> str:
        """Mint a 6-digit code, after checking this is not being abused."""
        self._check_sms_limits(phone, ip)
        code = f"{secrets.randbelow(1_000_000):06d}"
        expires = _now() + timedelta(minutes=CODE_MINUTES)
        with self._lock:
            self._db.execute(
                "INSERT INTO codes (phone, code, purpose, attempts, sent_at, expires_at) "
                "VALUES (?,?,?,0,?,?) "
                "ON CONFLICT(phone) DO UPDATE SET code=excluded.code, "
                "purpose=excluded.purpose, attempts=0, sent_at=excluded.sent_at, "
                "expires_at=excluded.expires_at",
                (phone, code, purpose, _stamp(), _stamp(expires)),
            )
            self._db.execute(
                "INSERT INTO sms_log (phone, ip, at) VALUES (?,?,?)",
                (phone, ip or "unknown", _stamp()),
            )
            self._db.commit()
        return code

    def check_code(self, phone: str, code: str, *, purpose: str) -> bool:
        with self._lock:
            row = self._db.execute(
                "SELECT code, purpose, attempts, expires_at FROM codes WHERE phone = ?",
                (phone,),
            ).fetchone()
        if row is None:
            raise ValidationError("Ask for a code first.")
        if row["purpose"] != purpose:
            raise ValidationError("That code was for something else. Ask for a new one.")

        expires = _parse(row["expires_at"])
        if expires is None or expires < _now():
            self.clear_code(phone)
            raise ValidationError("That code has expired. Ask for a new one.")
        if int(row["attempts"]) >= CODE_ATTEMPTS:
            self.clear_code(phone)
            raise RateLimited("Too many wrong codes. Ask for a new one.")

        if not hmac.compare_digest(str(row["code"]), str(code).strip()):
            with self._lock:
                self._db.execute(
                    "UPDATE codes SET attempts = attempts + 1 WHERE phone = ?", (phone,)
                )
                self._db.commit()
            return False

        self.clear_code(phone)
        return True

    def clear_code(self, phone: str) -> None:
        with self._lock:
            self._db.execute("DELETE FROM codes WHERE phone = ?", (phone,))
            self._db.commit()

    def seconds_until_resend(self, phone: str) -> int:
        with self._lock:
            row = self._db.execute(
                "SELECT sent_at FROM codes WHERE phone = ?", (phone,)
            ).fetchone()
        if row is None:
            return 0
        sent = _parse(row["sent_at"])
        if sent is None:
            return 0
        waited = (_now() - sent).total_seconds()
        return max(0, int(RESEND_COOLDOWN_SECONDS - waited))

    def _check_sms_limits(self, phone: str, ip: str) -> None:
        left = self.seconds_until_resend(phone)
        if left:
            raise RateLimited(f"Wait {left} more seconds before asking for another code.")

        day_ago = _stamp(_now() - timedelta(days=1))
        hour_ago = _stamp(_now() - timedelta(hours=1))
        with self._lock:
            per_phone = self._db.execute(
                "SELECT COUNT(*) AS n FROM sms_log WHERE phone = ? AND at > ?",
                (phone, day_ago),
            ).fetchone()["n"]
            per_ip = self._db.execute(
                "SELECT COUNT(*) AS n FROM sms_log WHERE ip = ? AND at > ?",
                (ip or "unknown", hour_ago),
            ).fetchone()["n"]

        if per_phone >= SMS_PER_PHONE_PER_DAY:
            raise RateLimited(
                "That number has had too many codes today. Try again tomorrow."
            )
        if per_ip >= SMS_PER_IP_PER_HOUR:
            raise RateLimited("Too many codes requested from here. Try again later.")

    def sms_sent_today(self) -> int:
        day_ago = _stamp(_now() - timedelta(days=1))
        with self._lock:
            return int(
                self._db.execute(
                    "SELECT COUNT(*) AS n FROM sms_log WHERE at > ?", (day_ago,)
                ).fetchone()["n"]
            )
