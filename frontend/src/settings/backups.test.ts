import { describe, expect, it } from "vitest";
import type { BackupItem } from "../api/types";
import { presetFor, retentionText, totalSize } from "./backups";

const item = (kind: BackupItem["kind"], size: number): BackupItem => ({
  id: kind + size, filename: "x.zip", path: "backups/x.zip", kind, note: "", size_bytes: size,
  created_at: "2026-10-15T00:00:00Z", game_build_id: null, saved_at: null, exists: true,
});

describe("backups", () => {
  it("maps cron expressions to presets", () => {
    expect(presetFor("0 * * * *")).toBe("0 * * * *");
    expect(presetFor(" 0  *  * * * ")).toBe("0 * * * *");
    expect(presetFor("15 2 * * 1")).toBe("custom");
  });

  it("sums sizes and describes retention", () => {
    const list = [item("scheduled", 10), item("scheduled", 20), item("manual", 5)];
    expect(totalSize(list)).toBe(35);
    expect(retentionText(list, 48)).toBe("2 of 48 scheduled backups kept");
    expect(retentionText(list, 0)).toBe("2 scheduled backups (all kept)");
  });
});
