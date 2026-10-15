"""Round-trip rule: only managed keys change; everything else survives."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from app.renderers.env import EnvRenderer
from app.renderers.json_file import (
    JsonFileRenderer,
    detect_indent,
    merge_server_config,
    read_config,
)
from tests.conftest import FIXTURES

REFERENCE = FIXTURES / "reference-default-enshrouded_server.json"


def _copy_reference(tmp_path: Path) -> Path:
    target = tmp_path / "server" / "enshrouded_server.json"
    target.parent.mkdir(parents=True)
    shutil.copy(REFERENCE, target)
    return target


def _diff(a: object, b: object, path: str = "") -> list[str]:
    if isinstance(a, dict) and isinstance(b, dict):
        out = []
        for k in sorted(set(a) | set(b)):
            out += _diff(a.get(k, "<missing>"), b.get(k, "<missing>"), f"{path}.{k}")
        return out
    if isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
        out = []
        for i, (x, y) in enumerate(zip(a, b, strict=True)):
            out += _diff(x, y, f"{path}[{i}]")
        return out
    return [] if a == b else [path]


def test_change_one_setting_only_diffs_that_setting(tmp_path: Path) -> None:
    path = _copy_reference(tmp_path)
    before = json.loads(REFERENCE.read_text())
    JsonFileRenderer().write(
        path,
        {"gameSettingsPreset": "Custom", "gameSettings": {"enemyDamageFactor": 2}},
        uid=None,
        gid=None,
    )
    after = json.loads(path.read_text())
    assert sorted(_diff(before, after)) == [".gameSettings.enemyDamageFactor",
                                            ".gameSettingsPreset"]
    assert after["tags"] == [] and after["bannedAccounts"] == []


def test_key_order_and_indent_preserved(tmp_path: Path) -> None:
    path = _copy_reference(tmp_path)
    before = json.loads(REFERENCE.read_text())
    JsonFileRenderer().write(path, {"name": "Renamed"}, uid=None, gid=None)
    raw = path.read_text()
    after = json.loads(raw)
    assert list(after) == list(before)
    assert list(after["gameSettings"]) == list(before["gameSettings"])
    assert detect_indent(raw) == "\t"


def test_unmanaged_and_future_keys_survive(tmp_path: Path) -> None:
    path = _copy_reference(tmp_path)
    doc = json.loads(path.read_text())
    doc["futureTopLevel"] = {"x": 1}
    doc["gameSettings"]["futureSetting"] = 3
    doc["bannedAccounts"] = ["12345"]
    doc["userGroups"][0]["futureRoleKey"] = True
    path.write_text(json.dumps(doc, indent=2))

    roles = json.loads(json.dumps(doc["userGroups"]))
    for r in roles:
        r.pop("futureRoleKey", None)
    roles[0]["canKickBan"] = False
    JsonFileRenderer().write(
        path,
        {"gameSettings": {"enemyDamageFactor": 1.5}, "userGroups": roles},
        uid=None,
        gid=None,
    )
    after, raw = read_config(path)
    assert after["futureTopLevel"] == {"x": 1}
    assert after["gameSettings"]["futureSetting"] == 3
    assert after["bannedAccounts"] == ["12345"]
    assert after["userGroups"][0]["futureRoleKey"] is True
    assert after["userGroups"][0]["canKickBan"] is False
    assert detect_indent(raw) == 2


def test_missing_keys_are_inserted_in_canonical_position() -> None:
    current = {"name": "x", "queryPort": 1, "enableTextChat": False, "userGroups": []}
    merged = merge_server_config(current, {"tags": ["English"], "gameSettingsPreset": "Hard"})
    assert list(merged) == ["name", "queryPort", "tags", "enableTextChat",
                            "gameSettingsPreset", "userGroups"]


def test_deleting_a_role(tmp_path: Path) -> None:
    path = _copy_reference(tmp_path)
    doc = json.loads(path.read_text())
    roles = [r for r in doc["userGroups"] if r["name"] != "Visitor"]
    JsonFileRenderer().write(path, {"userGroups": roles}, uid=None, gid=None)
    after = json.loads(path.read_text())
    assert [r["name"] for r in after["userGroups"]] == ["Admin", "Friend", "Guest"]


def test_atomic_write_leaves_no_temp_files(tmp_path: Path) -> None:
    path = _copy_reference(tmp_path)
    JsonFileRenderer().write(path, {"slotCount": 4}, uid=None, gid=None)
    assert [p.name for p in path.parent.iterdir()] == ["enshrouded_server.json"]


def test_write_to_missing_file_creates_it(tmp_path: Path) -> None:
    path = tmp_path / "new" / "enshrouded_server.json"
    doc = JsonFileRenderer().write(path, {"name": "A", "queryPort": 15637}, uid=None, gid=None)
    assert json.loads(path.read_text()) == doc == {"name": "A", "queryPort": 15637}


def test_env_renderer_maps_settings_and_roles() -> None:
    env = EnvRenderer().container_env(
        {
            "gameSettingsPreset": "Custom",
            "gameSettings": {"enemyDamageFactor": 2, "enableDurability": False,
                             "tombstoneMode": "Everything"},
            "userGroups": [{"name": "Admin", "password": "pw", "canKickBan": True,
                            "reservedSlots": 1}],
        }
    )
    assert env["SERVER_GS_PRESET"] == "Custom"
    assert env["SERVER_GS_ENEMY_DAMAGE_FACTOR"] == "2"
    assert env["SERVER_GS_ENABLE_DURABILITY"] == "false"
    assert env["SERVER_GS_TOMBSTONE_MODE"] == "Everything"
    assert env["SERVER_ROLE_0_NAME"] == "Admin"
    assert env["SERVER_ROLE_0_CAN_KICK_BAN"] == "true"
    assert env["SERVER_ROLE_0_RESERVED_SLOTS"] == "1"
