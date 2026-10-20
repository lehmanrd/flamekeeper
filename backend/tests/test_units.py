"""Pure-function units: state derivation, ports, build id, log parsing, redaction."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.logging_setup import redact, register_secret
from app.services.config_service import default_roles, generate_role_password, initial_document
from app.services.docker_adapter import split_timestamp
from app.services.log_lines import parse_line
from app.services.log_patterns import load_patterns
from app.services.ports import parse_proc_net_udp, suggest_port
from app.services.settings_schema import load_schema
from app.services.state import StateInputs, derive_state
from app.services.world_paths import SLUG_RE, parse_buildid, slugify

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("inputs", "expected"),
    [
        (StateInputs(container_exists=False), "missing"),
        (StateInputs(True, "created"), "stopped"),
        (StateInputs(True, "exited", exit_code=0), "stopped"),
        (StateInputs(True, "exited", exit_code=137), "crashed"),
        (StateInputs(True, "exited", exit_code=137, desired_stopped=True), "stopped"),
        (StateInputs(True, "restarting"), "crashed"),
        (StateInputs(True, "running"), "starting"),
        (StateInputs(True, "running", update_running=True), "updating"),
        (StateInputs(True, "running", host_online_seen=True), "starting"),
        (StateInputs(True, "running", host_online_seen=True, a2s_ok=True), "online"),
        (StateInputs(True, "running", host_online_seen=True, a2s_ok=True, a2s_failures=3),
         "unresponsive"),
        (StateInputs(True, "running", host_online_seen=True, a2s_failures=2, a2s_ok=True),
         "online"),
    ],
)
def test_derive_state(inputs: StateInputs, expected: str) -> None:
    assert derive_state(inputs) == expected


def test_crash_loop_needs_more_than_three_starts_in_ten_minutes() -> None:
    starts = tuple(NOW - timedelta(minutes=m) for m in (1, 2, 3))
    base = dict(container_exists=True, container_status="running", host_online_seen=True,
                a2s_ok=True, now=NOW)
    assert derive_state(StateInputs(**base, recent_starts=starts)) == "online"
    starts += (NOW - timedelta(minutes=4),)
    assert derive_state(StateInputs(**base, recent_starts=starts)) == "crashed"
    old = tuple(NOW - timedelta(minutes=m) for m in (11, 12, 13, 14, 15))
    assert derive_state(StateInputs(**base, recent_starts=old)) == "online"


def test_parse_proc_net_udp() -> None:
    text = (
        "  sl  local_address rem_address   st tx_queue rx_queue\n"
        "  12: 00000000:3D15 00000000:0000 07 00000000:00000000\n"
        "  13: 0100007F:0035 00000000:0000 07 00000000:00000000\n"
    )
    assert parse_proc_net_udp(text) == {15637, 53}


def test_suggest_port_skips_used() -> None:
    assert suggest_port(set()) == 15637
    assert suggest_port({15637, 15638}) == 15639
    assert suggest_port({15637, 15639}) == 15638


def test_parse_buildid() -> None:
    acf = '"AppState"\n{\n\t"appid"\t\t"2278520"\n\t"buildid"\t\t"23178631"\n}'
    assert parse_buildid(acf) == "23178631"
    assert parse_buildid("nothing") is None
    assert parse_buildid('"buildid"\t\t"0"') is None


def test_installed_build_prefers_the_images_version_file(tmp_path) -> None:
    from app.services.world_paths import WorldPaths, installed_build

    paths = WorldPaths(tmp_path)
    assert installed_build(paths) is None
    paths.appmanifest.parent.mkdir(parents=True)
    paths.appmanifest.write_text('"AppState"\n{\n\t"buildid"\t\t"23100000"\n}\n')
    assert installed_build(paths) == "23100000"
    paths.current_version.write_text("23178631\n")
    assert installed_build(paths) == "23178631"
    paths.current_version.write_text("garbage")
    assert installed_build(paths) == "23100000"


def test_slugify_and_slug_rule() -> None:
    assert slugify("Embervale Keep!") == "embervale-keep"
    assert slugify("Ünïcode  World") == "unicode-world"
    assert slugify("***") == "world"
    assert SLUG_RE.match("a")
    assert SLUG_RE.match("world-2")
    assert not SLUG_RE.match("-bad")
    assert not SLUG_RE.match("Bad")


# Real lines captured from a running server.
HOST_ONLINE = "2026-10-14 19:55:47.419 supervisord: enshrouded-server [Session] 'HostOnline' (up)!"
STEAM = ("2026-10-14 19:54:48.215 supervisord: enshrouded-updater  Update state (0x61) "
         "downloading, progress: 61.89 (5453324751 / 8811579458)")
PROC_START = ("2026-10-14 19:55:37.117 supervisord: enshrouded-server INFO - "
              "Running enshrouded-server")
SUPERVISOR = "2026-10-14 19:55:37,110 INFO spawned: 'enshrouded-server' with pid 271"
BAD_CONFIG = ("2026-10-14 19:57:36.657 supervisord: enshrouded-server Internal Error: "
              "Failed to parse Config")


def test_log_patterns_classify_real_lines() -> None:
    p = load_patterns()
    assert p.classify(HOST_ONLINE).kind == "host_online"
    ev = p.classify(STEAM)
    assert ev.kind == "steam_progress" and ev.data["pct"] == 61.89
    assert ev.data["total"] == "8811579458"
    assert p.classify(PROC_START).kind == "server_process_start"
    assert p.classify("2026 supervisord: enshrouded-server [Session] 'HostOnline' (down)!") is None
    assert p.classify(BAD_CONFIG).kind == "failure"
    bad_tag = p.classify("2026-10-14 19:57:36.657 supervisord: enshrouded-server Failed to parse "
                         "enum 'DedicatedServerTagFlag: Invalid value 'probe-tag'.")
    assert bad_tag.kind == "failure"
    assert bad_tag.data["title"] == ("Invalid value 'probe-tag' in enshrouded_server.json "
                                     "(DedicatedServerTagFlag)")


def test_parse_line_structure() -> None:
    line = parse_line(HOST_ONLINE)
    assert line["source"] == "enshrouded-server" and line["level"] == "info"
    assert line["text"] == "[Session] 'HostOnline' (up)!"
    sup = parse_line(SUPERVISOR)
    assert sup["source"] == "supervisord" and sup["level"] == "info"
    # The game container runs in UTC: say so, so the browser shows local time.
    assert line["ts"] == "2026-10-14T19:55:47.419+00:00"
    assert sup["ts"] == "2026-10-14T19:55:37.110+00:00"
    assert parse_line("plain")["ts"] is None
    assert parse_line(BAD_CONFIG)["level"] == "error"
    assert parse_line("plain")["text"] == "plain"
    assert "#033" not in parse_line("2026-10-14 19:54:46.205 supervisord: "
                                     "enshrouded-updater #033[0m")["text"]


def test_split_timestamp() -> None:
    ts, text = split_timestamp("2026-10-14T19:55:47.419123456Z hello world")
    assert ts == datetime(2026, 10, 14, 19, 55, 47, 419123, tzinfo=UTC)
    assert text == "hello world"
    assert split_timestamp("nots") == (None, "nots")


def test_redaction() -> None:
    assert redact('{"password": "hunter2"}') == '{"password": "***"}'
    assert redact("password=abc123 ok") == "password=*** ok"
    register_secret("FriendXyZ123")
    assert redact("joined with FriendXyZ123") == "joined with ***"


def test_default_roles_shape() -> None:
    roles = default_roles()
    assert [r["name"] for r in roles] == ["Admin", "Friend", "Guest", "Visitor"]
    assert list(roles[0]) == ["name", "password", "canKickBan", "canAccessInventories",
                              "canEditWorld", "canEditBase", "canExtendBase", "reservedSlots"]
    assert len({r["password"] for r in roles}) == 4
    assert generate_role_password("Admin").startswith("Admin")


def test_initial_document_matches_reference_shape() -> None:
    import json

    from tests.conftest import FIXTURES

    ref = json.loads((FIXTURES / "reference-default-enshrouded_server.json").read_text())
    doc = initial_document(name="W", query_port=15640, slot_count=8)
    assert list(doc) == list(ref)
    assert set(doc["gameSettings"]) == set(ref["gameSettings"])
    for key, value in ref["gameSettings"].items():
        assert doc["gameSettings"][key] == value, key
    with pytest.raises(ValueError):
        initial_document(name="W", query_port=1, slot_count=1, preset="Nope")


def test_schema_keys_cover_reference_file() -> None:
    import json

    from tests.conftest import FIXTURES

    ref = json.loads((FIXTURES / "reference-default-enshrouded_server.json").read_text())
    schema_keys = {s["key"] for s in load_schema()["settings"]}
    assert schema_keys == set(ref["gameSettings"])


def test_schema_enums_match_values_reported_by_real_server() -> None:
    """server-enums.json was harvested from a real server."""
    import json

    from tests.conftest import FIXTURES

    server = json.loads((FIXTURES / "server-enums.json").read_text())
    schema = load_schema()
    for setting in schema["settings"]:
        if setting["type"] == "enum":
            ours = sorted(o["value"] for o in setting["options"])
            assert ours == sorted(server[setting["key"]]["values"]), setting["key"]
    voice = next(f for f in schema["server"] if f["key"] == "voiceChatMode")
    assert sorted(voice["options"]) == sorted(server["voiceChatMode"]["values"])
    assert sorted(schema["presets"]) == sorted(server["gameSettingsPreset"]["values"])


def test_player_join_leave_real_lines() -> None:
    p = load_patterns()
    pre = "2026-10-14 21:03:02.815 supervisord: enshrouded-server "
    joined = p.classify(pre + "[server] Player 'Wanderer' logged in with Permissions:")
    assert joined.kind == "player_joined" and joined.data["name"] == "Wanderer"
    left = p.classify(pre + "[server] Remove Player 'Wanderer'")
    assert left.kind == "player_left" and left.data["name"] == "Wanderer"
    # Same leave also logs this line; it must not count twice.
    assert p.classify(pre + "[server] Remove Entity for Player 'Wanderer'") is None
    assert p.classify(pre + "[server] Machine '1': Player '0(0)' logged in") is None
    assert parse_line(pre + "[server] Player 'Ryn' logged in with Permissions:")["player"]
