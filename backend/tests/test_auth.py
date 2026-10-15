"""Admin login: first setup, the setup window, sessions, throttling, CSRF header, CLI."""

from __future__ import annotations

import time

from fastapi.testclient import TestClient

from app.config import Settings
from app.services.auth import SETUP_WINDOW_S, hash_password, verify_password
from tests.conftest import ADMIN


def test_password_hashing() -> None:
    stored = hash_password("correct-horse-battery")
    assert stored.startswith("scrypt$") and "correct" not in stored
    assert verify_password("correct-horse-battery", stored)
    assert not verify_password("wrong", stored)
    assert not verify_password("x", "not-a-hash")


def test_first_setup_in_the_browser(anon: TestClient, settings: Settings) -> None:
    assert anon.get("/api/auth/status").json() == {"setup_required": True,
                                                   "setup_locked": False, "user": None}
    assert anon.get("/api/health").status_code == 200  # public
    res = anon.get("/api/worlds")
    assert res.status_code == 401 and res.json()["detail"] == "Set up FlameKeeper first"
    assert anon.post("/api/auth/login", json=ADMIN).status_code == 409
    assert anon.post("/api/auth/setup", json={**ADMIN, "password": "short"}).status_code == 422
    assert anon.post("/api/auth/setup", json={**ADMIN, "username": "no spaces"}
                     ).status_code == 422

    res = anon.post("/api/auth/setup", json=ADMIN)
    assert res.status_code == 200 and res.json() == {"user": {"username": "admin"}}
    cookie = res.headers["set-cookie"]
    assert "fk_session=" in cookie and "HttpOnly" in cookie and "samesite=lax" in cookie.lower()
    assert anon.get("/api/worlds").status_code == 200
    assert anon.get("/api/auth/status").json()["user"] == {"username": "admin"}
    assert anon.post("/api/auth/setup", json=ADMIN).status_code == 409


def test_setup_locks_after_the_window(anon: TestClient) -> None:
    """An install nobody set up doesn't stay open to whoever finds it."""
    auth = anon.app.state.services.auth
    auth.started -= SETUP_WINDOW_S + 1
    assert anon.get("/api/auth/status").json()["setup_locked"] is True
    res = anon.post("/api/auth/setup", json=ADMIN)
    assert res.status_code == 403 and "flamekeeper restart" in res.json()["detail"]
    auth.started = time.monotonic()  # what a restart does
    assert anon.post("/api/auth/setup", json=ADMIN).status_code == 200


def test_login_logout_and_throttling(client: TestClient) -> None:
    assert client.post("/api/auth/logout").status_code == 204
    res = client.get("/api/worlds")
    assert res.status_code == 401 and res.json()["detail"] == "Sign in first"

    for _ in range(5):
        assert client.post("/api/auth/login",
                           json={**ADMIN, "password": "nope-nope"}).status_code == 401
    res = client.post("/api/auth/login", json=ADMIN)
    assert res.status_code == 429 and "Try again" in res.json()["detail"]

    client.app.state.services.auth._failures.clear()  # the window has passed
    assert client.post("/api/auth/login", json=ADMIN).status_code == 200
    assert client.get("/api/worlds").status_code == 200


def test_changes_need_the_csrf_header(client: TestClient) -> None:
    bare = TestClient(client.app, cookies=dict(client.cookies))  # signed in, no header
    res = bare.post("/api/worlds", json={"display_name": "Sneaky"})
    assert res.status_code == 403 and "X-FlameKeeper" in res.json()["detail"]
    assert bare.get("/api/worlds").status_code == 200  # reads are fine
    assert bare.post("/api/auth/login", json=ADMIN).status_code == 403


def test_change_password_signs_out_other_browsers(client: TestClient) -> None:
    other = TestClient(client.app, headers={"X-FlameKeeper": "1"})
    assert other.post("/api/auth/login", json=ADMIN).status_code == 200
    assert client.post("/api/auth/password", json={"current_password": "wrong-wrong",
                                                   "new_password": "new-password-1"}
                       ).status_code == 403
    assert client.post("/api/auth/password", json={"current_password": ADMIN["password"],
                                                   "new_password": "short"}).status_code == 422
    res = client.post("/api/auth/password", json={"current_password": ADMIN["password"],
                                                  "new_password": "new-password-1"})
    assert res.status_code == 204
    assert client.get("/api/worlds").status_code == 200  # this browser stays signed in
    assert other.get("/api/worlds").status_code == 401
    assert other.post("/api/auth/login", json=ADMIN).status_code == 401
    assert other.post("/api/auth/login", json={**ADMIN, "password": "new-password-1"}
                      ).status_code == 200


def test_cli_set_admin_and_admin_exists(client: TestClient, settings: Settings,
                                        monkeypatch, capsys) -> None:
    """What the installer and `flamekeeper reset-admin` run inside the container."""
    import io

    from app import cli
    from app.config import get_settings

    monkeypatch.setenv("DATA_DIR", str(settings.data_dir))
    get_settings.cache_clear()
    try:
        assert cli.main(["admin-exists"]) == 0
        monkeypatch.setattr("sys.stdin", io.StringIO("short\n"))
        assert cli.main(["set-admin", "boss"]) == 1
        assert "at least 8" in capsys.readouterr().err
        monkeypatch.setattr("sys.stdin", io.StringIO("new secret pass\n"))
        assert cli.main(["set-admin", "boss"]) == 0
        assert "'boss' is ready" in capsys.readouterr().out
        assert cli.main(["set-admin"]) == 2
        assert cli.main(["nonsense"]) == 2
    finally:
        get_settings.cache_clear()
    # The old admin and every session are gone; the new account works.
    assert client.get("/api/worlds").status_code == 401
    assert client.post("/api/auth/login", json=ADMIN).status_code == 401
    assert client.post("/api/auth/login", json={"username": "boss",
                                               "password": "new secret pass"}).status_code == 200


def test_old_setup_code_file_is_removed(settings: Settings, anon: TestClient) -> None:
    (settings.data_dir / "setup-code").write_text("ABCD-EFGH\n")
    anon.app.state.services.auth.log_setup_hint()
    assert not (settings.data_dir / "setup-code").exists()
