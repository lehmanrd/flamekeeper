"""Discord notifications: settings, which events are sent, failed jobs, rate limits."""

from __future__ import annotations

from typing import Any

import httpx
from fastapi.testclient import TestClient

from app.services.jobs import JobFailed
from app.services.notifications import event_message, mask
from tests.conftest import wait_for, wait_job
from tests.test_lifecycle import _create

HOOK = "https://discord.com/api/webhooks/123456789/abcDEF-ghi_jkl"


class FakeDiscord:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict[str, Any]]] = []
        self.replies: list[httpx.Response] = []

    async def post(self, url: str, payload: dict[str, Any]) -> httpx.Response:
        self.sent.append((url, payload))
        return self.replies.pop(0) if self.replies else httpx.Response(204)

    def titles(self) -> list[str]:
        return [p["embeds"][0]["title"] for _, p in self.sent]


def _discord(client: TestClient) -> FakeDiscord:
    fake = FakeDiscord()
    client.app.state.services.notifier.post = fake.post
    return fake


def test_settings_validate_and_mask_the_webhook(client: TestClient) -> None:
    view = client.get("/api/settings/notifications").json()
    assert view["discord_configured"] is False
    assert "player_joined" in view["events"] and "player_left" not in view["events"]

    res = client.put("/api/settings/notifications", json={"discord_webhook": "https://evil.example/x"})
    assert res.status_code == 422
    res = client.put("/api/settings/notifications",
                     json={"discord_webhook": HOOK, "events": ["online", "player_left"]})
    view = res.json()
    assert view["discord_configured"] and "abcDEF" not in view["discord_webhook"]
    assert view["discord_webhook"] == "https://discord.com/api/webhooks/123456789/••••••"
    assert view["events"] == ["online", "player_left"]
    assert client.put("/api/settings/notifications", json={"events": ["bogus"]}).status_code == 422

    # Leaving the URL out keeps it; "" removes it.
    assert client.put("/api/settings/notifications", json={"events": []}).json()[
        "discord_configured"]
    assert not client.put("/api/settings/notifications", json={"discord_webhook": ""}).json()[
        "discord_configured"]
    assert mask("garbage") == "••••••"


def test_events_and_failed_jobs_are_sent(client: TestClient) -> None:
    fake = _discord(client)
    world = _create(client)
    services = client.app.state.services
    assert fake.sent == []  # no webhook yet: nothing is sent
    client.put("/api/settings/notifications", json={"discord_webhook": HOOK})

    services.events.record(world["id"], "player_joined", player="Wanderer")
    services.events.record(world["id"], "player_left", player="Wanderer")  # off by default
    assert wait_for(lambda: fake.titles() == ["👋 Wanderer joined Embervale"])
    url, payload = fake.sent[0]
    assert url == HOOK and payload["username"] == "FlameKeeper"
    assert payload["allowed_mentions"] == {"parse": []}

    async def boom(ctx) -> None:  # noqa: ANN001
        raise JobFailed("Disk full")

    job = client.portal.call(lambda: services.jobs.submit("backup", world["id"], boom, "x"))
    assert wait_job(client, job)["status"] == "failed"
    assert wait_for(lambda: "❌ Backup failed on Embervale" in fake.titles())
    assert fake.sent[-1][1]["embeds"][0]["description"] == "Disk full"


def test_rate_limit_is_retried(client: TestClient) -> None:
    fake = _discord(client)
    client.put("/api/settings/notifications", json={"discord_webhook": HOOK})
    world = _create(client)
    fake.sent.clear()
    fake.replies = [httpx.Response(429, json={"retry_after": 0.01})]
    client.app.state.services.events.record(world["id"], "online")  # on by default
    assert wait_for(lambda: len(fake.sent) >= 2)
    assert fake.titles()[:2] == ["🟢 Embervale is online"] * 2


def test_test_message(client: TestClient) -> None:
    fake = _discord(client)
    res = client.post("/api/settings/notifications/test", json={})
    assert res.status_code == 422 and "webhook URL first" in res.json()["detail"]
    assert client.post("/api/settings/notifications/test",
                       json={"discord_webhook": HOOK}).status_code == 204
    assert fake.titles() == ["🔥 FlameKeeper test message"]
    fake.replies = [httpx.Response(404, text='{"message": "Unknown Webhook"}')]
    res = client.post("/api/settings/notifications/test", json={"discord_webhook": HOOK})
    assert res.status_code == 422 and "Unknown Webhook" in res.json()["detail"]


def test_event_messages() -> None:
    assert event_message("updated", "Embervale", {"target": "game", "from_build": "1",
                                                  "to_build": "2"})["description"] == "Build 1 → 2"
    assert event_message("crashed", "Embervale", {})["title"] == "🔴 Embervale crashed"
    assert event_message("config_saved", "Embervale", {}) is None
