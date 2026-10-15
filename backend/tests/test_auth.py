"""Admin login: first setup with the setup code, sessions, throttling, CSRF header."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.config import Settings
from app.services.auth import hash_password, verify_password
from tests.conftest import ADMIN, setup_code


def _setup(c: TestClient, settings: Settings, **over) -> object:
    return c.post("/api/auth/setup", json={"setup_code": setup_code(settings), **ADMIN, **over})


def test_password_hashing() -> None:
    stored = hash_password("correct-horse-battery")
    assert stored.startswith("scrypt$") and "correct" not in stored
    assert verify_password("correct-horse-battery", stored)
    assert not verify_password("wrong", stored)
    assert not verify_password("x", "not-a-hash")


def test_first_setup_needs_the_code(anon: TestClient, settings: Settings) -> None:
    assert anon.get("/api/auth/status").json() == {"setup_required": True, "user": None}
    assert anon.get("/api/health").status_code == 200  # public
    res = anon.get("/api/worlds")
    assert res.status_code == 401 and res.json()["detail"] == "Set up FlameKeeper first"
    assert anon.post("/api/auth/login", json=ADMIN).status_code == 409

    code_file = settings.data_dir / "setup-code"
    assert code_file.stat().st_mode & 0o777 == 0o600
    bad = anon.post("/api/auth/setup", json={"setup_code": "AAAA-BBBB", **ADMIN})
    assert bad.status_code == 403
    assert _setup(anon, settings, password="short").status_code == 422
    assert _setup(anon, settings, username="no spaces").status_code == 422

    # Lowercase, no dash: still accepted.
    code = setup_code(settings).lower().replace("-", "")
    res = anon.post("/api/auth/setup", json={"setup_code": code, **ADMIN})
    assert res.status_code == 200 and res.json() == {"user": {"username": "admin"}}
    cookie = res.headers["set-cookie"]
    assert "fk_session=" in cookie and "HttpOnly" in cookie and "samesite=lax" in cookie.lower()
    assert not code_file.exists()
    assert anon.get("/api/worlds").status_code == 200
    assert anon.get("/api/auth/status").json()["user"] == {"username": "admin"}
    assert anon.post("/api/auth/setup", json={"setup_code": code, **ADMIN}).status_code == 409


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


def test_reset_admin_goes_back_to_setup(client: TestClient, settings: Settings) -> None:
    code = client.app.state.services.auth.reset()
    assert setup_code(settings) == code
    assert client.get("/api/worlds").status_code == 401
    assert client.get("/api/auth/status").json()["setup_required"] is True
    res = client.post("/api/auth/setup", json={"setup_code": code, "username": "boss",
                                               "password": "another-secret"})
    assert res.status_code == 200


def test_cli_setup_code_and_reset(settings: Settings, monkeypatch, capsys) -> None:
    from app import cli
    from app.config import get_settings

    monkeypatch.setenv("DATA_DIR", str(settings.data_dir))
    get_settings.cache_clear()
    try:
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        assert cli.main(["setup-code"]) == 0
        code = capsys.readouterr().out.strip()
        assert code == setup_code(settings) and len(code) == 9
        assert cli.main(["reset-admin"]) == 0
        assert "setup code" in capsys.readouterr().out
        assert cli.main(["nonsense"]) == 2
    finally:
        get_settings.cache_clear()
