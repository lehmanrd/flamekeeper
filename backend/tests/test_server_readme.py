"""Keen's enshrouded_server_readme.txt: parsing, and drift against settings-schema.json."""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.services.server_readme import check_install, compare, parse_readme
from app.services.settings_schema import load_schema
from tests.conftest import FIXTURES

README = FIXTURES / "enshrouded_server_readme-1.0.0.0.txt"
README_09 = FIXTURES / "enshrouded_server_readme-0.9.0.0.txt"


def _readme(path: Path = README):
    return parse_readme(path.read_text(encoding="utf-8"))


def test_parses_every_setting() -> None:
    r = _readme()
    assert r.version == "1.0.0.0"
    assert len(r.settings) == 38
    flame = r.settings["flameLevelFogRemoval"]  # 1.0's short form
    assert (flame.default, flame.min, flame.max) == (11, 0, 20)
    assert flame.title == "Required Flame level to enable placing Flame Altars into the Shroud"
    spawner = r.settings["randomSpawnerAmount"]  # the full entry wins over the 1.0 summary
    assert spawner.default == "Normal" and "Automatic" in spawner.options
    night = r.settings["nightTimeDuration"]
    assert (night.default, night.min, night.max) == (720000000000, 120000000000, 3600000000000)
    assert r.settings["curseModifier"].options == ["Easy", "Normal", "Hard"]
    assert r.settings["fishingDifficulty"].options == ["VeryEasy", "Easy", "Normal", "Hard",
                                                       "VeryHard"]
    assert r.settings["enableDurability"].options is None  # booleans
    assert r.settings["bossDamageFactor"].min == 0.2
    assert "LookingForPlayers" in r.tags and len(r.tags) == 19
    assert r.default_document["gameSettings"]["perkUpgradeRecyclingFactor"] == 0.5
    assert r.server_keys[-1] == "bans"


def test_option_labels_are_the_readme_names() -> None:
    """Dropdowns show the readme's option names, in its order, words spaced (issue #9)."""
    r = _readme()
    for s in load_schema()["settings"]:
        if s["type"] == "enum":
            names = [re.sub(r"(?<=[a-z])(?=[A-Z])", " ", v) for v in r.settings[s["key"]].options]
            assert [o["label"] for o in s["options"]] == names, s["key"]


def test_parses_the_0_9_readme() -> None:
    r = _readme(README_09)
    assert r.version == "0.9.0.0" and len(r.settings) == 37
    assert "flameLevelFogRemoval" not in r.settings


def test_schema_matches_the_shipped_readme() -> None:
    """If this fails after a game update, settings-schema.json needs updating."""
    assert [d.message for d in compare(_readme())] == []


def test_drift_is_detected() -> None:
    schema = copy.deepcopy(load_schema())
    by_key = {s["key"]: s for s in schema["settings"]}
    by_key["enemyDamageFactor"]["max"] = 4
    by_key["curseModifier"]["options"] = by_key["curseModifier"]["options"][1:]
    by_key["dayTimeDuration"]["default"] = 1
    schema["settings"] = [s for s in schema["settings"] if s["key"] != "threatBonus"]
    next(f for f in schema["server"] if f["key"] == "tags")["options"].remove("Thai")

    readme = _readme()
    readme.default_document["shinyNewTopLevel"] = True
    readme.default_document["gameSettings"]["onlyInTheDefaultFile"] = 1
    kinds = {(d.kind, d.key) for d in compare(readme, schema)}
    assert kinds == {("range", "enemyDamageFactor"), ("options", "curseModifier"),
                     ("default", "dayTimeDuration"), ("new_setting", "threatBonus"),
                     ("tags", "tags"), ("top_level", "shinyNewTopLevel"),
                     ("new_setting", "onlyInTheDefaultFile")}


def test_check_install_remembers_what_it_saw(tmp_path: Path) -> None:
    server, state = tmp_path / "server", tmp_path / "state"
    assert check_install(server, state) is None
    server.mkdir()
    (server / "enshrouded_server_readme.txt").write_bytes(README.read_bytes())
    first = check_install(server, state)
    assert first["changed"] and first["version"] == "1.0.0.0" and first["drift"] == []
    assert check_install(server, state)["changed"] is False
    text = README.read_text().replace('"threatBonus": 1,', '"threatBonus": 1,\n"newThing": 2,')
    (server / "enshrouded_server_readme.txt").write_text(text)
    assert check_install(server, state)["changed"] is True
    assert json.loads((state / "readme-seen.json").read_text())["version"] == "1.0.0.0"


def test_readme_endpoint_and_drift_event(client: TestClient, settings: Settings) -> None:
    from tests.test_lifecycle import _create

    world = _create(client)
    res = client.get("/api/system/settings-readme").json()
    assert res["not_installed"] == ["embervale"]

    server = settings.worlds_dir / "embervale" / "game" / "server"
    text = README.read_text().replace(
        "Pacified Enemies", 'Brand New\n"gameSettings": {\n"brandNewFactor": 1,\n}\nNew.\n'
        "Min value: 0.5\nMax value: 2\n\n\nPacified Enemies")
    (server / "enshrouded_server_readme.txt").write_text(text)
    res = client.get("/api/system/settings-readme").json()
    assert res["readmes"][0]["version"] == "1.0.0.0"
    assert [d["key"] for d in res["readmes"][0]["drift"]] == ["brandNewFactor"]

    # The next successful start notices the new readme and warns once.
    job = client.post(f"/api/worlds/{world['id']}/restart", json={"mode": "process"}).json()
    from tests.conftest import wait_job

    assert wait_job(client, job["job_id"])["status"] == "succeeded"
    warnings = [e for e in client.get(f"/api/worlds/{world['id']}/events").json()
                if e["kind"] == "warning"]
    assert len(warnings) == 1 and "settings readme" in warnings[0]["data"]["message"]
