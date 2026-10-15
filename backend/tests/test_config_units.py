"""Unit tests for config validation and diffs (no HTTP)."""

from __future__ import annotations

from app.services.config_diff import diff_documents
from app.services.config_validation import (
    settings_by_key,
    validate_game_settings,
    validate_roles,
    validate_setting,
)


def test_validate_setting_types() -> None:
    s = settings_by_key()
    assert validate_setting(s["enemyDamageFactor"], 2) is None
    assert validate_setting(s["enemyDamageFactor"], 2.0) is None
    assert validate_setting(s["enemyDamageFactor"], True) == "must be a number"
    assert "between" in validate_setting(s["enemyDamageFactor"], 0.1)
    assert validate_setting(s["nightTimeDuration"], 3_600_000_000_000) is None
    assert validate_setting(s["nightTimeDuration"], 3_600_000_000_000.5) == "must be a whole number"
    assert "between" in validate_setting(s["nightTimeDuration"], 7_200_000_000_000)
    assert validate_setting(s["pacifyAllEnemies"], 0) == "must be true or false"
    assert validate_setting(s["randomSpawnerAmount"], "Extreme") is None
    assert "Few" in validate_setting(s["randomSpawnerAmount"], "More")


def test_full_game_settings_must_be_complete() -> None:
    defaults = {k: v["default"] for k, v in settings_by_key().items()}
    assert not validate_game_settings(defaults, partial=False).errors
    del defaults["curseModifier"]
    errors = validate_game_settings(defaults, partial=False).errors
    assert errors == [{"loc": ["gameSettings", "curseModifier"], "msg": "missing"}]


def test_roles_open_role_is_a_warning() -> None:
    perms = {"canKickBan": False, "canAccessInventories": False, "canEditWorld": False,
             "canEditBase": False, "canExtendBase": False, "reservedSlots": 0}
    p = validate_roles([{"name": "Visitor", "password": "", **perms}], 16)
    assert not p.errors
    assert p.warnings[0]["msg"] == "Open: anyone can join as Visitor without a password"
    p = validate_roles([{"name": "Admin", "password": " padded ", **perms}], 16)
    assert not p.errors and "space" in p.warnings[0]["msg"]


def test_diff_labels_masks_and_order() -> None:
    old = {
        "name": "A", "tags": [], "gameSettingsPreset": "Default",
        "gameSettings": {"enemyDamageFactor": 1, "curseModifier": "Normal"},
        "userGroups": [{"name": "Admin", "password": "x", "canKickBan": True},
                       {"name": "Guest", "password": "y", "canKickBan": False}],
        "bannedAccounts": [], "password": "legacy",
    }
    new = {
        "name": "B", "tags": ["English"], "gameSettingsPreset": "Custom",
        "gameSettings": {"enemyDamageFactor": 2.0, "curseModifier": "Normal"},
        "userGroups": [{"name": "Guest", "password": "", "canKickBan": False},
                       {"name": "Admin", "password": "x", "canKickBan": False}],
        "bannedAccounts": ["1"],
    }
    changes = diff_documents(old, new)
    labels = [c["label"] for c in changes]
    assert labels == ["Server name", "Server tags", "Preset", "Enemy Damage",
                      "Guest: Password", "Admin: Kick / ban", "Role order", "bannedAccounts",
                      "Legacy password"]
    guest_pw = changes[4]
    assert guest_pw["secret"] and guest_pw["kind"] == "opened"
    assert guest_pw["before"] is None and guest_pw["after"] is None
    assert changes[-1]["before"] is None  # legacy password value is never shown
    # 2 and 2.0 are the same value (the server rewrites numbers as floats).
    assert diff_documents({"gameSettings": {"a": 2}}, {"gameSettings": {"a": 2.0}}) == []
