import { describe, expect, it } from "vitest";
import type { ImportScan } from "../api/types";
import { FROM_SOURCE, fromScan, importBody, importStepErrors, initialImportState, needsPlayerConfirm, portNeedsCheck } from "./importWizard";

function scan(patch: Partial<ImportScan> = {}): ImportScan {
  return {
    token: "0123456789abcdef",
    created_at: "2026-10-19T00:00:00Z",
    source: { kind: "container", container_id: "abc", container_name: "enshrouded", image: "x/enshrouded", label: "enshrouded" },
    config_path: "/opt/enshrouded/server/enshrouded_server.json",
    save_path: "/opt/enshrouded/server/savegame",
    save_options: ["/opt/enshrouded/server/savegame"],
    summary: {
      has_config: true, name: "Old Embervale", slot_count: 8, query_port: 15637, preset: "Hard", custom_settings: 0,
      roles: [], legacy_password: false, bans: 0, save_format: "1.0", save_files: 1, save_bytes: 10, saved_at: null,
    },
    warnings: [],
    errors: [],
    port: 15637,
    reusable_port: 15637,
    running: true,
    players_online: null,
    game_files: null,
    ...patch,
  };
}

describe("import wizard", () => {
  it("takes the old server's name, port and save folder", () => {
    const s = fromScan(initialImportState(), scan());
    expect([s.name, s.port, s.savePath]).toEqual(["Old Embervale", "15637", "/opt/enshrouded/server/savegame"]);
    expect(fromScan(initialImportState(), scan({ summary: { ...scan().summary, name: null } })).name).toBe("enshrouded");
  });

  it("doesn't check the old server's own port", () => {
    const sc = scan();
    const s = fromScan(initialImportState(), sc);
    expect(portNeedsCheck(s, sc)).toBe(false);
    expect(importStepErrors(s, 2, sc, false)).toEqual([]);
    const other = { ...s, port: "15640" };
    expect(portNeedsCheck(other, sc)).toBe(true);
    expect(importStepErrors(other, 2, sc, false)).toEqual(["That port is taken."]);
  });

  it("blocks on config errors and unconfirmed players", () => {
    const s = fromScan(initialImportState(), scan());
    expect(importStepErrors(s, 1, scan({ errors: ["Roles: same password"] }), null)).toHaveLength(1);
    expect(needsPlayerConfirm(scan({ players_online: 0 }))).toBe(false);
    expect(needsPlayerConfirm(scan({ running: false }))).toBe(false);
    expect(importStepErrors(s, 3, scan(), null)).toEqual(["Confirm that the old server may be stopped."]);
    expect(importStepErrors({ ...s, confirmPlayers: true }, 3, scan(), null)).toEqual([]);
  });

  it("builds the create body", () => {
    const sc = scan({ save_options: ["/a/savegame", "/b/savegame"], save_path: "/a/savegame" });
    const s = { ...fromScan(initialImportState(), sc), confirmPlayers: true };
    expect(importBody(s, sc, false)).toMatchObject({
      display_name: "Old Embervale", slug: "old-embervale", query_port: 15637, start: false,
      save_path: undefined, confirm_players: true, backup: { enabled: true, cron: "0 * * * *", retention_count: 48 },
    });
    expect(importBody({ ...s, savePath: "/b/savegame" }, sc).save_path).toBe("/b/savegame");
  });
});

describe("game files from the old server", () => {
  it("defaults to copying them when the scan found them", () => {
    const sc = scan({ game_files: { path: "/opt/enshrouded/server", build: "25988414" } });
    const s = fromScan(initialImportState(), sc);
    expect(s.copyFrom).toBe(FROM_SOURCE);
    expect(importBody(s, sc)).toMatchObject({ copy_game_from_source: true, copy_game_from: undefined });
    expect(fromScan(s, scan()).copyFrom).toBe("");
    expect(importBody({ ...s, copyFrom: "world-1" }, sc)).toMatchObject({ copy_game_from_source: false, copy_game_from: "world-1" });
  });
});
