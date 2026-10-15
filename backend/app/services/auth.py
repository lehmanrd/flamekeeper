"""Admin login: password hashing, the admin account, sessions and login throttling.

v1 has a single admin account. The installer creates it from the terminal (see
app.cli set-admin), so a fresh install is never left unclaimed. Without the installer,
the first visitor can create it in the browser, but only within SETUP_WINDOW_S of the
controller starting: an install nobody set up doesn't stay open to whoever finds it
(e.g. a bot scanning a rented server). After that, restart FlameKeeper or use the CLI.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import re
import secrets
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import delete, func, select

from app.db import session_scope
from app.models import AuthSession, User

log = logging.getLogger("flamekeeper")

SESSION_DAYS = 30
COOKIE_NAME = "fk_session"
MIN_PASSWORD = 8
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.@-]{1,64}$")
# Login throttling, per client address: this many failures within the window, then wait.
MAX_FAILURES = 5
FAILURE_WINDOW_S = 300.0
SETUP_WINDOW_S = 600.0
_SCRYPT = {"n": 2**14, "r": 8, "p": 1}


class AuthError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, dklen=32, **_SCRYPT)
    b64 = base64.b64encode
    return (f"scrypt${_SCRYPT['n']}${_SCRYPT['r']}${_SCRYPT['p']}$"
            f"{b64(salt).decode()}${b64(digest).decode()}")


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = base64.b64decode(digest)
        actual = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt),
                                n=int(n), r=int(r), p=int(p), dklen=len(expected))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def check_password(password: str) -> None:
    if len(password) < MIN_PASSWORD:
        raise AuthError(f"Use at least {MIN_PASSWORD} characters for the password.", 422)
    if len(password) > 1024:
        raise AuthError("That password is too long.", 422)


@dataclass
class CurrentUser:
    id: str
    username: str


class AuthService:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.started = time.monotonic()
        self._failures: dict[str, list[float]] = {}

    # -- setup ----------------------------------------------------------------------

    def setup_required(self) -> bool:
        with session_scope() as s:
            return not s.scalar(select(func.count()).select_from(User))

    def setup_open(self) -> bool:
        """True while the browser may create the admin (no account yet, recent start)."""
        return self.setup_required() and time.monotonic() - self.started < SETUP_WINDOW_S

    def log_setup_hint(self) -> None:
        # Installs from 0.2.0 kept a setup code here; it's no longer used.
        (self.data_dir / "setup-code").unlink(missing_ok=True)
        if self.setup_required():
            log.warning("FIRST SETUP: no admin account yet. Open FlameKeeper in a browser "
                        "within %d minutes to create it, or run: flamekeeper reset-admin",
                        SETUP_WINDOW_S // 60)

    def create_admin(self, username: str, password: str) -> CurrentUser:
        """First setup in the browser."""
        if not self.setup_required():
            raise AuthError("FlameKeeper is already set up. Sign in instead.", 409)
        if not self.setup_open():
            raise AuthError("Setup is locked because nobody created the admin account within "
                            f"{int(SETUP_WINDOW_S // 60)} minutes of FlameKeeper starting. Run "
                            "`sudo flamekeeper restart` and reload this page, or create the "
                            "account with `sudo flamekeeper reset-admin`.", 403)
        return self._add_admin(username, password)

    def set_admin(self, username: str, password: str) -> CurrentUser:
        """From the terminal (installer, reset-admin): replace any admin account and sign
        everyone out."""
        self._check(username, password)
        with session_scope() as s:
            s.execute(delete(AuthSession))
            s.execute(delete(User))
        return self._add_admin(username, password)

    @staticmethod
    def _check(username: str, password: str) -> str:
        username = username.strip()
        if not USERNAME_RE.match(username):
            raise AuthError("Username: letters, digits and . _ @ - (up to 64).", 422)
        check_password(password)
        return username

    def _add_admin(self, username: str, password: str) -> CurrentUser:
        username = self._check(username, password)
        with session_scope() as s:
            user = User(username=username, password_hash=hash_password(password))
            s.add(user)
            s.flush()
            out = CurrentUser(user.id, user.username)
        log.info("admin account %r created", username)
        return out

    # -- login ----------------------------------------------------------------------

    def throttled(self, client: str) -> float:
        """Seconds until this client may try again (0 = now)."""
        now = time.monotonic()
        recent = [t for t in self._failures.get(client, []) if now - t < FAILURE_WINDOW_S]
        self._failures[client] = recent
        if len(recent) < MAX_FAILURES:
            return 0.0
        return FAILURE_WINDOW_S - (now - recent[0])

    def login(self, username: str, password: str, client: str) -> CurrentUser:
        if wait := self.throttled(client):
            raise AuthError(f"Too many failed sign-ins. Try again in {int(wait) + 1} s.", 429)
        with session_scope() as s:
            user = s.scalar(select(User).where(User.username == username.strip()))
            ok = user is not None and verify_password(password, user.password_hash)
            if not ok and user is None:
                verify_password(password, hash_password("timing"))  # same cost either way
            out = CurrentUser(user.id, user.username) if ok and user else None
        if out is None:
            self._failures.setdefault(client, []).append(time.monotonic())
            raise AuthError("Wrong username or password.", 401)
        self._failures.pop(client, None)
        return out

    def change_password(self, user: CurrentUser, current: str, new: str) -> None:
        check_password(new)
        with session_scope() as s:
            row = s.get(User, user.id)
            if row is None or not verify_password(current, row.password_hash):
                raise AuthError("The current password isn't right.", 403)
            row.password_hash = hash_password(new)

    # -- sessions -------------------------------------------------------------------

    def start_session(self, user: CurrentUser) -> str:
        token = secrets.token_urlsafe(32)
        now = datetime.now(UTC)
        with session_scope() as s:
            s.execute(delete(AuthSession).where(AuthSession.expires_at < now))
            s.add(AuthSession(token_hash=_token_hash(token), user_id=user.id, created_at=now,
                              expires_at=now + timedelta(days=SESSION_DAYS)))
        return token

    def session_user(self, token: str | None) -> CurrentUser | None:
        """The user a cookie belongs to; extends the session while it's in use."""
        if not token:
            return None
        now = datetime.now(UTC)
        with session_scope() as s:
            sess = s.get(AuthSession, _token_hash(token))
            if sess is None:
                return None
            if _aware(sess.expires_at) <= now:
                s.delete(sess)
                return None
            if _aware(sess.expires_at) - now < timedelta(days=SESSION_DAYS - 1):
                sess.expires_at = now + timedelta(days=SESSION_DAYS)  # sliding expiry
            user = s.get(User, sess.user_id)
            return CurrentUser(user.id, user.username) if user else None

    def end_session(self, token: str | None) -> None:
        if token:
            with session_scope() as s:
                s.execute(delete(AuthSession).where(AuthSession.token_hash == _token_hash(token)))

    def end_other_sessions(self, user: CurrentUser, keep: str | None) -> None:
        keep_hash = _token_hash(keep) if keep else ""
        with session_scope() as s:
            s.execute(delete(AuthSession).where(AuthSession.user_id == user.id,
                                                AuthSession.token_hash != keep_hash))
