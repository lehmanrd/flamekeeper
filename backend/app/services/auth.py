"""Admin login: password hashing, the first-setup code, sessions and login throttling.

v1 has a single admin account. Until it exists, FlameKeeper is in setup mode: the only
thing the UI can do is create the account, and that needs a one-time setup code written
to `<data dir>/setup-code` (and logged). The code proves the person setting it up can
see the host, so nobody else on the network can claim the install first.
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
# No 0/O/1/I/L: the code is read off a terminal and typed into a browser.
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
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
        self.code_file = data_dir / "setup-code"
        self._failures: dict[str, list[float]] = {}

    # -- setup ----------------------------------------------------------------------

    def setup_required(self) -> bool:
        with session_scope() as s:
            return not s.scalar(select(func.count()).select_from(User))

    def ensure_setup_code(self) -> str | None:
        """In setup mode, make sure a setup code exists and log it. Returns the code."""
        if not self.setup_required():
            self.code_file.unlink(missing_ok=True)
            return None
        code = self._read_code()
        if code is None:
            code = self.new_setup_code()
        log.warning("FIRST SETUP: open FlameKeeper in a browser and create the admin "
                    "account with setup code %s", code)
        return code

    def new_setup_code(self) -> str:
        raw = "".join(secrets.choice(CODE_ALPHABET) for _ in range(8))
        code = f"{raw[:4]}-{raw[4:]}"
        self.code_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.code_file.with_suffix(".tmp")
        tmp.write_text(code + "\n")
        tmp.chmod(0o600)
        tmp.replace(self.code_file)
        return code

    def _read_code(self) -> str | None:
        try:
            code = self.code_file.read_text().strip()
        except FileNotFoundError:
            return None
        return code or None

    def create_admin(self, setup_code: str, username: str, password: str) -> CurrentUser:
        if not self.setup_required():
            raise AuthError("FlameKeeper is already set up. Sign in instead.", 409)
        expected = self._read_code()
        given = setup_code.strip().upper().replace(" ", "")
        if len(given) == 8:
            given = f"{given[:4]}-{given[4:]}"
        if expected is None or not hmac.compare_digest(given, expected):
            raise AuthError("That setup code isn't right. Run `flamekeeper setup-code` on "
                            "the server to see it.", 403)
        username = username.strip()
        if not USERNAME_RE.match(username):
            raise AuthError("Username: letters, digits and . _ @ - (up to 64).", 422)
        check_password(password)
        with session_scope() as s:
            user = User(username=username, password_hash=hash_password(password))
            s.add(user)
            s.flush()
            out = CurrentUser(user.id, user.username)
        self.code_file.unlink(missing_ok=True)
        log.info("admin account %r created", username)
        return out

    def reset(self) -> str:
        """Remove every account and session (forgotten password); back to setup mode."""
        with session_scope() as s:
            s.execute(delete(AuthSession))
            s.execute(delete(User))
        return self.new_setup_code()

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
